"""The analysis context handed to every rule."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from djaudit.models import Location

if TYPE_CHECKING:
    from djaudit.api.discovery import ApiSurface
    from djaudit.graph.nodes import ModelGraph

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


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Something about the *analysis* that the user must be told.

    Distinct from a finding, and deliberately not one. A finding says the
    project has a problem; a diagnostic says djaudit could not see enough of the
    project to judge. Conflating them would let a blind spot be silenced with a
    baseline entry or a ``# djaudit: ignore`` comment, which is precisely the
    outcome to avoid: the tool would then report a confident, permanent, and
    entirely uninformed all-clear.
    """

    code: str
    message: str
    """One line, stating what could not be analysed."""

    detail: str
    """Why it happened and what the user can do about it."""

    blocking: bool = True
    """Whether the run should exit non-zero because its coverage is incomplete."""


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
    diagnostics: tuple[Diagnostic, ...] = ()
    """Gaps in what could be analysed. Reported before findings, never as one."""

    live: bool = False
    """Whether the target's virtualenv is available for live-tier rules."""

    _trees: dict[Path, ast.Module | None] = field(default_factory=dict, repr=False)
    _lines: dict[Path, list[str]] = field(default_factory=dict, repr=False)
    _model_graph: ModelGraph | None = field(default=None, repr=False)
    _api_surface: ApiSurface | None = field(default=None, repr=False)
    _modules: dict[str, Path] | None = field(default=None, repr=False)
    parse_errors: dict[Path, str] = field(default_factory=dict, repr=False)

    @property
    def model_graph(self) -> ModelGraph:
        """The project's models, reconstructed once and shared by every rule.

        Built lazily because most settings rules never look at a model, and
        walking every app's ``models`` module to answer a question nobody asked
        would make the cheap half of a run as slow as the expensive half.
        """
        if self._model_graph is None:
            # Imported here because the graph reads AUTH_USER_MODEL through the
            # settings resolver, which needs this class. The cycle is real and
            # deferring the import is the fix, not a workaround for one.
            from djaudit.graph.builder import build_model_graph  # noqa: PLC0415

            self._model_graph = build_model_graph(self)
        return self._model_graph

    @property
    def api_surface(self) -> ApiSurface:
        """Serializers, views, routes and querysets, resolved once per run.

        Lazy for the same reason the model graph is: a project with no DRF pays
        for one pass over the class index and nothing more, and a run that asks
        only about settings never triggers it at all.
        """
        if self._api_surface is None:
            from djaudit.api import build_api_surface  # noqa: PLC0415  (cycle)

            self._api_surface = build_api_surface(self, self.model_graph)
        return self._api_surface

    def module_path(self, dotted: str) -> Path | None:
        """The file a dotted module name refers to, or ``None`` if it is not ours.

        The inverse of :func:`djaudit.discovery.dotted_path`, built once and
        shared. ``None`` is the ordinary answer for anything installed from a
        dependency: this is a static tool and does not read site-packages, so
        "not in this project" and "does not exist" are the same answer here.
        """
        if self._modules is None:
            # Imported here rather than at module scope because discovery
            # constructs this class, so the cycle is real.
            from djaudit.discovery import dotted_path  # noqa: PLC0415

            self._modules = {dotted_path(self.root, path): path for path in self.python_files}
        exact = self._modules.get(dotted)
        if exact is not None:
            return exact
        # A src/ layout names modules from a directory that is not the
        # repository root, so pretix's "pretix.multidomain.middlewares" is
        # indexed here as "src.pretix.multidomain.middlewares" and an exact
        # lookup misses every module in the project. Falling back to a suffix
        # match fixes that for any such layout without needing to guess where
        # the import root is -- and only when exactly one module can be meant,
        # because two apps ending in ".models" must never resolve to whichever
        # was walked first.
        suffix = f".{dotted}"
        matches = [path for name, path in self._modules.items() if name.endswith(suffix)]
        return matches[0] if len(matches) == 1 else None

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
