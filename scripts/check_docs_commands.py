"""Every `djaudit` command in the documentation has to be a real command.

Documentation drifts silently. A flag gets renamed, a subcommand gains a
required argument, and the guide that told people to run it goes on looking
correct forever -- nobody re-reads a paragraph they wrote last month, and no
test executes prose. This project has already shipped three versions of that
defect: two docs pinning an install of `v0.1.0` at version 0.2.0, both of which
passed their own gates, and a test here that invoked `--write-baseline` with no
argument.

So this reads every fenced block under `docs/` and in `README.md`, finds the
lines that invoke `djaudit`, and asks click what it thinks of them. It checks:

- the subcommand exists;
- every long option exists on that subcommand;
- an option that requires a value was given one;
- an option that takes no value was not given one.

It also resolves every relative markdown link, because a guide that points at
a page nobody wrote reads exactly like one that does not.

It deliberately does not execute anything. Executing the examples would need a
Django project, network access and a container runtime, and would test the
examples' environment rather than the examples.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer.main

from djaudit.cli import app

ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r"^```")
# A documented invocation, with or without a `$` prompt, and allowing a
# container runtime to be the thing that carries the arguments.
INVOCATION = re.compile(r"(?:^|\s)djaudit\s")
# Placeholders a reader is expected to substitute. `<...>` and `{{ ... }}` are
# not paths we can check, but they are not errors either.
PLACEHOLDER = re.compile(r"[<>${}]")


def _click_command() -> Any:
    return typer.main.get_command(app)


def _subcommands() -> dict[str, Any]:
    group = _click_command()
    return dict(group.commands)


class _Help:
    is_flag = True
    nargs = 0


_HELP = _Help()


def _options(command: Any) -> dict[str, Any]:
    # click injects `--help` rather than declaring it, so it is not in `params`.
    out: dict[str, Any] = {"--help": _HELP}
    for param in command.params:
        for opt in getattr(param, "opts", []) + getattr(param, "secondary_opts", []):
            out[opt] = param
    return out


def _lines(text: str) -> list[str]:
    """Lines inside fenced blocks only. Prose mentions a flag; it does not run it."""
    inside = False
    found = []
    for line in text.splitlines():
        if FENCE.match(line.strip()):
            inside = not inside
            continue
        if inside:
            found.append(line)
    return found


def _tokens(line: str) -> list[str] | None:
    """The argv a documented line would produce, from `djaudit` onwards."""
    stripped = line.strip().removeprefix("$ ").strip()
    if not INVOCATION.search(f" {stripped}"):
        return None
    if stripped.endswith("\\"):  # a continued line; joined by the caller
        return None
    try:
        parts = shlex.split(stripped, comments=True)
    except ValueError:
        return None
    if "djaudit" not in parts:
        return None
    return parts[parts.index("djaudit") + 1 :]


def _join_continuations(lines: list[str]) -> list[str]:
    joined: list[str] = []
    buffer = ""
    for line in lines:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1].strip() + " "
            continue
        joined.append(buffer + stripped.strip())
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


def _check_line(argv: list[str], where: str, problems: list[str]) -> None:
    commands = _subcommands()
    if not argv:
        return
    name = argv[0]
    if name.startswith("-"):
        # `djaudit --help` and friends: check against the group itself.
        command = _click_command()
        rest = argv
    elif name not in commands:
        problems.append(
            f"{where}: unknown subcommand {name!r} (have {', '.join(sorted(commands))})"
        )
        return
    else:
        command = commands[name]
        rest = argv[1:]

    options = _options(command)
    index = 0
    while index < len(rest):
        token = rest[index]
        index += 1
        if not token.startswith("-") or token == "-":
            continue
        flag, sep, _ = token.partition("=")
        if flag not in options:
            problems.append(f"{where}: {name} has no option {flag!r}")
            continue
        param = options[flag]
        wants_value = getattr(param, "is_flag", False) is False and param.nargs != 0
        if not wants_value:
            if sep:
                problems.append(f"{where}: {flag} takes no value, but got {token!r}")
            continue
        if sep:
            continue
        if index >= len(rest) or (rest[index].startswith("-") and rest[index] != "-"):
            problems.append(f"{where}: {flag} requires a value")
            continue
        index += 1


LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _check_links(path: Path, text: str, problems: list[str]) -> int:
    """Relative links only. External URLs are somebody else's uptime."""
    checked = 0
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        anchorless = target.split("#", 1)[0]
        if not anchorless:
            continue
        checked += 1
        if not (path.parent / anchorless).exists():
            problems.append(f"{path.relative_to(ROOT)}: link to {target!r} does not resolve")
    return checked


def check() -> int:
    problems: list[str] = []
    files = [*sorted(ROOT.joinpath("docs").rglob("*.md")), ROOT / "README.md"]
    checked = 0
    links = 0
    for path in files:
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        links += _check_links(path, text, problems)
        for line in _join_continuations(_lines(text)):
            argv = _tokens(line)
            if argv is None:
                continue
            if any(PLACEHOLDER.search(token) for token in argv):
                # `-o <file>` and `${{ }}` are for the reader to fill in.
                argv = [token for token in argv if not PLACEHOLDER.search(token)]
            checked += 1
            _check_line(argv, f"{rel}", problems)

    if problems:
        print("::error::documentation that does not match reality:")
        for problem in sorted(set(problems)):
            print(f"  {problem}")
        return 1
    print(f"documentation checks out: {checked} invocations · {links} links · {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
