"""The analysis context handed to every rule."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from djaudit.models import Location

MAX_SNIPPET_LENGTH = 240


class SettingsRole(StrEnum):
    """What a settings module is for.

    Role drives severity. ``DEBUG = True`` in ``settings/development.py`` is
    correct and must never be reported; the same line in ``settings/production.py``
    is a critical finding. Tools that ignore this distinction get uninstalled.
    """

    PRIMARY = "primary"
    """A lone ``settings.py``. Serves production unless something overrides it."""

    BASE = "base"
    """Shared base imported by the environment-specific modules."""

    PRODUCTION = "production"
    DEVELOPMENT = "development"
    TEST = "test"
    UNKNOWN = "unknown"

    @property
    def reaches_production(self) -> bool:
        return self not in (SettingsRole.DEVELOPMENT, SettingsRole.TEST)


@dataclass(frozen=True, slots=True)
class SettingsModule:
    """A discovered Django settings module."""

    path: Path
    dotted: str
    role: SettingsRole
    is_entrypoint: bool = False
    """True when ``DJANGO_SETTINGS_MODULE`` points here."""


@dataclass
class ProjectContext:
    """Everything a static-tier rule needs, with parsing cached across rules.

    Rules receive one of these and must not perform their own filesystem walks:
    the cache is what keeps a full run over a large project (NetBox is ~20 apps)
    from re-parsing the same modules once per rule.
    """

    root: Path
    python_files: tuple[Path, ...] = ()
    manage_py: Path | None = None
    settings_entrypoint: str | None = None
    settings_modules: tuple[SettingsModule, ...] = ()
    django_version: str | None = None
    live: bool = False
    """Whether the target's virtualenv is available for live-tier rules."""

    _trees: dict[Path, ast.Module | None] = field(default_factory=dict, repr=False)
    _lines: dict[Path, list[str]] = field(default_factory=dict, repr=False)
    parse_errors: dict[Path, str] = field(default_factory=dict, repr=False)

    def rel(self, path: Path) -> str:
        """POSIX path relative to the project root, so findings stay portable."""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def parse(self, path: Path) -> ast.Module | None:
        """Parse a file, caching the tree. Returns ``None`` on syntax errors.

        A file we cannot parse is recorded in :attr:`parse_errors` and skipped.
        One unparseable module must never abort a whole run.
        """
        if path in self._trees:
            return self._trees[path]
        tree: ast.Module | None
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError) as exc:
            self.parse_errors[path] = str(exc)
            tree = None
        self._trees[path] = tree
        return tree

    def lines(self, path: Path) -> list[str]:
        if path not in self._lines:
            try:
                self._lines[path] = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                self._lines[path] = []
        return self._lines[path]

    def snippet(self, path: Path, line: int, end_line: int | None = None) -> str:
        """Source text for a 1-based line range, trimmed and length-capped."""
        source = self.lines(path)
        if not source or line < 1 or line > len(source):
            return ""
        last = min(end_line or line, len(source))
        text = "\n".join(source[line - 1 : last]).strip()
        if len(text) > MAX_SNIPPET_LENGTH:
            text = text[:MAX_SNIPPET_LENGTH].rstrip() + " ..."
        return text

    def location(self, path: Path, node: ast.AST) -> Location:
        """Build a :class:`Location` from an AST node."""
        line = getattr(node, "lineno", 1)
        end_line = getattr(node, "end_lineno", None)
        return Location(
            file=self.rel(path),
            line=line,
            column=getattr(node, "col_offset", 0) + 1,
            end_line=end_line,
            end_column=(
                col + 1 if (col := getattr(node, "end_col_offset", None)) is not None else None
            ),
            snippet=self.snippet(path, line, end_line),
        )
