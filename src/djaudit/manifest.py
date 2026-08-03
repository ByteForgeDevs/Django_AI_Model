"""Finding and reading the project's declared dependencies.

Everything else in this phase reads Python. This reads the files that decide
what is *on the machine*, which turns out to be a different question with a
different answer: a package can be absent from ``INSTALLED_APPS`` and still be
installed, and then the only thing keeping it from running is a setting.

The whole difficulty is telling a production manifest from a development one.
Get that wrong in the permissive direction and the rules built on this are
silent on real deployments; get it wrong in the strict direction and every
project that has correctly separated its tooling gets reported for doing so.
So the classification is deliberately conservative: a file is development only
when it says so in its own name or lives under a table that says so.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

MAX_DEPTH = 3
"""How far below the project root to look.

Deep enough for ``requirements/production.txt`` and for a project whose Django
tree sits one directory down; shallow enough not to walk a vendored virtualenv
that slipped past the directory skip list.
"""

SKIP_DIRS = frozenset(
    {
        ".bzr",
        ".direnv",
        ".eggs",
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "site-packages",
        "venv",
    }
)

DEV_TOKENS = frozenset(
    {
        "bench",
        "benchmark",
        "check",
        "checks",
        "ci",
        "debug",
        "dev",
        "develop",
        "development",
        "doc",
        "docs",
        "lint",
        "linting",
        "local",
        "mypy",
        "qa",
        "style",
        "test",
        "testing",
        "tests",
        "tooling",
        "type",
        "types",
        "typing",
    }
)
"""Words that mark a requirement set as not deployed.

