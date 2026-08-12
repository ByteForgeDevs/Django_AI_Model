"""What a project can ask for once, in `[tool.djaudit]`, instead of every run.

Three things about this file are decisions rather than mechanics.

**A flag left unset is not a flag set to its default.** ``--min-severity``
defaults to ``low``, so a run that never mentions it and a run that says
``--min-severity low`` arrive at the command identically. If configuration were
merged by asking "is this value still the default?", every setting in the file
would lose to a default nobody typed, and it would lose silently. Click records
where each value came from, and ``chosen`` below asks it.

**An unknown key is an error.** A misspelled ``min_severty`` that is quietly
ignored produces a run with the wrong thresholds and no indication why -- and
for a tool whose output people gate merges on, that is worse than refusing to
start.

**The file cannot turn on anything that executes the target.** This config is
read out of the repository being audited, so it is exactly as trustworthy as
that repository. The static tier's promise is that it never imports or executes
what it is pointed at, and the live tier is opt-in; a file inside the target
that could set ``live = true`` would move that opt-in from the operator to the
author of the code under audit. ``live`` and ``external`` may therefore be
switched off here but never on, and asking for them says so.
"""

from __future__ import annotations

import difflib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Confidence, Family, Severity
from .reporters import OutputFormat


class ConfigError(Exception):
    """A configuration this tool will not run with."""


# Keys that name a tier which runs somebody else's code. They may appear, so a
# project can pin them off, but only with the value that reduces what runs.
EXECUTING = {"live": "--live", "external": "--external"}

# `write_baseline` is deliberately absent. It records every finding and exits
# zero; as a persistent setting it would turn every audit into a no-op that
# passes, which is the one failure this tool must never have.
KNOWN = (
    "min_severity",
    "min_confidence",
    "fail_on",
    "family",
    "select",
    "ignore",
    "exclude_paths",
    "baseline",
    "live",
    "external",
    "format",
    "output",
)

# `[tool.djaudit.llm]` is read by djaudit.llm.config, not here.
SUBTABLES = ("llm",)


@dataclass(frozen=True, slots=True)
class FileConfig:
    """Settings a project stated. ``None`` means it did not state one.

    Every field is optional and defaults to ``None`` rather than to the CLI's
    default, because "absent" has to survive as far as the merge. A field
    holding ``Severity.LOW`` would be indistinguishable from a project that
    asked for ``low``, and the merge would then have no way to let a command
    line win.
    """

    min_severity: Severity | None = None
    min_confidence: Confidence | None = None
    fail_on: Severity | None = None
    family: tuple[Family, ...] | None = None
    select: tuple[str, ...] | None = None
    ignore: tuple[str, ...] | None = None
    exclude_paths: tuple[str, ...] | None = None
    baseline: Path | None = None
    live: bool | None = None
    external: bool | None = None
    format: OutputFormat | None = None
    output: Path | None = None

    @property
    def empty(self) -> bool:
        return all(getattr(self, name) is None for name in KNOWN)


def _suggest(key: str) -> str:
    close = difflib.get_close_matches(key, [*KNOWN, *SUBTABLES], n=1, cutoff=0.6)
    return f"; did you mean {close[0]!r}?" if close else ""


def _enum(value: Any, kind: type[Severity] | type[Confidence], key: str, where: Path) -> Any:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: [tool.djaudit] {key} must be a string, not {_name(value)}")
    try:
        return kind(value.lower())
    except ValueError:
        allowed = ", ".join(repr(member.value) for member in kind)
        raise ConfigError(
            f"{where}: [tool.djaudit] {key} = {value!r} is not one of {allowed}"
        ) from None


def _strings(value: Any, key: str, where: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(
            f"{where}: [tool.djaudit] {key} must be a list of strings, not {_name(value)}"
        )
    return tuple(value)


def _bool(value: Any, key: str, where: Path) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            f"{where}: [tool.djaudit] {key} must be true or false, not {_name(value)}"
        )
    return value


def _path(value: Any, key: str, where: Path) -> Path:
    if not isinstance(value, str):
        raise ConfigError(
            f"{where}: [tool.djaudit] {key} must be a string path, not {_name(value)}"
        )
    return Path(value)


