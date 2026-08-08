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
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.inventory import LoopSite
    from djaudit.dataflow.querysets import QuerysetValue
    from djaudit.dataflow.scopes import Scope
    from djaudit.graph.nodes import ModelGraph
    from djaudit.live.context import LiveContext
    from djaudit.migrations.graph import MigrationGraph
    from djaudit.migrations.state import Applied

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

    live_context: LiveContext | None = None
    """What the target's own Django reported, when the live tier ran.

    Carried rather than flattened into the fields above because a live rule
    needs the *interpreter* to run anything, and re-deriving it would let a
    rule execute an environment other than the one that was disclosed and
    consented to.
    """

    live_problem: str | None = None
    """Why the live tier is unavailable, when it was asked for and did not start.

    "Unavailable" is not an action. "no virtualenv was found in the target" and
    "the target's Django did not start: ImproperlyConfigured" send the reader to
    two different places, so the reason travels with the flag.
    """

    _trees: dict[Path, ast.Module | None] = field(default_factory=dict, repr=False)
    _source: dict[Path, str | None] = field(default_factory=dict, repr=False)
    _lines: dict[Path, list[str]] = field(default_factory=dict, repr=False)
    _model_graph: ModelGraph | None = field(default=None, repr=False)
    _api_surface: ApiSurface | None = field(default=None, repr=False)
    _loops: tuple[LoopSite, ...] | None = field(default=None, repr=False)
    _modules: dict[str, Path] | None = field(default=None, repr=False)
    _migration_graph: MigrationGraph | None = field(default=None, repr=False)
    _migration_history: tuple[Applied, ...] | None = field(default=None, repr=False)
    _scopes: dict[Path, Scope | None] = field(default_factory=dict, repr=False)
    _def_use: dict[int, DefUse] = field(default_factory=dict, repr=False)
    _tracked: dict[int, dict[int, QuerysetValue]] = field(default_factory=dict, repr=False)
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

    @property
    def migration_graph(self) -> MigrationGraph:
        """Every migration in the project, parsed and linked once per run.

        Lazy like the model graph, and for a sharper reason: parsing 875
        migrations is wasted work for the many runs that never ask a `DJM`
        question, and migrations are the one input a project can have thousands
        of without anybody noticing.
        """
        if self._migration_graph is None:
            from djaudit.migrations.graph import build_migration_graph  # noqa: PLC0415  (cycle)

            self._migration_graph = build_migration_graph(self)
        return self._migration_graph

    @property
    def migration_history(self) -> tuple[Applied, ...]:
        """Each operation in dependency order, with the state it acted against.

        Separate from `migration_graph` because replay costs materially more
        than parsing, and a rule that only wants to know which migrations exist
        should not pay for the state machine.
        """
        if self._migration_history is None:
            from djaudit.migrations.state import replay  # noqa: PLC0415  (cycle)

            self._migration_history = tuple(replay(self.migration_graph))
        return self._migration_history

    @property
    def loops(self) -> tuple[LoopSite, ...]:
        """Every loop in the project, resolved once and shared by all `DJP` rules.

        Lazy for the same reason the model graph is, and more so: a run that
        asks only about settings never builds a def-use chain at all.
        """
        if self._loops is None:
            from djaudit.dataflow.inventory import build_loop_inventory  # noqa: PLC0415

            self._loops = build_loop_inventory(self)
        return self._loops

    def scopes(self, path: Path) -> Scope | None:
        """The lexical scope tree for ``path``, built once per run.

        Three separate consumers need it -- the loop inventory, `DJP-003` and
        `DJP-005` -- and each was rebuilding it. The trees are read-only once
        built, so one copy serves them all, and the cache keeps a
        whole-repository run from paying for the same walk three times.
        """
        if path not in self._scopes:
            from djaudit.dataflow.scopes import build_scopes  # noqa: PLC0415  (cycle)

            tree = self.parse(path)
            self._scopes[path] = build_scopes(tree) if tree is not None else None
        return self._scopes[path]

    def def_use(self, scope: Scope) -> DefUse:
        """Def-use chains for one scope, built once per run.

        Keyed on ``id(scope.node)``, which is stable because :meth:`parse` and
        :meth:`scopes` both hold their results for the lifetime of the run, so
        no tree an id refers to can be collected and its address reused.
        """
        key = id(scope.node)
        if key not in self._def_use:
            from djaudit.dataflow.chains import def_use  # noqa: PLC0415  (cycle)

            self._def_use[key] = def_use(scope)
        return self._def_use[key]

    def tracked(self, path: Path, scope: Scope) -> dict[int, QuerysetValue]:
        """Queryset-valued expressions in one scope, resolved once per run.

        Four consumers wanted this for overlapping scopes -- the loop
        inventory, `DJP-002`, `DJP-003` and `DJP-005` -- and each was
        recomputing it. The answer depends only on the scope, the model graph
        and the app label, and the latter two are fixed for a given path, so
        one result serves them all.

        Returned by reference rather than copied. Callers read the mapping and
        none of them mutate it, and copying a dict per scope would give back
        much of what the cache is here to save.
        """
        key = id(scope.node)
        if key not in self._tracked:
            from djaudit.dataflow.querysets import track  # noqa: PLC0415  (cycle)
            from djaudit.graph.builder import app_label_for  # noqa: PLC0415  (cycle)

            self._tracked[key] = track(
                scope, self.def_use(scope), self.model_graph, app_label=app_label_for(path, self)
            )
        return self._tracked[key]

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

    def source(self, path: Path) -> str | None:
        """File text, read once and shared by parsing, snippets and pre-checks.

        Reading was previously done separately by :meth:`parse` and
        :meth:`lines`, so any rule that needed both paid for two reads of every
        file it touched. One cache also lets a caller ask a cheap textual
        question -- "could this file contain a loop at all" -- without paying
        for a parse to find out.
        """
        if path not in self._source:
            try:
                self._source[path] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.parse_errors[path] = str(exc)
                self._source[path] = None
        return self._source[path]

    def parse(self, path: Path) -> ast.Module | None:
        """Parse a file, caching the tree. Returns ``None`` on syntax errors.

        A file we cannot parse is recorded in :attr:`parse_errors` and skipped.
        One unparseable module must never abort a whole run.
        """
        if path in self._trees:
            return self._trees[path]
        tree: ast.Module | None = None
        source = self.source(path)
        if source is not None:
            try:
                tree = ast.parse(source, filename=str(path))
            except (SyntaxError, ValueError) as exc:
                self.parse_errors[path] = str(exc)
                tree = None
        self._trees[path] = tree
        return tree

    def lines(self, path: Path) -> list[str]:
        if path not in self._lines:
            source = self.source(path)
            self._lines[path] = source.splitlines() if source is not None else []
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
