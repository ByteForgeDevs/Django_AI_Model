"""Shared machinery for rules about the API surface.

Every `DJA` rule asks a question of the same three things: what a router
routes, what guards it, and what rows it can reach. Resolving those per rule
would walk the class index seven times and let seven copies of "which settings
module decides the DRF defaults" drift apart, so the loop lives here and a rule
supplies only its judgement.

The one policy decision worth stating is which settings module supplies
``DEFAULT_PERMISSION_CLASSES``. A project with a permissive `dev.py` and a
locked-down `production.py` has two different answers, and a security rule
wants the one that ships -- so the most production-like module wins, and every
finding names it rather than leaving the reader to guess which file was read.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from djaudit.api.permissions import Defaults, Guard, build_guards, read_defaults
from djaudit.context import ProjectContext, SettingsRole
from djaudit.models import Confidence, Evidence, EvidenceKind, Finding, Location
from djaudit.registry import Rule
from djaudit.settings import SettingsView, resolve_all

if TYPE_CHECKING:
    from djaudit.api.discovery import ApiSurface
    from djaudit.api.querysets import QuerysetNode
    from djaudit.api.views import ViewNode

_ROLE_WEIGHT: dict[SettingsRole, int] = {
    SettingsRole.PRODUCTION: 4,
    SettingsRole.PRIMARY: 3,
    SettingsRole.UNKNOWN: 2,
    SettingsRole.BASE: 1,
    SettingsRole.DEVELOPMENT: 0,
    SettingsRole.TEST: -1,
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One routed view, with everything a rule needs to judge it."""

    view: ViewNode
    guard: Guard
    queryset: QuerysetNode | None
    methods: frozenset[str]
    """HTTP methods a router actually binds, uppercased."""

    collection_methods: frozenset[str] = frozenset()
    """Methods a router bound to a collection URL rather than a detail one."""

    @property
    def label(self) -> str:
        return self.view.label

    @property
    def writes(self) -> bool:
        return bool(self.methods & {"POST", "PUT", "PATCH", "DELETE"})

    @property
    def lists(self) -> bool:
        """Whether a GET returns a collection rather than one row.

        The difference between an unscoped queryset that leaks the one row
        whose id the caller guessed and one that hands over the table.

        A router says so outright, which is why the route graph is asked
        first. A plain ``path()`` entry does not -- NetBox routes a
        single-object ``DashboardView`` at ``dashboard/`` -- so for those the
        view has to claim the ``list`` action itself.
        """
        if "GET" in self.collection_methods:
            return True
        return "list" in self.view.actions and "GET" in self.methods


class ApiRule(Rule):
    """A rule about routed endpoints rather than about settings."""

    ceiling: Confidence = Confidence.FIRM
    """Static reading cannot see a request. Almost nothing here is CERTAIN,
    and a rule claiming otherwise should say why in its own declaration."""

    surface: ApiSurface
    defaults: Defaults
    settings_module: str | None
    settings_view: SettingsView | None

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for endpoint in self.endpoints(ctx):
            yield from self.inspect(ctx, endpoint)

    def endpoints(self, ctx: ProjectContext) -> Iterator[Endpoint]:
        """Every routed view, resolved once.

        Separated from :meth:`check` so a rule that also has something to say
        about the project as a whole can count the endpoints its answer
        applies to without running its own judgement over them twice.
        """
        self.surface = ctx.api_surface
        self.settings_view = production_settings(ctx)
        self.defaults = read_defaults(self.settings_view)
        self.settings_module = (
            self.settings_view.module.dotted if self.settings_view is not None else None
        )
        index = self.surface.index
        if not self.surface.views or index is None:
            return
        guards = build_guards(self.surface, index, self.defaults)
        for label, guard in sorted(guards.views.items()):
            node = self.surface.views.get(label)
            if node is None:
                continue
            yield Endpoint(
                view=node,
                guard=guard,
                queryset=self.surface.querysets.get(label),
                methods=frozenset(m.upper() for m in self.surface.routes.methods_for(label)),
                collection_methods=frozenset(
                    e.method.upper()
                    for e in self.surface.routes.for_view(label)
                    if e.kind == "list"
                ),
            )

    @abstractmethod
    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        """Judge one routed endpoint."""
        raise NotImplementedError

    def at(self, ctx: ProjectContext, view: ViewNode, line: int | None = None) -> Location:
        return Location(
            file=ctx.rel(view.path),
            line=line or view.lineno,
            snippet=ctx.snippet(view.path, line or view.lineno),
        )

    def guard_evidence(self, guard: Guard) -> Evidence:
        """Where the permission answer came from, in the reader's terms.

        Written out because "no permission classes" is the kind of claim a
        reader will not believe about their own code, and the useful reply is
        the chain that produced it rather than the verdict.
        """
        declared = guard.declared_by or "nowhere"
        classes = ", ".join(guard.classes) if guard.classes else "(empty)"
        return Evidence(
            kind=EvidenceKind.AST,
            content=f"permission_classes = [{classes}] via {guard.source.value} in {declared}",
            source="djaudit API surface",
        )


def production_settings(ctx: ProjectContext) -> SettingsView | None:
    """The settings module whose configuration ships.

    An entrypoint module named by ``DJANGO_SETTINGS_MODULE`` wins outright,
    because the project has told us which one it runs. Otherwise the most
    production-like role wins, and a tie goes to the longer chain -- a module
    that imports a base and overrides it is a later word than the base.
    """
    views = resolve_all(ctx)
    if not views:
        return None
    return max(
        views.values(),
        key=lambda view: (
            view.module.is_entrypoint,
            _ROLE_WEIGHT.get(view.module.role, 0),
            len(view.chain),
        ),
    )
