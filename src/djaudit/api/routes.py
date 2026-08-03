"""Which HTTP methods actually reach a view.

2.2.2 stopped one step short of an answer. A ``ModelViewSet`` has six actions
and no HTTP methods at all; the methods appear only when a router binds it, and
the binding is what decides that ``destroy`` is reachable as ``DELETE`` on the
detail URL and nowhere else. Until that join exists, "can an anonymous caller
delete this" has no static answer.

Two spellings produce it. A router is given a viewset and generates the routes
itself, from the table in ``rest_framework/routers.py``:

* the **list** route maps ``get`` to ``list`` and ``post`` to ``create``;
* the **detail** route maps ``get``/``put``/``patch``/``delete`` to
  ``retrieve``/``update``/``partial_update``/``destroy``;
* one **dynamic** route per ``@action``, on whichever of the two the action's
  ``detail`` argument selects.

A urlconf, by contrast, names the view directly -- ``path("x/", View.as_view())``
-- and the methods are the view's own.

The important subtlety is DRF's ``get_method_map``: a mapping entry only
becomes a route if the viewset actually implements that action. That is why a
``ReadOnlyModelViewSet`` on a router answers GET and 405s everything else,
without anyone writing a restriction, and it is the difference between reading
the router table and reading what the router will do with it.

Prefixes are deliberately not reconstructed. A route's full URL depends on
every ``include()`` above it, which can be several files away and conditional
on the way; :mod:`djaudit.urlconf` declines to guess and so does this. What is
reported is the pattern as written, plus whether that is the whole of it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from djaudit.api.views import ViewNode
from djaudit.astutils import dotted_name, literal
from djaudit.graph.inheritance import ClassIndex, package_dotted
from djaudit.urlconf import ROUTERS, pattern_of

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from djaudit.api.discovery import ApiSurface
    from djaudit.context import ProjectContext

ROUTER_BASES = frozenset(
    {
        "rest_framework.routers.BaseRouter",
        "rest_framework.routers.SimpleRouter",
        "rest_framework.routers.DefaultRouter",
    }
)
"""DRF's routers. Projects subclass them -- NetBox routes everything through a
``NetBoxRouter(DefaultRouter)`` -- so membership is decided by ancestry, and a
registration on a class the index has never heard of is not treated as one."""

LIST_MAPPING: dict[str, str] = {"get": "list", "post": "create"}
DETAIL_MAPPING: dict[str, str] = {
    "get": "retrieve",
    "put": "update",
    "patch": "partial_update",
    "delete": "destroy",
}
"""``SimpleRouter.routes``, transcribed. The detail mapping is where every
destructive standard action lives, which is why ``destroy`` never appears on a
collection URL however the viewset is written."""

ROUTE_SLOTS: dict[int, bool] = {0: False, 2: True}
"""``SimpleRouter.routes`` is ``[list, dynamic-list, detail, dynamic-detail]``,
so index 0 is the list route and index 2 the detail one. Positional because
that is how a subclass reaches them: NetBox mutates ``self.routes[0].mapping``
to put bulk ``PUT``, ``PATCH`` and ``DELETE`` on every collection URL in the
product, and a reader that ignored it would call 139 writable endpoints
read-only."""

LIST_SLOT = 0
DETAIL_SLOT = 2


@dataclass(frozen=True, slots=True)
class Registration:
    """One ``router.register(prefix, viewset)`` call."""

    prefix: str
    literal: bool
    """Whether :attr:`prefix` is the whole pattern, per :func:`pattern_of`."""

    view_ref: str
    """The viewset argument as written -- ``views.SiteViewSet``."""

    view: str | None
    """The resolved view label, or ``None`` when it left the project."""

    basename: str | None
    router: str | None
    """The router class, when it could be resolved to one."""

    path: Path
    line: int


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One HTTP method reaching one view, and what runs when it does."""

    view: str
    method: str
    detail: bool | None
    """``True`` on a detail URL, ``False`` on a collection URL, ``None`` when
    the route is a plain urlconf entry, where the distinction does not exist."""

    action: str | None
    """The viewset action or ``@action`` method this method dispatches to."""

    pattern: str
    literal: bool
    kind: str
    """``list``, ``detail``, ``action`` or ``path``."""

    path: Path
    line: int

    @property
    def writes(self) -> bool:
        return self.method in {"post", "put", "patch", "delete"}