def _name(value: Any) -> str:
    """Describe a wrong value by what it is, so the fix is obvious."""
    return f"{value!r} ({type(value).__name__})"


def _check_keys(table: dict[str, Any], where: Path) -> None:
    for key, value in table.items():
        if key in SUBTABLES:
            continue
        if key in KNOWN:
            continue
        if key.replace("-", "_") in KNOWN:
            raise ConfigError(
                f"{where}: [tool.djaudit] has {key!r}; the key is spelled {key.replace('-', '_')!r}"
            )
        if isinstance(value, dict):
            raise ConfigError(f"{where}: [tool.djaudit.{key}] is not a table djaudit reads")
        raise ConfigError(f"{where}: [tool.djaudit] has no key {key!r}{_suggest(key)}")


def _check_executing(table: dict[str, Any], where: Path) -> None:
    for key, flag in EXECUTING.items():
        if table.get(key) is True:
            raise ConfigError(
                f"{where}: [tool.djaudit] {key} = true is refused. This file is read "
                f"out of the project being audited, so honouring it would let that "
                f"project decide to have its own code run. Pass {flag} on the command "
                f"line, where the person deciding is the person running djaudit."
            )


def from_pyproject(path: Path) -> FileConfig:
    """Read ``[tool.djaudit]``, or return the empty config.

    A missing file, a missing table and an empty table are the same answer:
    nothing was asked for. Only a malformed or dishonest one is an error.
    """
    if not path.is_file():
        return FileConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc

    tool = data.get("tool")
    table = tool.get("djaudit") if isinstance(tool, dict) else None
    if not isinstance(table, dict) or not table:
        return FileConfig()

    _check_keys(table, path)
    _check_executing(table, path)

    return FileConfig(
        min_severity=_read(table, "min_severity", lambda v, k: _enum(v, Severity, k, path)),
        min_confidence=_read(table, "min_confidence", lambda v, k: _enum(v, Confidence, k, path)),
        fail_on=_read(table, "fail_on", lambda v, k: _enum(v, Severity, k, path)),
        family=_read(table, "family", lambda v, k: _families(v, k, path)),
        select=_read(table, "select", lambda v, k: _rules(v, k, path)),
        ignore=_read(table, "ignore", lambda v, k: _rules(v, k, path)),
        exclude_paths=_read(table, "exclude_paths", lambda v, k: _strings(v, k, path)),
        baseline=_read(table, "baseline", lambda v, k: _path(v, k, path)),
        live=_read(table, "live", lambda v, k: _bool(v, k, path)),
        external=_read(table, "external", lambda v, k: _bool(v, k, path)),
        format=_read(table, "format", lambda v, k: _format(v, k, path)),
        output=_read(table, "output", lambda v, k: _path(v, k, path)),
    )


def _read(table: dict[str, Any], key: str, parse: Any) -> Any:
    """Absent stays absent. Only a stated key is parsed."""
    return parse(table[key], key) if key in table else None


def _families(value: Any, key: str, where: Path) -> tuple[Family, ...]:
    found = []
    for item in _strings(value, key, where):
        try:
            found.append(Family(item.upper()))
        except ValueError:
            allowed = ", ".join(repr(member.value) for member in Family)
            raise ConfigError(
                f"{where}: [tool.djaudit] {key} lists {item!r}, which is not one of {allowed}"
            ) from None
    return tuple(found)


def _rules(value: Any, key: str, where: Path) -> tuple[str, ...]:
    """Rule ids, upper-cased to match how the CLI normalises them."""
    return tuple(item.upper() for item in _strings(value, key, where))


def _format(value: Any, key: str, where: Path) -> OutputFormat:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: [tool.djaudit] {key} must be a string, not {_name(value)}")
    try:
        return OutputFormat(value.lower())
    except ValueError:
        allowed = ", ".join(repr(member.value) for member in OutputFormat)
        raise ConfigError(
            f"{where}: [tool.djaudit] {key} = {value!r} is not one of {allowed}"
        ) from None
