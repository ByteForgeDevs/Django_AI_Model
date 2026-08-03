"""Finding the serializers, and tying each one to the model it exposes.

Models can only live in an app's `models` module, so the graph finds them by
looking in the obvious place. Serializers have no such rule — NetBox spreads
224 of them over `api/serializers.py` and `api/serializers_/*.py` — so they
are found by ancestry instead, which means resolving base classes across
module boundaries exactly as 2.1.6 does for models.

Ancestry is also the only honest test. `NetBoxModelSerializer` is four classes
removed from `ModelSerializer` and mixes in two plain `Serializer` subclasses
along the way; nothing about its name or location says what it is.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from djaudit.api.querysets import QuerysetNode, read_queryset
from djaudit.api.routes import RouteGraph, build_route_graph
from djaudit.api.serializers import (
    SERIALIZER_BASES,
    SerializerNode,
    build_serializer,
)
from djaudit.api.views import (
    VIEW_BASES,
    ViewNode,
    build_function_view,
    build_view,
)
from djaudit.astutils import resolve_dotted
from djaudit.graph.inheritance import ClassIndex, package_dotted

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.graph.nodes import ModelGraph


@dataclass
class ApiSurface:
    """Every serializer in the project, and the models they expose."""

    serializers: dict[str, SerializerNode] = field(default_factory=dict)

    views: dict[str, ViewNode] = field(default_factory=dict)
    """Every view, class-based or decorated function, keyed by dotted label."""

    routes: RouteGraph = field(default_factory=RouteGraph)
    """Which HTTP methods actually reach each view, once routers are applied."""

    querysets: dict[str, QuerysetNode] = field(default_factory=dict)
    """What rows each view can reach, and whether the request narrows them."""

    index: ClassIndex | None = None
    """The class index this surface was built from.

    Kept because permission resolution is deliberately not done here -- it
    depends on which settings module a rule considers authoritative -- and a
    rule that has the surface should not have to rebuild the index to finish
    the job.
    """

    by_model: dict[str, list[SerializerNode]] = field(default_factory=dict)
    """Serializers keyed by the model label they serialise. A model reachable
    through several serializers is exposed by the loosest of them, so a rule
    asking "is this model over-exposed" has to see all of them."""

    unresolved_models: tuple[str, ...] = ()
    """``Meta.model`` references that named something outside the model graph
    -- a third-party model, usually. Kept so the number can be watched rather
    than silently absorbed."""

    def get(self, label: str) -> SerializerNode | None:
        return self.serializers.get(label)

    def for_model(self, label: str) -> list[SerializerNode]:
        return self.by_model.get(label, [])

    @property
    def model_serializers(self) -> list[SerializerNode]:
        return [s for s in self.serializers.values() if s.is_model_serializer]

    @property
    def viewsets(self) -> list[ViewNode]:
        return [v for v in self.views.values() if v.is_viewset]

    @property
    def writable_views(self) -> list[ViewNode]:
        """Views through which data can change, which is where authorization
        rules start rather than the full set."""
        return [v for v in self.views.values() if v.writes]

    @property
    def unfiltered_views(self) -> list[ViewNode]:
        """Routed views that read every row of a model without consulting the
        request. The precondition for an IDOR finding, never the finding: the
        endpoint may be public by design, or a permission class may be doing
        the work instead."""
        return [
            view
            for label, view in self.views.items()
            if label in self.querysets and self.querysets[label].unfiltered
        ]

    @property
    def unrouted_views(self) -> list[ViewNode]:
        """Views nothing routes.

        Usually a base class the project defines for its own viewsets to
        inherit, which is worth separating out: an unreachable view cannot be
        an authorization defect, and reporting one is how a tool trains people
        to stop reading its output."""
        routed = self.routes.routed()
        return [v for v in self.views.values() if v.label not in routed]

    def __len__(self) -> int:
        return len(self.serializers)

    def __iter__(self) -> Iterator[SerializerNode]:
        return iter(self.serializers.values())


def resolve_model(
    node: SerializerNode,
    index: ClassIndex,
    by_class: dict[tuple[Path, str], str],
) -> str | None:
    """Turn ``Meta.model`` into a model graph label.

    ``model = Circuit`` is a name in the serializer's module, so it resolves
    through that module's imports -- the same binding-not-spelling rule the
    model graph uses, and the reason a serializer importing ``Circuit`` from a
    package ``__init__`` still lands on the right class. A dotted
    ``"app.Model"`` string is not valid here: unlike a ForeignKey,
    ``Meta.model`` must be the class itself.

    Matching is by definition site rather than by name, because two apps
    routinely define classes with the same name.
    """
    if node.model_ref is None:
        return None
    bindings = index.bindings_for(node.module)
    record = index.lookup(resolve_dotted(bindings, node.model_ref))
    if record is None:
        # The name may be defined in the serializer's own module rather than
        # imported into it.
        record = index.lookup(f"{node.module}.{node.model_ref}")
    if record is None:
        return None
    return by_class.get((record.path, record.name))


def build_api_surface(
    ctx: ProjectContext,
    graph: ModelGraph,
    index: ClassIndex | None = None,
) -> ApiSurface:
    """Discover every serializer and resolve what it exposes."""
    index = index if index is not None else ClassIndex(ctx)
    surface = ApiSurface()
    unresolved: list[str] = []
    by_class = {(model.path, model.name): model.label for model in graph}

    for record in index.records():
        if not index.inherits(record, SERIALIZER_BASES):
            continue
        node = build_serializer(record, index)
        surface.serializers[node.label] = node
        node.model = resolve_model(node, index, by_class)
        if node.model is not None:
            surface.by_model.setdefault(node.model, []).append(node)
        elif node.model_ref is not None:
            unresolved.append(node.model_ref)

    surface.unresolved_models = tuple(sorted(set(unresolved)))

    for record in index.records():
        if index.inherits(record, VIEW_BASES):
            view = build_view(record, index)
            surface.views[view.label] = view

    for view in discover_function_views(ctx, index):
        surface.views[view.label] = view

    surface.index = index
    surface.routes = build_route_graph(ctx, surface, index)

    # Only routed views: an unrouted one is a base class the project wrote for
    # its own viewsets, and resolving a queryset for it costs an ancestry walk
    # to describe rows nobody can request.
    routed = surface.routes.routed()
    for label, view in surface.views.items():
        if label in routed:
            surface.querysets[label] = read_queryset(view, index, by_class)

    return surface


def discover_function_views(ctx: ProjectContext, index: ClassIndex) -> Iterator[ViewNode]:
    """Functions carrying ``@api_view``, which no class index can find.

    Walked at module scope only. A view defined inside another function is
    not routable without the enclosing call having run, which is not something
    a static read can claim happened.
    """
    for path in ctx.python_files:
        tree = ctx.parse(path)
        if tree is None or not _mentions_api_view(tree):
            continue
        # ``package_dotted``, not ``dotted_path``: the class index keys modules
        # the way Python imports them, by walking up while ``__init__.py``
        # exists. NetBox's code sits in ``<root>/netbox/`` and pretix's in
        # ``<root>/src/``, so anchoring on the root produces a module name that
        # is in no index and silently binds nothing.
        module = package_dotted(path)
        bindings = index.bindings_for(module)
        for stmt in tree.body:
            if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            view = build_function_view(stmt, module, path, bindings)
            if view is not None:
                yield view


def _mentions_api_view(tree: ast.Module) -> bool:
    """Cheap gate before walking a module's functions.

    ``api_view`` has to be imported to be used, and the import is at module
    scope, so a module that never names it cannot contain a function view.
    Over a thousand files this is the difference between reading imports and
    reading every decorator in the project.
    """
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            if (stmt.module or "").startswith("rest_framework"):
                return True
        elif isinstance(stmt, ast.Import) and any(
            alias.name.startswith("rest_framework") for alias in stmt.names
        ):
            return True
    return False
