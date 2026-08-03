"""Walking the graph, which is what the authorization rules actually need.

An IDOR rule's question is never "does this model have a user field". It is
"can a row of this model be traced to the user who is allowed to see it, and by
what route" — because the route is what a `get_queryset` override has to
filter on. `Check` belongs to a user through `project__owner`, and a rule that
cannot produce that string cannot tell whether the view scoped it.

Three properties of a route decide how much it proves. A nullable foreign key
means a row may belong to nobody, so scoping on it silently drops rows. A
many-valued hop means the row belongs to *several* users and "the owner" is
not a question with one answer. And length matters: two hops is a design, six
is a coincidence.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from djaudit.graph.nodes import FieldNode, ModelGraph, ModelNode, RelationEdge

DEFAULT_MAX_HOPS = 4
"""Far enough for the ownership chains real projects write, short enough that
what it finds is a design rather than a coincidence. Healthchecks reaches its
user in one or two; NetBox's deepest genuine chain is three."""

DEFAULT_MAX_DEPTH = 2
"""Depth cap for field expansion. Every extra level multiplies by the width of
the models found at the last one, and a serializer nobody wrote is not worth
enumerating."""


@dataclass(frozen=True, slots=True)
class RelationPath:
    """A route from one model to another, as the ORM would spell it."""

    edges: tuple[RelationEdge, ...]

    target: str | None = None
    """Label of the model at the far end.

    Carried rather than derived, because the last edge often cannot supply it:
    a foreign key to ``settings.AUTH_USER_MODEL`` resolving to Django's own
    ``auth.User`` has ``target`` of ``None``, since that class is not in the
    project's source. The path builder holds the graph and knows the label, so
    it records it here instead of leaving the most important query answerless.
    """

    @property
    def hops(self) -> int:
        return len(self.edges)

    @property
    def lookup(self) -> str:
        """The path as a query lookup: ``project__owner``.

        The point of the whole exercise. This is the string a ``get_queryset``
        override has to filter on, so a rule can compare what a view wrote
        against what the graph says ownership requires.
        """
        return "__".join(edge.field_name for edge in self.edges)

    @property
    def is_optional(self) -> bool:
        """Some hop is nullable, so a row may belong to nobody.

        Scoping on a path like this drops those rows rather than protecting
        them, which is a different bug and often the one that matters.
        """
        return any(edge.null for edge in self.edges)

    @property
    def is_multi(self) -> bool:
        """Some hop is many-valued, so a row does not have *one* owner.

        Scoping a queryset on a path like this needs a ``distinct()`` and
        answers a different question than a rule usually assumes.
        """
        return any(edge.is_multi_valued for edge in self.edges)