@dataclass
class RouteGraph:
    """Every reachable endpoint, and the views nothing reaches."""

    endpoints: list[Endpoint] = field(default_factory=list)
    registrations: list[Registration] = field(default_factory=list)

    unresolved_views: tuple[str, ...] = ()
    """Registrations naming a viewset outside the project. Kept visible: a
    growing count means the resolver is losing endpoints, not that the project
    has none."""

    def for_view(self, label: str) -> list[Endpoint]:
        return [e for e in self.endpoints if e.view == label]

    @property
    def writable(self) -> list[Endpoint]:
        return [e for e in self.endpoints if e.writes]

    def methods_for(self, label: str) -> tuple[str, ...]:
        return tuple(sorted({e.method for e in self.for_view(label)}))

    def routed(self) -> frozenset[str]:
        return frozenset(e.view for e in self.endpoints)

    def __len__(self) -> int:
        return len(self.endpoints)

    def __iter__(self) -> Iterator[Endpoint]:
        return iter(self.endpoints)


def _mapping_literal(node: ast.expr) -> dict[str, str]:
    value = literal(node)
    if not isinstance(value, dict):
        return {}
    return {
        str(k).lower(): str(v)
        for k, v in value.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def router_mappings(index: ClassIndex) -> dict[str, tuple[dict[str, str], dict[str, str]]]:
    """Each router class's list and detail mappings, defaults included.

    A subclass that leaves ``routes`` alone gets DRF's table. One that edits it
    gets the edit applied, which is not an exotic case: bulk operations are
    conventionally added exactly this way, by reaching into
    ``self.routes[0].mapping`` in ``__init__``, and they add the three methods
    that matter most to an authorization rule.
    """
    found: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
    for record in index.records():
        if not index.inherits(record, ROUTER_BASES):
            continue
        list_map = dict(LIST_MAPPING)
        detail_map = dict(DETAIL_MAPPING)
        for link in (record, *index.ancestry(record)):
            for slot, mapping in _mapping_edits(link.node).items():
                if slot == LIST_SLOT:
                    list_map.update(mapping)
                elif slot == DETAIL_SLOT:
                    detail_map.update(mapping)
        found[record.dotted] = (list_map, detail_map)
    return found


def _mapping_edits(node: ast.ClassDef) -> dict[int, dict[str, str]]:
    """``self.routes[N].mapping.update({...})``, wherever in the class it sits.

    Narrow on purpose. This recognises the one shape projects actually use and
    declines everything else, because a router that rebuilds ``routes`` from
    scratch is not something a static read can follow, and pretending otherwise
    would report method sets that no request would ever match.
    """
    edits: dict[int, dict[str, str]] = {}
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "update":
            continue
        mapping = func.value
        if not isinstance(mapping, ast.Attribute) or mapping.attr != "mapping":
            continue
        subscript = mapping.value
        if not isinstance(subscript, ast.Subscript):
            continue
        slot = literal(subscript.slice)
        if not isinstance(slot, int) or slot not in ROUTE_SLOTS:
            continue
        entries = _mapping_literal(call.args[0])
        if entries:
            edits.setdefault(slot, {}).update(entries)
    return edits


def _register_calls(tree: ast.Module) -> Iterator[ast.Call]:
    """``x.register("prefix", ViewSet)`` calls, and nothing that merely looks
    like one.

    ``register`` is a popular method name -- pretix has a plugin registry and
    Django's admin has ``admin.site.register(Model, ModelAdmin)``, both of
    which would otherwise land here. Requiring a string first argument
    separates them exactly: a router prefix is a URL fragment and an admin
    registration's first argument is a model class.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "register":
            continue
        if not isinstance(node.args[0], ast.Constant | ast.JoinedStr | ast.BinOp):
            continue
        pattern, _ = pattern_of(node.args[0])
        if pattern or isinstance(node.args[0], ast.JoinedStr):
            yield node


def _router_variables(
    tree: ast.Module, module: str, index: ClassIndex, known: frozenset[str]
) -> dict[str, str]:
    """Names bound to a router instance in one module, by router class.

    ``router = NetBoxRouter()``, which is how every urlconf in both benchmark
    projects opens.
    """
    found: dict[str, str] = {}
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
            continue
        called = dotted_name(stmt.value.func)
        if called is None:
            continue
        resolved = index.resolve_name(module, called)
        if resolved not in known and resolved not in ROUTER_BASES:
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                found[target.id] = resolved
    return found


def router_variables(
    ctx: ProjectContext, index: ClassIndex, known: frozenset[str]
) -> dict[str, dict[str, str]]:
    """Every router instance in the project, by the module that built it.

    Collected up front because a router is routinely registered on from
    somewhere else. Every pretix plugin does
    ``from pretix.api.urls import event_router`` and registers its viewsets on
    that shared instance, so a reader that only recognised routers constructed
    in the file it is looking at would call nine plugin viewsets -- including
    two ``ModelViewSet``s -- unroutable, and therefore exempt from every
    authorization rule.
    """
    found: dict[str, dict[str, str]] = {}
    for path in ctx.python_files:
        tree = ctx.parse(path)
        if tree is None:
            continue
        module = package_dotted(path)
        variables = _router_variables(tree, module, index, known)
        if variables:
            found[module] = variables
    return found


def _imported_router(
    index: ClassIndex, module: str, name: str, routers: dict[str, dict[str, str]]
) -> str | None:
    """A router instance this module imported from another one."""
    resolved = index.resolve_name(module, name)
    origin, _, attribute = resolved.rpartition(".")
    return routers.get(origin, {}).get(attribute)


def _basename(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "basename":
            value = literal(kw.value)
            return value if isinstance(value, str) else None
    if len(call.args) >= 3:
        value = literal(call.args[2])
        return value if isinstance(value, str) else None
    return None


def registrations_in(
    ctx: ProjectContext,
    path: Path,
    index: ClassIndex,
    surface: ApiSurface,
    routers: dict[str, dict[str, str]] | None = None,
) -> Iterator[Registration]:
    """Router registrations in one module, with the viewset resolved."""
    tree = ctx.parse(path)
    if tree is None:
        return
    module = package_dotted(path)
    routers = routers if routers is not None else {}
    local = routers.get(module, {})

    for call in _register_calls(tree):
        func = call.func
        assert isinstance(func, ast.Attribute)
        receiver = dotted_name(func.value)
        if receiver is None:
            continue
        router = local.get(receiver) or _imported_router(index, module, receiver, routers)
        if router is None:
            # A registration on something that is not a known router is a
            # registry of some other kind; pretix has several.
            continue
        prefix, is_literal = pattern_of(call.args[0])
        written = dotted_name(call.args[1]) or ast.unparse(call.args[1])
        resolved = index.resolve_name(module, written)
        yield Registration(
            prefix=prefix,
            literal=is_literal,
            view_ref=written,
            view=resolved if resolved in surface.views else None,
            basename=_basename(call),
            router=router,
            path=path,
            line=call.lineno,
        )


def endpoints_for(
    registration: Registration,
    view: ViewNode,
    mappings: dict[str, tuple[dict[str, str], dict[str, str]]],
) -> Iterator[Endpoint]:
    """Expand one registration into the requests it will answer.

    DRF's ``get_method_map`` binds a method only when the viewset implements
    the action it maps to, so the standard mappings are filtered against what
    2.2.2 found rather than applied wholesale. Without that filter every
    registered viewset would look like it accepted DELETE.

    "Implemented" spans both the actions the mixins supply and any method the
    class defines, because a customised router names actions DRF has never
    heard of -- NetBox's bulk operations among them -- and only the class says
    whether they exist.
    """
    list_map, detail_map = mappings.get(registration.router or "", (LIST_MAPPING, DETAIL_MAPPING))
    implemented = set(view.actions) | set(view.defined)

    for detail, mapping in ((False, list_map), (True, detail_map)):
        for method, action in sorted(mapping.items()):
            if action not in implemented:
                continue
            yield Endpoint(
                view=view.label,
                method=method,
                detail=detail,
                action=action,
                pattern=registration.prefix,
                literal=registration.literal,
                kind="detail" if detail else "list",
                path=registration.path,
                line=registration.line,
            )

    for extra in view.extra_actions:
        for method in extra.methods:
            yield Endpoint(
                view=view.label,
                method=method,
                detail=extra.detail,
                action=extra.name,
                pattern=f"{registration.prefix}/{extra.url_path or extra.name}",
                literal=registration.literal,
                kind="action",
                path=registration.path,
                line=registration.line,
            )


def _as_view_target(node: ast.expr) -> tuple[str | None, dict[str, str]]:
    """The view a route's second argument names, and any explicit binding.

    ``SiteViewSet.as_view({"get": "list"})`` is the router-free way to route a
    viewset, and the dictionary is the mapping, so it replaces the router table
    rather than being filtered by it.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "as_view":
            mapping = _mapping_literal(node.args[0]) if node.args else {}
            return dotted_name(func.value), mapping
        return None, {}
    return dotted_name(node), {}


def endpoints_in(
    ctx: ProjectContext, path: Path, index: ClassIndex, surface: ApiSurface
) -> Iterator[Endpoint]:
    """Endpoints from plain urlconf entries in one module.

    Covers the two shapes a router never produces: an ``APIView`` or generic
    routed by ``as_view()``, and an ``@api_view`` function routed by name.
    """
    tree = ctx.parse(path)
    if tree is None:
        return
    module = package_dotted(path)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ROUTERS:
            continue
        written, mapping = _as_view_target(node.args[1])
        if written is None:
            continue
        view = surface.views.get(index.resolve_name(module, written))
        if view is None:
            continue
        pattern, is_literal = pattern_of(node.args[0])
        methods: dict[str, str | None] = (
            dict(mapping) if mapping else dict.fromkeys(view.http_methods)
        )
        for method, action in sorted(methods.items(), key=lambda kv: kv[0]):
            yield Endpoint(
                view=view.label,
                method=method,
                detail=None,
                action=action,
                pattern=pattern,
                literal=is_literal,
                kind="path",
                path=path,
                line=node.args[0].lineno,
            )


def build_route_graph(
    ctx: ProjectContext, surface: ApiSurface, index: ClassIndex | None = None
) -> RouteGraph:
    """Join every view to the requests that reach it."""
    index = index if index is not None else ClassIndex(ctx)
    graph = RouteGraph()
    mappings = router_mappings(index)
    routers = router_variables(ctx, index, frozenset(mappings))
    unresolved: list[str] = []

    for path in ctx.python_files:
        for registration in registrations_in(ctx, path, index, surface, routers):
            graph.registrations.append(registration)
            if registration.view is None:
                unresolved.append(registration.view_ref)
                continue
            view = surface.views[registration.view]
            graph.endpoints.extend(endpoints_for(registration, view, mappings))
        graph.endpoints.extend(endpoints_in(ctx, path, index, surface))

    graph.unresolved_views = tuple(sorted(set(unresolved)))
    return graph
