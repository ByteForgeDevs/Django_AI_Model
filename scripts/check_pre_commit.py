"""Check `.pre-commit-hooks.yaml` against the CLI it invokes.

A hook manifest is executed inside someone else's repository, by a tool that
builds its own environment from our package. Almost nothing about it fails
here: a flag that no longer exists, a family letter that was renamed, an
`id` the documentation no longer matches -- each of those surfaces as a
failing commit in a repository we cannot see.

The checks below are the ones that can be made without running pre-commit:

* every hook invokes a command the CLI actually has;
* every flag in `args` exists on that command, and every value handed to a
  choice option is one of its choices;
* `pass_filenames` is false, because djaudit takes one directory and pre-commit
  would otherwise append the changed files to it;
* every hook id is documented, and every documented id exists.

`tests/test_pre_commit.py` runs the real `pre-commit` against a fixture
repository, which is what proves the manifest is one the tool accepts.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

import typer.main
import yaml

from djaudit.cli import app

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / ".pre-commit-hooks.yaml"
DOC = ROOT / "docs/pre-commit.md"


def _commands() -> dict[str, Any]:
    group = typer.main.get_command(app)
    return dict(group.commands)  # type: ignore[attr-defined]


def _params(command: Any) -> dict[str, Any]:
    return {
        opt: param
        for param in command.params
        for opt in getattr(param, "opts", [])
        if opt.startswith("--")
    }


def _check_args(hook: dict[str, Any], commands: dict[str, Any]) -> list[str]:
    name = hook.get("id")
    entry = str(hook.get("entry", "")).split()
    if len(entry) < 2 or entry[0] != "djaudit":
        return [f"hook {name!r} does not invoke djaudit"]
    if entry[1] not in commands:
        return [f"hook {name!r} invokes `djaudit {entry[1]}`, which is not a command"]

    command = commands[entry[1]]
    params = _params(command)
    problems: list[str] = []
    args = [str(a) for a in hook.get("args", [])]

    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            problems.append(f"hook {name!r} passes a bare argument {token!r} in args")
            index += 1
            continue
        if token not in params:
            problems.append(f"hook {name!r} passes {token}, which `djaudit {entry[1]}` rejects")
            index += 2
            continue
        param = params[token]
        # A flag with no value consumes nothing; everything else takes the
        # next token, and that token has to be one the option accepts.
        if getattr(param, "is_flag", False):
            index += 1
            continue
        if index + 1 >= len(args):
            problems.append(f"hook {name!r} ends with {token}, which needs a value")
            break
        value = args[index + 1]
        allowed = list(getattr(param.type, "choices", []))
        if allowed and value not in allowed:
            problems.append(
                f"hook {name!r} passes `{token} {value}`, but {token} accepts {', '.join(allowed)}"
            )
        index += 2
    return problems


def _check_doc(ids: list[str]) -> list[str]:
    if not DOC.exists():
        return [f"{DOC.relative_to(ROOT)} is missing"]
    text = DOC.read_text()
    where = DOC.relative_to(ROOT)
    problems = [f"hook {name!r} is not documented in {where}" for name in ids if name not in text]

    # Only the "The hooks" section is the catalogue. Elsewhere `- id:` appears
    # inside configuration examples, where a trailing comment is legitimate and
    # a repeated id is the point.
    catalogue = text.split("## The hooks", 1)
    if len(catalogue) != 2:
        return [*problems, f"{where} has no '## The hooks' section listing what is published"]
    section = catalogue[1].split("\n## ", 1)[0]
    documented = [
        line.strip().removeprefix("- id:").strip()
        for line in section.splitlines()
        if line.strip().startswith("- id:")
    ]
    for cited in documented:
        if cited not in ids:
            problems.append(f"{where} documents hook {cited!r}, which does not exist")
    for name in ids:
        if name not in documented:
            problems.append(f"{where} does not list hook {name!r} under '## The hooks'")
    return problems


def check() -> int:
    if not MANIFEST.exists():
        print("::error::.pre-commit-hooks.yaml is missing")
        return 1

    hooks = yaml.safe_load(MANIFEST.read_text())
    if not isinstance(hooks, list) or not hooks:
        print("::error::.pre-commit-hooks.yaml must be a non-empty list of hooks")
        return 1

    commands = _commands()
    problems: list[str] = []
    ids: list[str] = []

    for hook in hooks:
        name = hook.get("id")
        ids.append(name)
        for required in ("id", "name", "entry", "language"):
            if not hook.get(required):
                problems.append(f"hook {name!r} is missing `{required}`")
        if hook.get("language") != "python":
            problems.append(f"hook {name!r} declares language {hook.get('language')!r}, not python")
        if hook.get("pass_filenames") is not False:
            problems.append(
                f"hook {name!r} does not set `pass_filenames: false`, so pre-commit appends the "
                "changed files to a command that takes one directory"
            )
        if not hook.get("types"):
            problems.append(f"hook {name!r} does not restrict `types`, so it runs on every commit")
        problems.extend(_check_args(hook, commands))

    if len(set(ids)) != len(ids):
        problems.append("two hooks share an id")
    problems.extend(_check_doc(ids))

    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    print(f"pre-commit manifest consistent: {len(hooks)} hooks ({', '.join(ids)})")
    return 0


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