Matched as whole words against the parts of a filename or the name of a
dependency group, so ``requirements-dev.txt``, ``dev-requirements.txt`` and
``requirements/dev.txt`` are all development while ``base_requirements.txt``
and ``requirements/production.txt`` are not.
"""

_WORDS = re.compile(r"[^A-Za-z0-9]+")
_COMMENT = re.compile(r"(?:^|\s)#.*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_NORMALISE = re.compile(r"[-_.]+")
_REQUIREMENT_FILE = re.compile(r"^.*requirements?.*\.(?:txt|in)$|^.*\.(?:txt|in)$")


def normalise(name: str) -> str:
    """A distribution name in the one spelling PEP 503 says to compare."""
    return _NORMALISE.sub("-", name).lower()


def is_development(label: str) -> bool:
    """Whether a filename stem or group name marks its contents as undeployed."""
    return any(word in DEV_TOKENS for word in _WORDS.split(label.lower()) if word)


@dataclass(frozen=True, slots=True)
class Requirement:
    """One declared dependency, and where it was declared."""

    name: str
    """Normalised per PEP 503, so ``Django_Debug_Toolbar`` matches."""

    raw: str
    line: int


@dataclass(frozen=True, slots=True)
class Manifest:
    """One set of dependencies declared in one place."""

    path: Path
    kind: str
    """``requirements``, ``pyproject`` or ``pipfile``."""

    group: str
    """The extra or dependency group, empty for the main set."""

    development: bool
    requirements: tuple[Requirement, ...]

    @property
    def label(self) -> str:
        """How to name this manifest in a message."""
        return f"{self.path.name} [{self.group}]" if self.group else self.path.name

    def find(self, name: str) -> Requirement | None:
        return next((req for req in self.requirements if req.name == name), None)


def requirement_name(line: str) -> str | None:
    """The distribution a requirement line installs, if it names one.

    Handles the shapes that actually occur: extras (``Django[argon2]==6.0.7``),
    environment markers, direct references (``pkg @ git+https://...``), and the
    ``-r``/``--index-url`` option lines, which name no distribution at all.
    """
    text = _COMMENT.sub("", line).strip()
    if not text or text.startswith("-"):
        return None
    text = text.split(";", 1)[0].strip()
    match = _NAME.match(text)
    if match is None:
        return None
    # A bare URL starts with a scheme that looks like a name until the colon.
    if ":" in text[: match.end()] or text[match.end() : match.end() + 3] == "://":
        return None
    return normalise(match.group())


def read_requirements(path: Path) -> tuple[Requirement, ...]:
    """Parse a pip requirements file, keeping each line's position."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ()

    found: list[Requirement] = []
    buffer, start = "", 0
    for number, line in enumerate(lines, start=1):
        if not buffer:
            start = number
        buffer += line
        # A trailing backslash continues the requirement onto the next line.
        if buffer.endswith("\\"):
            buffer = buffer[:-1]
            continue
        name = requirement_name(buffer)
        if name is not None:
            found.append(Requirement(name=name, raw=buffer.strip(), line=start))
        buffer = ""
    return tuple(found)


def locate(lines: list[str], needle: str) -> int:
    """The first line mentioning ``needle``, or the top of the file.

    ``tomllib`` discards positions, and a finding that cannot point at a line is
    much harder to act on than one that points at approximately the right one.
    """
    for number, line in enumerate(lines, start=1):
        if needle in line:
            return number
    return 1


def _from_toml_entries(entries: Iterable[object], lines: list[str]) -> tuple[Requirement, ...]:
    """Requirements from a PEP 508 string array."""
    found = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        name = requirement_name(entry)
        if name is not None:
            found.append(Requirement(name=name, raw=entry, line=locate(lines, entry)))
    return tuple(found)


def _from_toml_table(table: dict[str, object], lines: list[str]) -> tuple[Requirement, ...]:
    """Requirements from a ``name = spec`` table, as Poetry and Pipenv write."""
    return tuple(
        Requirement(name=normalise(key), raw=key, line=locate(lines, key))
        for key in table
        if key.lower() != "python"
    )


def read_pyproject(path: Path) -> Iterator[Manifest]:
    """Every dependency set declared in a ``pyproject.toml``.

    Covers PEP 621 dependencies and optional-dependencies, PEP 735 dependency
    groups, and Poetry's own tables, because a rule that only understood one of
    them would be silent on most of the projects using the others.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return

    def make(group: str, requirements: tuple[Requirement, ...], *, dev: bool) -> Manifest:
        return Manifest(
            path=path,
            kind="pyproject",
            group=group,
            development=dev,
            requirements=requirements,
        )

    project = data.get("project")
    if isinstance(project, dict):
        deps = project.get("dependencies")
        if isinstance(deps, list):
            yield make("", _from_toml_entries(deps, lines), dev=False)
        extras = project.get("optional-dependencies")
        if isinstance(extras, dict):
            for name, entries in extras.items():
                if isinstance(entries, list):
                    yield make(name, _from_toml_entries(entries, lines), dev=is_development(name))

    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for name, entries in groups.items():
            if isinstance(entries, list):
                yield make(name, _from_toml_entries(entries, lines), dev=is_development(name))

    poetry = data.get("tool", {})
    poetry = poetry.get("poetry", {}) if isinstance(poetry, dict) else {}
    if not isinstance(poetry, dict):
        return
    main = poetry.get("dependencies")
    if isinstance(main, dict):
        yield make("", _from_toml_table(main, lines), dev=False)
    legacy = poetry.get("dev-dependencies")
    if isinstance(legacy, dict):
        yield make("dev", _from_toml_table(legacy, lines), dev=True)
    for name, group in (poetry.get("group") or {}).items():
        table = group.get("dependencies") if isinstance(group, dict) else None
        if isinstance(table, dict):
            yield make(name, _from_toml_table(table, lines), dev=is_development(name))


def read_pipfile(path: Path) -> Iterator[Manifest]:
    """The two dependency sets a ``Pipfile`` can declare."""
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return
    lines = text.splitlines()
    for table, group, dev in (("packages", "", False), ("dev-packages", "dev-packages", True)):
        entries = data.get(table)
        if isinstance(entries, dict):
            yield Manifest(
                path=path,
                kind="pipfile",
                group=group,
                development=dev,
                requirements=_from_toml_table(entries, lines),
            )


def candidates(root: Path) -> Iterator[Path]:
    """Manifest files under ``root``, breadth-first and depth-limited."""
    frontier = [(root, 0)]
    while frontier:
        directory, depth = frontier.pop(0)
        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if depth < MAX_DEPTH and child.name not in SKIP_DIRS:
                    frontier.append((child, depth + 1))
            elif child.name in {"pyproject.toml", "Pipfile"} or (
                child.suffix in {".txt", ".in"} and "requirements" in child.stem.lower()
            ):
                yield child


def discover(root: Path) -> tuple[Manifest, ...]:
    """Every dependency set the project declares.

    A requirements file is classified by its own name; a TOML table by the name
    of the table it sits in. Nothing infers development status from contents,
    because that would be the rule reasoning in a circle.
    """
    found: list[Manifest] = []
    for path in candidates(root):
        if path.name == "pyproject.toml":
            found.extend(read_pyproject(path))
        elif path.name == "Pipfile":
            found.extend(read_pipfile(path))
        else:
            found.append(
                Manifest(
                    path=path,
                    kind="requirements",
                    group="",
                    development=is_development(path.stem),
                    requirements=read_requirements(path),
                )
            )
    return tuple(found)


def deployed(manifests: Iterable[Manifest], name: str) -> tuple[Manifest, Requirement] | None:
    """Where a distribution is declared for production, if anywhere.

    Returns one production manifest rather than all of them, so a project that
    lists a package in both its ``.in`` source and its compiled ``.txt`` is
    reported once. Compiled files are considered last, because the fix belongs
    in the file a human edits -- editing the output is how a dependency comes
    back on the next ``pip-compile``.
    """
    wanted = normalise(name)
    ordered = sorted(manifests, key=lambda m: m.path.suffix == ".txt")
    for manifest in ordered:
        if manifest.development:
            continue
        requirement = manifest.find(wanted)
        if requirement is not None:
            return manifest, requirement
    return None
