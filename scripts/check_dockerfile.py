"""Check the `Dockerfile` against the CLI, the packaging metadata and the docs.

The image cannot be built on the machine this was written on -- there is no
docker or podman here -- so CI builds it and runs it against fixtures. That
leaves a gap between writing the file and learning whether it works, and this
script closes as much of it as can be closed without a container runtime:

* every path the build stage copies exists, because a missing one fails the
  build with a message about the context rather than about the file;
* the base image is pinned to a real Python version, and that version satisfies
  `requires-python`;
* the image drops to a non-root user, and does so *after* the install;
* `ENTRYPOINT` names a command the CLI has, `CMD`'s first token is a real
  subcommand, and its remaining arguments are ones that subcommand accepts;
* the directory `CMD` audits is the one `WORKDIR` sets, because a mismatch
  produces an image that audits an empty directory and exits 0;
* the documentation's mount path is the same directory.

`tests/test_container.py` builds a wheel from exactly the copied paths, which
is the part of the image build that does not need a runtime.
"""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib
from typing import Any

import typer.main

from djaudit.cli import app

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOC = ROOT / "docs/container.md"
PYPROJECT = ROOT / "pyproject.toml"

FROM = re.compile(r"^FROM\s+(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?", re.MULTILINE)
COPY = re.compile(r"^COPY\s+(?P<args>.+)$", re.MULTILINE)
USER = re.compile(r"^USER\s+(?P<user>\S+)", re.MULTILINE)
WORKDIR = re.compile(r"^WORKDIR\s+(?P<path>\S+)", re.MULTILINE)
JSON_ARRAY = re.compile(r"^(?P<kind>ENTRYPOINT|CMD)\s+\[(?P<items>.*)\]", re.MULTILINE)
PYTHON_TAG = re.compile(r"^python:(?P<version>\d+\.\d+)-\S+$")


def _tokens(items: str) -> list[str]:
    return [part.strip().strip('"') for part in items.split(",") if part.strip()]


def _commands() -> dict[str, Any]:
    group = typer.main.get_command(app)
    return dict(group.commands)  # type: ignore[attr-defined]


def _copied(text: str) -> list[str]:
    """Every source path copied from the build context.

    `COPY --from=build` moves files between stages, so its sources are not
    context paths and checking them against the repository would be wrong.
    """
    sources: list[str] = []
    for match in COPY.finditer(text):
        args = match.group("args").split()
        if any(arg.startswith("--from=") for arg in args):
            continue
        sources.extend(arg for arg in args[:-1] if not arg.startswith("--"))
    return sources


def _check_base(text: str) -> tuple[list[str], str | None]:
    problems: list[str] = []
    images = [match.group("image") for match in FROM.finditer(text)]
    if not images:
        return ["the Dockerfile has no FROM"], None
    for image in images:
        if image.endswith(":latest") or ":" not in image:
            problems.append(f"base image {image!r} is not pinned to a version")
    if len(set(images)) != 1:
        problems.append(
            f"the stages build on different bases ({', '.join(sorted(set(images)))}), so the "
            "wheel is built against a different interpreter than the one that runs it"
        )
    return problems, images[0]


def _check_python(image: str) -> list[str]:
    tag = PYTHON_TAG.match(image)
    if not tag:
        return [f"base image {image!r} is not a python image this script can read"]
    required = tomllib.loads(PYPROJECT.read_text())["project"]["requires-python"]
    floor = required.removeprefix(">=").strip()
    have = tuple(int(part) for part in tag.group("version").split("."))
    want = tuple(int(part) for part in floor.split("."))
    if have < want:
        return [f"the image runs Python {tag.group('version')}, below requires-python {required}"]
    return []


def _check_entrypoint(text: str, workdir: str | None) -> list[str]:
    found = {
        match.group("kind"): _tokens(match.group("items")) for match in JSON_ARRAY.finditer(text)
    }
    problems: list[str] = []
    entrypoint = found.get("ENTRYPOINT")
    if not entrypoint:
        return ["the Dockerfile has no ENTRYPOINT, so `docker run` would start a shell"]
    if entrypoint[0] != "djaudit":
        problems.append(f"ENTRYPOINT runs {entrypoint[0]!r}, not djaudit")

    command = found.get("CMD")
    if not command:
        return [*problems, "the Dockerfile has no CMD, so a bare `docker run` does nothing useful"]

    commands = _commands()
    name = command[0]
    if name not in commands:
        return [*problems, f"CMD runs `djaudit {name}`, which is not a command"]

    positional = [token for token in command[1:] if not token.startswith("-")]
    if workdir and positional and positional[0] != workdir:
        problems.append(
            f"CMD audits {positional[0]!r} but WORKDIR is {workdir!r}; an image that audits a "
            "directory nobody mounted into reports nothing and exits 0"
        )

    params = {
        opt: param
        for param in commands[name].params
        for opt in getattr(param, "opts", [])
        if opt.startswith("--")
    }
    for token in command[1:]:
        if token.startswith("--") and token not in params:
            problems.append(f"CMD passes {token}, which `djaudit {name}` rejects")
    return problems


def _check_doc(workdir: str | None) -> list[str]:
    # Not `DOC.relative_to(ROOT)`: that raises when the two are unrelated, and a
    # gate that crashes while reporting a problem reports nothing at all. Tests
    # point DOC at a temporary file, and so would anyone vendoring this check.
    label = DOC.relative_to(ROOT) if DOC.is_relative_to(ROOT) else DOC
    if not DOC.exists():
        return [f"{label} is missing"]
    text = DOC.read_text()
    problems: list[str] = []
    if workdir and workdir not in text:
        problems.append(
            f"{label} never mentions {workdir}, which is where a project has to "
            "be mounted for the image to audit it"
        )
    if "--live" not in text:
        problems.append(
            f"{label} does not say what happens to the live tier, which cannot "
            "work in an image that does not have the target's dependencies"
        )
    return problems


def _check_copied(text: str) -> list[str]:
    """Every path the build stage copies must be in the repository.

    Extracted for the same reason as `_check_user`: a test that reimplements
    the loop to exercise it would keep passing after `check` stopped calling
    it, which is how this check was found to be untested in the first place.
    """
    return [
        f"the build stage copies {source!r}, which is not in the repository"
        for source in _copied(text)
        if not (ROOT / source).exists()
    ]


def _check_user(text: str) -> list[str]:
    """The image must install as root and then stop being root.

    A function rather than inline in `check`, because a test that reimplements
    this logic to exercise it would keep passing if the gate stopped calling
    it -- the reimplementation *is* the thing under test at that point.
    """
    user = USER.search(text)
    if not user:
        return ["the image never drops privileges with USER"]
    if user.group("user") == "root":
        return ["the image runs as root"]
    if text.rfind("pip install") > user.start():
        return [
            "USER comes before the last `pip install`, so the install runs unprivileged "
            "and writes somewhere the entrypoint will not look"
        ]
    return []


def check() -> int:
    if not DOCKERFILE.exists():
        print("::error::Dockerfile is missing")
        return 1

    text = DOCKERFILE.read_text()
    problems, image = _check_base(text)
    if image:
        problems.extend(_check_python(image))

    problems.extend(_check_copied(text))
    problems.extend(_check_user(text))

    workdirs = [match.group("path") for match in WORKDIR.finditer(text)]
    workdir = workdirs[-1] if workdirs else None
    problems.extend(_check_entrypoint(text, workdir))
    problems.extend(_check_doc(workdir))

    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    print(f"Dockerfile consistent: {image} · audits {workdir}")
    return 0


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