def relation_path(
    graph: ModelGraph,
    source: str,
    target: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> RelationPath | None:
    """The shortest forward route from ``source`` to ``target``, if there is one.

    Forward only: ownership is a thing a row *has*, and the query that enforces
    it filters forward. A reverse relation says other rows point here, which is
    a different claim and not one that scopes anything.

    Breadth-first, so the route found is the shortest. A longer one may exist
    and be the one a developer had in mind, but the shortest is the one a
    reviewer will recognise and the one a rule should suggest.
    """
    start = graph.get(source)
    end = graph.get(target)
    if start is None:
        return None
    target_label = end.label if end is not None else target
    if start.label == target_label:
        return RelationPath((), target_label)

    seen = {start.label}
    queue: deque[tuple[ModelNode, tuple[RelationEdge, ...]]] = deque([(start, ())])
    while queue:
        model, route = queue.popleft()
        if len(route) >= max_hops:
            continue
        for edge in model.relations:
            if edge.target is None or edge.target in seen:
                continue
            extended = (*route, edge)
            if edge.target == target_label:
                return RelationPath(extended, target_label)
            seen.add(edge.target)
            following = graph.get(edge.target)
            if following is not None:
                queue.append((following, extended))
    return None


def path_to_user(
    graph: ModelGraph,
    model: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    allow_multi: bool = False,
) -> RelationPath | None:
    """How a row of ``model`` reaches the user who owns it.

    Only single-valued hops count. A row reached through a many-to-many does
    not have *an* owner, it has a set of them, and filtering a queryset along
    such a path returns duplicate rows rather than a scoped view. Allowing
    many-valued hops calls 133 of NetBox's 140 models user-owned, on chains
    like ``datafile__source__jobs__user`` -- "owned by whoever ran a job
    against the source of my file", which is not ownership. Requiring
    single-valued hops leaves 22, and those are real.

    The user model may be reached by a relation that never names it -- a
    project with a swapped ``AUTH_USER_MODEL`` writes
    ``settings.AUTH_USER_MODEL``, and Healthchecks writes ``User`` for a class
    that is not in its own checkout. Both are already marked on the edge, so
    they are followed here rather than re-derived.
    """
    node = graph.get(model)
    if node is None:
        return None
    if node.label == graph.user_model:
        return RelationPath((), graph.user_model)

    seen = {node.label}
    queue: deque[tuple[ModelNode, tuple[RelationEdge, ...]]] = deque([(node, ())])
    while queue:
        current, route = queue.popleft()
        if len(route) >= max_hops:
            continue
        for edge in current.relations:
            if edge.is_multi_valued and not allow_multi:
                continue
            extended = (*route, edge)
            if edge.points_at_user or edge.target == graph.user_model:
                return RelationPath(extended, graph.user_model)
            if edge.target is None or edge.target in seen:
                continue
            seen.add(edge.target)
            following = graph.get(edge.target)
            if following is not None:
                queue.append((following, extended))
    return None


def is_user_owned(
    graph: ModelGraph,
    model: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    allow_multi: bool = False,
) -> bool:
    """Whether rows of ``model`` belong to a user at all.

    The precondition for every IDOR finding: a model no user owns cannot be
    accessed by the wrong one, and reporting an unscoped queryset over a
    lookup table is how a rule teaches people to ignore it.
    """
    return path_to_user(graph, model, max_hops=max_hops, allow_multi=allow_multi) is not None


def reachable_fields(
    graph: ModelGraph,
    model: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    include_reverse: bool = False,
) -> dict[str, FieldNode]:
    """Every field reachable from ``model``, keyed by its ORM lookup path.

    What a serializer with ``depth`` set, or a filterset accepting arbitrary
    query parameters, actually exposes. ``fields = "__all__"`` on a serializer
    with ``depth = 1`` is a mass-assignment finding precisely when one of these
    paths lands on something like ``owner__is_staff``, and enumerating them is
    the only way to know.

    Reverse relations are opt-in because they answer a different question:
    forward is what a request can *set*, reverse is only what it can read.
    """
    node = graph.get(model)
    if node is None:
        return {}

    out: dict[str, FieldNode] = {}
    seen = {node.label}
    queue: deque[tuple[ModelNode, str, int]] = deque([(node, "", 0)])
    while queue:
        current, prefix, depth = queue.popleft()
        for name, fld in current.all_fields.items():
            out.setdefault(f"{prefix}{name}", fld)
        if depth >= max_depth:
            continue
        for edge in _outgoing(graph, current, include_reverse=include_reverse):
            label, step = edge
            if label in seen:
                continue
            following = graph.get(label)
            if following is None:
                continue
            seen.add(label)
            queue.append((following, f"{prefix}{step}__", depth + 1))
    return out


def _outgoing(
    graph: ModelGraph, model: ModelNode, *, include_reverse: bool
) -> list[tuple[str, str]]:
    """Traversable relations as ``(target label, lookup step)`` pairs."""
    steps = [(edge.target, edge.field_name) for edge in model.relations if edge.target]
    if include_reverse:
        # The reverse side is spelled with the query name, not the accessor:
        # author.book_set is attribute access, Author.objects.filter(book=...)
        # is the lookup, and only the second one belongs in a path.
        steps += [
            (edge.source, edge.query_name)
            for edge in graph.incoming.get(model.label, ())
            if edge.query_name
        ]
    return [(label, step) for label, step in steps if label and step]
