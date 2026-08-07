"""Where a queryset came from, and which model's rows it holds.

:mod:`djaudit.dataflow.chains` answers "which definition reaches this name".
This answers the question a `DJP` rule actually asks next: *is that definition a
queryset, and over what?*

Every ORM query starts at a manager. Recognising the manager is what turns a
name into a model label, and the model label is what makes the rest of Phase 3
possible -- you cannot say ``book.author`` is an unprefetched forward relation
until you know ``book`` is a ``Book``.

**Origins we recognise.** Each is a real shape from the benchmark targets, not a
hypothetical::

    Book.objects.all()              MANAGER          -- the ordinary case
    Book._default_manager.all()     DEFAULT_MANAGER  -- what Django's own generic
                                                        views use internally
    Book.objects.filter(...)        MANAGER          -- chained, still a Book
    author.books.all()              RELATED          -- reverse accessor, resolved
                                                        through the graph when the
                                                        instance model is known
    self.get_queryset()             SELF             -- a view's own, model unknown
                                                        here by design

**Following through assignment is the point.** A rule that only recognises
``Model.objects...`` written inline at the loop finds almost nothing, because
real code names the queryset first::

    qs = Book.objects.all()
    qs = qs.select_related("author")
    for book in qs:                 # still a Book queryset, two hops back

Resolution walks the def-use chain, so this works to any depth. It stops at an
**ambiguous** use -- more than one definition reaching -- rather than picking
one, for the reason given in :mod:`~djaudit.dataflow.chains`: choosing the
prefetched branch hides a real N+1 and choosing the other invents one.

**Self-reference is a real shape, not a pathology.** The accumulate-in-a-loop
idiom makes a queryset's definition depend on itself::

    qs = Book.objects.all()
    for f in requested_filters:
        qs = qs.filter(**f)

At the loop, two definitions reach ``qs``, so it is ambiguous and resolution
stops there anyway. But the recursion still has to be guarded, because the
second definition's value mentions the very name being resolved. We keep an
in-progress set and return ``None`` on re-entry rather than recursing forever.

**What we deliberately do not do.** No attempt is made to resolve a manager on
an expression whose model we cannot see -- ``self.model.objects``,
``get_user_model().objects``, a queryset handed in as a parameter. Those return
an :attr:`Origin.UNKNOWN` value carrying the chain but no model, so a caller can
still reason about the *methods* applied without being told a model that might
be wrong.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from djaudit.dataflow.scopes import BindingKind

if TYPE_CHECKING:
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.scopes import Binding, Scope
    from djaudit.graph.nodes import ModelGraph

#: Attributes Django itself puts on every model class.
IMPLICIT_MANAGERS = frozenset({"objects", "_default_manager", "_base_manager"})

#: Synthetic step names for subscripting, which has no method name of its own.
SLICE = "__slice__"
INDEX = "__index__"

#: Methods that return a queryset, so the chain continues through them.
CHAINING = frozenset(
    {
        "all",
        "filter",
        "exclude",
        "annotate",
        "alias",
        "order_by",
        "reverse",
        "distinct",
        "values",
        "values_list",
        "dates",
        "datetimes",
        "none",
        "union",
        "intersection",
        "difference",
        "select_related",
        "prefetch_related",
        "extra",
        "defer",
        "only",
        "using",
        "select_for_update",
        "raw",
        "get_queryset",
        SLICE,
    }
)

#: Methods that end a queryset, returning a model instance or a scalar.
TERMINAL = frozenset(
    {
        "get",
        "first",
        "last",
        "create",
        "get_or_create",
        "update_or_create",
        "count",
        "exists",
        "aggregate",
        "earliest",
        "latest",
        "in_bulk",
        "delete",
        "update",
        "bulk_create",
        "bulk_update",
        INDEX,
    }
)


class Origin(StrEnum):
    """How a queryset was started."""

    MANAGER = "manager"
    """``Model.objects`` or any manager the model declares."""

    DEFAULT_MANAGER = "default_manager"
    """``Model._default_manager`` / ``_base_manager``."""

    RELATED = "related"
    """A related accessor on an instance -- ``author.books``."""

    SELF = "self"
    """``self.get_queryset()`` or ``super().get_queryset()``."""

    UNKNOWN = "unknown"
    """Looks like a queryset, but rooted at something we cannot see."""


@dataclass(frozen=True, slots=True)
class QuerysetValue:
    """A queryset-valued expression, and what we could learn about it."""

    node: ast.expr
    origin: Origin
    model: str | None = None
    """Resolved ``app_label.ModelName``, or ``None`` when unknown."""

    manager: str | None = None
    """The attribute the query started at -- ``objects`` unless declared
    otherwise."""

    chain: tuple[Step, ...] = ()
    """Steps applied after the manager, in the order written."""

    via: tuple[Binding, ...] = ()
    """Definitions walked through to get here, nearest first. Empty when the
    queryset was written inline."""

    @property
    def known(self) -> bool:
        """We can name the model, so a relation rule may speak about it."""
        return self.model is not None

    @property
    def terminal(self) -> bool:
        """The chain has left queryset-land and cannot come back.

        Any terminal step counts, not just the last one. ``get()`` returns an
        instance and ``count()`` returns an integer; nothing written after
        either is a queryset, so checking only ``chain[-1]`` answers ``False``
        for ``Book.objects.get(pk=1).pk`` -- an integer confidently reported as
        a ``Book`` queryset.
        """
        return any(step.name in TERMINAL for step in self.chain)

    @property
    def methods(self) -> tuple[str, ...]:
        """Just the method names, in the order written."""
        return tuple(step.name for step in self.chain)

    @property
    def indirect(self) -> bool:
        """Reached by following at least one assignment."""
        return bool(self.via)


@dataclass(frozen=True, slots=True)
class Step:
    """One method in a queryset chain, with the call that applied it.

    The call node is what makes 3.1.4 possible: ``select_related`` alone is not
    a fact, ``select_related("author")`` is. A step reached by following an
    assignment keeps the call from where it was *written*, which is the only
    place the arguments exist.
    """

    name: str
    call: ast.Call | None = None
    """``None`` when the attribute was never called -- ``Book.objects`` has a
    manager step with no call, and so does a chain that ends in an attribute."""

    @property
    def args(self) -> tuple[ast.expr, ...]:
        return tuple(self.call.args) if self.call is not None else ()

    @property
    def keywords(self) -> tuple[ast.keyword, ...]:
        return tuple(self.call.keywords) if self.call is not None else ()

    @property
    def called(self) -> bool:
        return self.call is not None


def _root(node: ast.expr) -> tuple[ast.expr, list[Step]]:
    """Peel an attribute/call chain down to its root, collecting steps.

    ``Book.objects.filter(x=1)`` gives root ``Book`` and steps ``objects`` (no
    call) then ``filter`` (the call). The call belongs to the attribute it was
    applied to, which is one level *out* from the attribute node itself.
    """
    steps: list[Step] = []
    current = node
    pending: ast.Call | None = None
    while True:
        if isinstance(current, ast.Call):
            pending = current
            current = current.func
        elif isinstance(current, ast.Attribute):
            steps.append(Step(current.attr, pending))
            pending = None
            current = current.value
        elif isinstance(current, ast.Subscript):
            # `qs[:10]` is a queryset operation like any other, and dropping it
            # here would make every sliced queryset invisible to tracking.
            steps.append(Step(SLICE if isinstance(current.slice, ast.Slice) else INDEX))
            pending = None
            current = current.value
        else:
            return current, list(reversed(steps))


#: Roots that mean "the object this method is on", whose model is not visible
#: from the scope being analysed. ``_root`` peels ``super()`` down to the bare
#: name, so both spellings arrive here as a plain ``Name``.
SELF_ROOTS = frozenset({"self", "cls", "super"})

#: Methods on ``self`` that genuinely hand back a queryset. Deliberately short:
#: every other queryset-shaped method name is also an ordinary method name on an
#: ordinary object, and treating them alike claims thousands of expressions on
#: the benchmark targets that have no rows behind them at all.
QUERYSET_PROVIDERS = frozenset({"get_queryset", "get_query_set", "filter_queryset"})


def _past_terminal(chain: Sequence[Step]) -> bool:
    """Whether anything is written after a step that ends the queryset.

    ``Book.objects.get(pk=1).pk`` is an integer and ``qs[0].site`` is a related
    instance. Both keep a model label attached to a value that is not a
    queryset at all, which is the precise shape of a confident wrong answer:
    a loop over one would be reported as a loop over rows.

    An *unknown* method is not treated this way. ``Book.objects.for_user(u)``
    and ``qs.filter_available()`` are custom manager and queryset methods and
    are genuinely querysets -- 2,433 of them on NetBox alone -- so the rule
    here is specifically about leaving via a known exit, not about arriving
    somewhere unrecognised.
    """
    return any(step.name in TERMINAL for step in chain[:-1])


class QuerysetTracker:
    """Classifies queryset expressions within one scope.

    Held rather than passed as a free function because resolution is recursive
    and memoised: the same name is asked about once per use, and a view with a
    long ``get_queryset`` asks about the same two or three names repeatedly.
    """

    def __init__(
        self,
        scope: Scope,
        chains: DefUse,
        graph: ModelGraph,
        *,
        app_label: str | None = None,
    ) -> None:
        self.scope = scope
        self.chains = chains
        self.graph = graph
        self.app_label = app_label
        self._cache: dict[int, QuerysetValue | None] = {}
        self._active: set[int] = set()

    def classify(self, node: ast.expr) -> QuerysetValue | None:
        """What this expression is, or ``None`` if it is not a queryset."""
        key = id(node)
        if key in self._cache:
            return self._cache[key]
        if key in self._active:
            # Self-referential definition -- `qs = qs.filter(...)`. Recursing
            # would not terminate and the use is ambiguous anyway.
            return None
        self._active.add(key)
        try:
            found = self._classify(node)
        finally:
            self._active.discard(key)
        self._cache[key] = found
        return found

    def _classify(self, node: ast.expr) -> QuerysetValue | None:
        root, attrs = _root(node)

        # `_root` peels `super()` down to the bare name, so both spellings of
        # "a queryset from the object we are inside" land here.
        if isinstance(root, ast.Name) and root.id in SELF_ROOTS:
            # Only `get_queryset` starts one. Requiring merely a queryset-ish
            # method name here would claim `self.get(...)` on a DRF view,
            # `self.update()` on a form and `self.count()` on anything at all,
            # which on the benchmark targets is thousands of expressions that
            # have no rows behind them.
            if attrs and attrs[0].name in QUERYSET_PROVIDERS:
                return QuerysetValue(node, Origin.SELF, chain=tuple(attrs))
            return None

        if isinstance(root, ast.Name):
            return self._from_name(node, root, attrs)
        return None

    def _from_name(self, node: ast.expr, root: ast.Name, attrs: list[Step]) -> QuerysetValue | None:
        model = self.graph.get(root.id, app_label=self.app_label)
        if model is not None and attrs:
            return self._from_manager(node, model.label, attrs)
        if attrs:
            # Not a model class. It may be a name holding a queryset, in which
            # case the attributes are a continuation of its chain.
            base = self._resolve_name(root)
            if base is not None:
                return self._extend(node, base, attrs)
            return None
        # A bare name: does it hold a queryset?
        return self._resolve_name(root)

    def _from_manager(self, node: ast.expr, label: str, attrs: list[Step]) -> QuerysetValue | None:
        manager, *chain = attrs
        model = self.graph.get(label)
        declared = model.managers if model is not None else {}
        if manager.name not in declared and manager.name not in IMPLICIT_MANAGERS:
            return None
        origin = (
            Origin.DEFAULT_MANAGER
            if manager.name in {"_default_manager", "_base_manager"}
            else Origin.MANAGER
        )
        if _past_terminal(chain):
            return None
        return QuerysetValue(node, origin, model=label, manager=manager.name, chain=tuple(chain))

    def _resolve_name(self, node: ast.Name) -> QuerysetValue | None:
        """Follow a name back to the queryset it holds, if exactly one does."""
        use = self.chains.of(node)
        if use is None or not use.unambiguous:
            # Either unanalysed, or several definitions reach and picking one
            # would be a guess. Both mean we stop.
            return None
        binding = use.definition
        if binding is None or binding.value is None:
            return None
        if binding.kind not in {BindingKind.ASSIGNMENT, BindingKind.WALRUS}:
            return None
        found = self.classify(binding.value)
        if found is None:
            return None
        return QuerysetValue(
            node,
            found.origin,
            model=found.model,
            manager=found.manager,
            chain=found.chain,
            via=(binding, *found.via),
        )

    def _extend(
        self, node: ast.expr, base: QuerysetValue, attrs: list[Step]
    ) -> QuerysetValue | None:
        if not all(a.name in CHAINING | TERMINAL for a in attrs):
            return None
        combined = (*base.chain, *attrs)
        if _past_terminal(combined):
            return None
        return QuerysetValue(
            node,
            base.origin,
            model=base.model,
            manager=base.manager,
            chain=combined,
            via=base.via,
        )

    def related_model(self, owner: str, accessor: str) -> str | None:
        """The model on the far side of a related accessor.

        ``author.books`` is a ``Book`` queryset, but only the graph knows that,
        and only if the accessor was not suppressed with a trailing ``+``.
        """
        for edge in self.graph.incoming.get(owner, ()):
            if edge.accessor == accessor:
                return edge.source
        model = self.graph.get(owner)
        if model is not None:
            for edge in model.relations:
                if edge.field_name == accessor and edge.kind == "ManyToManyField":
                    return edge.target
        return None


def track(
    scope: Scope,
    chains: DefUse,
    graph: ModelGraph,
    *,
    app_label: str | None = None,
) -> dict[int, QuerysetValue]:
    """Every queryset-valued expression in a scope, keyed by ``id(node)``.

    Only the outermost expression of a chain is reported.
    ``Book.objects.filter(x=1)`` answers once, not once per attribute access,
    because a rule wants the whole chain and a partial prefix of it is never
    the interesting object.
    """
    tracker = QuerysetTracker(scope, chains, graph, app_label=app_label)
    found: dict[int, QuerysetValue] = {}
    inner: set[int] = set()

    for node in ast.walk(scope.node):
        if not isinstance(node, ast.Call | ast.Attribute | ast.Name | ast.Subscript):
            continue
        value = tracker.classify(node)
        if value is None:
            continue
        found[id(node)] = value
        root, _ = _root(node)
        current: ast.expr = node
        while current is not root:
            if isinstance(current, ast.Call):
                current = current.func
            elif isinstance(current, ast.Attribute | ast.Subscript):
                current = current.value
            else:
                break
            inner.add(id(current))

    return {key: value for key, value in found.items() if key not in inner}
