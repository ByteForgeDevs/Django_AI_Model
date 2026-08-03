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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from djaudit.api.serializers import (
    SERIALIZER_BASES,
    SerializerNode,
    build_serializer,
)
from djaudit.astutils import resolve_dotted
from djaudit.graph.inheritance import ClassIndex

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.graph.nodes import ModelGraph


@dataclass
class ApiSurface:
    """Every serializer in the project, and the models they expose."""

    serializers: dict[str, SerializerNode] = field(default_factory=dict)

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
    return surface
