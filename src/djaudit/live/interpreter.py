"""Locating the interpreter that has the target's dependencies installed.

Every live-tier rule ultimately asks the same thing: run this in the
environment the target actually runs in. Getting that wrong is not a degraded
answer but a wrong one -- `manage.py check --deploy` under the wrong Django
reports the wrong checks, and `sqlmigrate` under the wrong backend emits the
wrong SQL.

**The environment is identified by `pyvenv.cfg`, not by the directory name.**
That file is what PEP 405 defines a virtual environment as, and its absence is
what tells a bare directory called `venv` apart from a real one. It also names
its creator, which was measured rather than assumed by making one environment
with each of the three tools that matter:

| creator      | key that identifies it | version key    |
|--------------|------------------------|----------------|
| stdlib venv  | `command`              | `version`      |
| `virtualenv` | `virtualenv = 20.35.4` | `version_info` |
| `uv`         | `uv = 0.12.1`          | `version_info` |

All three write `home`, so `home` is the marker and the rest is provenance.
Recording the creator costs nothing here and matters later: `uv` environments
may have no `pip`, which changes how a missing dependency should be explained.

**djaudit's own environment is refused, loudly.** This is the failure mode that
matters, because it does not look like a failure. djaudit normally runs from a
virtualenv itself, so `$VIRTUAL_ENV` is usually set and usually points at
*ours*. Following it would run the target's `manage.py` against our Django, our
settings and our installed packages, and produce findings that look completely
ordinary. `$VIRTUAL_ENV` is therefore only believed when it points inside the
target, and any candidate resolving to `sys.prefix` is rejected outright.

**Nothing here executes anything.** Detection is filesystem-only, so it is safe
to run against a repository nobody has vetted. Whether the interpreter actually
has Django installed is a question that needs a subprocess, and it belongs to
the runner rather than here.

**Environments kept outside the project are recorded as declines rather than
guessed at.** Poetry and pipenv place theirs in a user-level cache under a
directory whose name ends in a hash of the project path. Globbing for the name
half and hoping for a single match would silently attach to a different
checkout of the same project -- the exact class of mistake this module exists to
prevent. Asking the tool is the correct way and needs a subprocess, so it lands
with the runner. `.tox` is declined for a different reason: it holds one
environment per test factor, none of which is "the" environment, and choosing
between them would be choosing a Python version at random.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

CONFIG = "pyvenv.cfg"
"""PEP 405's marker file. Its presence is the definition of a virtualenv."""

CANDIDATE_DIRECTORIES = (".venv", "venv", "env", ".virtualenv")
"""Conventional in-project environment directories, in the order tried.

`.venv` first because it is what `uv`, Poetry's `in-project` setting and most
recent documentation produce. The rest are older conventions that still turn up.
"""

BIN_DIRECTORIES = ("bin", "Scripts")
"""Posix and Windows layouts. Both are tried rather than branching on the host,
because the host running djaudit need not be the host that made the target's
environment -- a checkout on a shared volume, or a container mount."""

EXECUTABLES = ("python", "python3", "python.exe")

DEFERRED = {
    "poetry.lock": (
        "Poetry keeps its environment in a user-level cache under a name ending "
        "in a hash of the project path, so it cannot be located from the "
        "project alone. `poetry env info --path` answers it, which needs a "
        "subprocess."
    ),
    "Pipfile.lock": (
        "Pipenv keeps its environment in a user-level cache unless "
        "`PIPENV_VENV_IN_PROJECT` is set, in which case it is `.venv` and was "
        "already tried. `pipenv --venv` answers the other case, which needs a "
        "subprocess."
    ),
    "tox.ini": (
        "`.tox` holds one environment per test factor and none of them is the "
        "project's environment; picking one would pick a Python version at "
        "random."
    ),
}
"""Files that prove an environment exists somewhere we decline to guess at.

Recorded so that "no interpreter found" can say which tool the project uses
rather than shrugging. `pyproject.toml` is deliberately absent: it says nothing
about where an environment lives, and most projects with one use `.venv`.
"""


class Creator(StrEnum):
    """Which tool built the environment, read from its own `pyvenv.cfg` key."""

    VENV = "venv"
    VIRTUALENV = "virtualenv"
    UV = "uv"
    UNKNOWN = "unknown"
    """A `pyvenv.cfg` with `home` but none of the keys we know. Still usable."""


class Interpreter(NamedTuple):
    """One interpreter, and how we came to believe in it.

    A `NamedTuple` rather than a frozen dataclass throughout this module, so
    that immutability is a property of the type instead of two keyword
    arguments a reader has to trust were passed.
    """

    executable: Path
    prefix: Path
    """The environment root -- the directory holding `pyvenv.cfg`."""

    creator: Creator
    version: str | None
    """As recorded by the creator. `None` when it wrote no version key."""

    found_by: str
    """Provenance, phrased for a person reading a diagnostic."""


class Rejection(NamedTuple):
    """A candidate that was considered and refused, with the reason.

    Kept because "we found nothing" and "we found your virtualenv and refused
    it because it is ours" are very different things to be told.
    """

    path: Path
    reason: str


class Search(NamedTuple):
    """The outcome of a search, including the road not taken."""

    interpreter: Interpreter | None
    rejected: tuple[Rejection, ...] = ()
    deferred: tuple[str, ...] = ()
    """Reasons an environment is believed to exist but was not looked for."""

    @property
    def found(self) -> bool:
        return self.interpreter is not None

    def explain(self) -> str:
        """Why the live tier is or is not available, as one paragraph.

        The plan requires that absence of the live tier is reported and never
        silently ignored, and a report that cannot say *why* is not much better
        than silence.
        """
        if self.interpreter is not None:
            return f"live tier using {self.interpreter.executable} ({self.interpreter.found_by})"
        # Lead with what was searched. A list of rejections alone reads as
        # though those were the only places looked, which is the opposite of
        # what happened.
        lead = (
            f"no usable interpreter found in the target ({', '.join(CANDIDATE_DIRECTORIES)} "
            "were tried); live-tier rules will not run."
        )
        parts = [f"{r.path}: {r.reason}." for r in self.rejected]
        parts.extend(self.deferred)
        return " ".join([lead, *parts])


def read_config(prefix: Path) -> dict[str, str] | None:
    """`pyvenv.cfg` as a mapping, or `None` when it is absent or unreadable.

    The format is `key = value` per line, which is close enough to several
    standard formats to be tempting and is none of them: `configparser` rejects
    it outright for having no section header. Parsed directly instead.

    Values are allowed to contain `=` -- a `command` key records a whole command
    line -- so only the first separator splits.
    """
    config = prefix / CONFIG
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    return values


def creator_of(config: Mapping[str, str]) -> Creator:
    """Which tool wrote this `pyvenv.cfg`, by the key only it writes."""
    if "uv" in config:
        return Creator.UV
    if "virtualenv" in config:
        return Creator.VIRTUALENV
    if "command" in config:
        return Creator.VENV
    return Creator.UNKNOWN


def executable_in(prefix: Path) -> Path | None:
    """The interpreter inside an environment, or `None` if it has none.

    An environment whose `python` has been removed -- or was never made, as
    with a half-finished `--without-pip` run interrupted partway -- has a
    `pyvenv.cfg` and nothing to run.
    """
    for directory in BIN_DIRECTORIES:
        for name in EXECUTABLES:
            candidate = prefix / directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _is_ours(prefix: Path) -> bool:
    """Whether this environment is the one djaudit is running from.

    Compared by resolved path rather than by string, because `$VIRTUAL_ENV` is
    routinely a symlink or a relative path to the same directory `sys.prefix`
    names absolutely.

    Not guarded against `OSError`: `Path.resolve` defaults to `strict=False`,
    which was measured to return the path unchanged for both a missing
    directory and a symlink loop rather than raising.
    """
    return prefix.resolve() == Path(sys.prefix).resolve()


def _inspect(prefix: Path, found_by: str) -> tuple[Interpreter | None, Rejection | None]:
    """Turn a candidate directory into an interpreter, or into a reason."""
    config = read_config(prefix)
    if config is None or "home" not in config:
        return None, Rejection(prefix, f"no {CONFIG}, so not a virtual environment")
    if _is_ours(prefix):
        return None, Rejection(
            prefix,
            "this is djaudit's own environment; running the target in it would "
            "audit our Django rather than theirs",
        )
    executable = executable_in(prefix)
    if executable is None:
        return None, Rejection(prefix, f"has {CONFIG} but no runnable interpreter in it")
    return (
        Interpreter(
            executable=executable,
            prefix=prefix,
            creator=creator_of(config),
            version=config.get("version") or config.get("version_info"),
            found_by=found_by,
        ),
        None,
    )


def _within(child: Path, parent: Path) -> bool:
    """Whether `child` is `parent` or sits underneath it.

    Only `ValueError` is caught, which is what `relative_to` raises when it is
    not. `resolve` does not raise here for the reason given in `_is_ours`.
    """
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def find_interpreter(root: Path, environ: Mapping[str, str] | None = None) -> Search:
    """Search `root` for the interpreter its code is meant to run under.

    Nothing is executed and nothing is imported: this reads directory entries
    and one text file per candidate, so it is safe against a repository nobody
    has vetted.
    """
    environ = os.environ if environ is None else environ
    rejected: list[Rejection] = []
    # `$VIRTUAL_ENV` routinely names a directory already tried by name, and
    # reporting one environment twice reads as two separate problems.
    seen: set[Path] = set()

    def remember(rejection: Rejection) -> None:
        key = rejection.path.resolve()
        if key not in seen:
            seen.add(key)
            rejected.append(rejection)

    for name in CANDIDATE_DIRECTORIES:
        prefix = root / name
        if not prefix.is_dir():
            continue
        interpreter, rejection = _inspect(prefix, f"{name}/ in the target")
        if interpreter is not None:
            return Search(interpreter, tuple(rejected))
        if rejection is not None:
            remember(rejection)

    # Only trusted when it points into the target. Outside it, it is almost
    # always ours -- djaudit is normally run from a virtualenv of its own.
    active = environ.get("VIRTUAL_ENV")
    if active:
        prefix = Path(active)
        if not _within(prefix, root):
            remember(
                Rejection(
                    prefix,
                    "$VIRTUAL_ENV points outside the target, so it is some "
                    "other project's environment and probably djaudit's",
                )
            )
        else:
            interpreter, rejection = _inspect(prefix, "$VIRTUAL_ENV, inside the target")
            if interpreter is not None:
                return Search(interpreter, tuple(rejected))
            if rejection is not None:
                remember(rejection)

    deferred = tuple(reason for marker, reason in DEFERRED.items() if (root / marker).exists())
    return Search(None, tuple(rejected), deferred)
