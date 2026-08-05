"""Which variable holds one row, and which rows it holds.

:mod:`~djaudit.dataflow.querysets` says a name holds a queryset over a model.
:mod:`~djaudit.dataflow.chaining` says what that queryset already fetched. This
answers the last question standing between those two and `DJP-001`: *when the
loop runs, which variable is a row, and a row of what?*

``book.author`` costs a query only if ``book`` is a ``Book`` row. Nothing said
so far establishes that, because nothing so far has looked at a loop.

**The wrappers are the substance of this substep.** Every one of these iterates
the same rows, and a matcher keyed on ``for x in <Model>.objects`` sees none of
them::

    for book in list(qs): ...            # the single most common spelling
    for book in sorted(qs, key=...): ...
    for book in reversed(qs): ...
    for book in qs.iterator(): ...       # exactly where N+1 hurts most
    for i, book in enumerate(qs): ...    # the row is at index 1, not 0
    for book, tag in zip(books, tags): ...  # position decides which queryset

Unwrapping composes with assignment, so ``rows = list(qs)`` followed by ``for
book in rows`` resolves, and it composes with itself, so ``reversed(sorted(
list(qs)))`` does too.

**Tuple targets are where a careless reader invents a model.** ``for a, b in
qs`` over an instance-yielding queryset cannot mean what it says -- a model
instance does not unpack -- so neither name is a row, and both are reported as
:attr:`Bind.OTHER` rather than being handed the model. The shape is real: it is
what ``values_list()`` looks like, and there the rows are tuples with no
related attributes and no N+1 available at all.

**What we deliberately do not do.** A loop target rebound inside the body is
not tracked here, because :mod:`~djaudit.dataflow.chains` already answers it:
a use after the rebind has the rebind as its reaching definition, and asking
the same question twice in two places is how the two answers start to
disagree. We record the binding node and let the caller ask.

**A Django trap that is not the one it looks like.** ``qs.prefetch_related(
"x").iterator()`` looks like a dropped prefetch, and before Django 4.1 that is
what it was. Since 4.1 it *raises* ``ValueError`` unless ``chunk_size`` is
given -- verified against ``django/db/models/query.py``, not assumed. So it is
a crash, not an N+1, and :attr:`Loop.prefetch_conflict` records it as its own
observation rather than quietly inflating the N+1 count. ``select_related`` is
unaffected either way: it is a join, and joins survive iteration.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from djaudit.dataflow.chaining import ChainSpec, analyse
from djaudit.dataflow.chains import DefUse
from djaudit.dataflow.querysets import QuerysetTracker, QuerysetValue
from djaudit.dataflow.scopes import BindingKind, Scope, ScopeKind
from djaudit.graph.nodes import ModelGraph

#: Callables that return the same rows in a different container. Iterating the
#: result iterates the queryset, so an N+1 inside is the same N+1.
ELEMENT_PRESERVING = frozenset({"list", "tuple", "set", "frozenset", "sorted", "reversed", "iter"})

#: QuerySet methods that hand back the same rows one at a time.
ROW_ITERATORS = frozenset({"iterator", "aiterator"})

#: Comprehension forms. Each is a loop wearing different punctuation.
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

#: Nodes that open a scope of their own. Their names resolve against their own
#: chains, so this scope must not walk into them and answer for them.
NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

#: How far ``rows = list(qs)`` chains are followed before we give up. Deep
#: enough for every shape seen on the corpora, bounded so a pathological or
#: cyclic definition cannot spin.
_MAX_HOPS = 8


class Bind(StrEnum):
    """What a loop target actually holds."""

    ELEMENT = "element"
    """One row of the iterated queryset."""

    COUNTER = "counter"
    """``enumerate``'s index. Never a row, however model-shaped the name."""

    OTHER = "other"
    """Part of a shape we could not attribute. Deliberately not a row."""


class LoopKind(StrEnum):
    FOR = "for"
    ASYNC_FOR = "async_for"
    COMPREHENSION = "comprehension"


@dataclass(frozen=True)
class Target:
    """A name bound by a loop, and what it holds."""

    name: str
    node: ast.Name
    binds: Bind
    value: QuerysetValue | None = None
    spec: ChainSpec | None = None

    @property
    def model(self) -> str | None:
        """The model whose row this name holds, when that is known."""
        if self.binds is not Bind.ELEMENT or self.value is None:
            return None
        if self.value.terminal:
            # `Book.objects.get(...)` is one instance and `qs[0]` is one row.
            # Looping over either is not looping over rows of the model.
            return None
        if self.spec is not None and not self.spec.yields_instances:
            # `values()` rows are dicts. They have no related attributes, so
            # naming a model here would invite a finding that cannot exist.
            return None
        return self.value.model


@dataclass(frozen=True)
class Loop:
    """One iteration construct, with its targets resolved."""

    node: ast.For | ast.AsyncFor | ast.comprehension
    kind: LoopKind
    iterable: ast.expr
    targets: tuple[Target, ...] = ()
    depth: int = 1
    """1 for an outermost loop, 2 inside it, and so on. An N+1 at depth 2
    runs N*M times, which is the difference between slow and down."""

    wrappers: tuple[str, ...] = ()
    """Unwrapped callables, outermost first -- evidence for why we claim a
    queryset the source does not name at the loop."""

    unbounded_rows: bool = True
    """False when the queryset was sliced, which caps the row count and so
    caps the damage."""

    prefetch_conflict: bool = False
    """``prefetch_related()`` followed by ``iterator()`` with no
    ``chunk_size``: a ``ValueError`` at runtime on Django >= 4.1."""

    @property
    def anchor(self) -> ast.expr | ast.stmt:
        """A node that carries a source position.

        ``ast.comprehension`` is not a statement and has no ``lineno``, so a
        rule that reports at ``loop.node`` raises ``AttributeError`` on every
        comprehension it sees. The iterable is the right anchor anyway: it is
        the expression whose cost the finding is about.
        """
        if isinstance(self.node, ast.comprehension):
            return self.iterable
        return self.node

    @property
    def lineno(self) -> int:
        """Line to report a finding against."""
        return self.anchor.lineno

    @property
    def element(self) -> Target | None:
        """The single target holding a row, if exactly one does."""
        rows = [t for t in self.targets if t.binds is Bind.ELEMENT]
        return rows[0] if len(rows) == 1 else None

    @property
    def over_model(self) -> str | None:
        target = self.element
        return target.model if target is not None else None

    @property
    def body(self) -> tuple[ast.stmt, ...]:
        """Statements that run per row. Empty for a comprehension, whose body
        is an expression the caller already holds."""
        if isinstance(self.node, ast.For | ast.AsyncFor):
            return tuple(self.node.body)
        return ()

    @property
    def nested(self) -> bool:
        return self.depth > 1


@dataclass
class _Resolver:
    tracker: QuerysetTracker
    chains: DefUse
    parameters: Mapping[int, QuerysetValue] = field(default_factory=dict)
    scope: Scope | None = None
    seen: set[int] = field(default_factory=set)

    def iterated(
        self, node: ast.expr, hops: int = 0
    ) -> tuple[QuerysetValue | None, tuple[str, ...]]:
        """The queryset a loop over ``node`` actually walks, and the wrappers
        peeled off to find it."""
        if hops > _MAX_HOPS or id(node) in self.seen:
            return None, ()
        self.seen.add(id(node))

        direct = self.tracker.classify(node)
        if direct is not None:
            return direct, ()

        if isinstance(node, ast.Call):
            inner = _unwrap_call(node)
            if inner is not None:
                name, arg = inner
                value, rest = self.iterated(arg, hops + 1)
                return value, (name, *rest)

        if isinstance(node, ast.Name):
            supplied = self._from_parameter(node)
            if supplied is not None:
                return supplied, ()
            following = self._through_assignment(node)
            if following is not None:
                return self.iterated(following, hops + 1)

        return None, ()

    def _from_parameter(self, node: ast.Name) -> QuerysetValue | None:
        """What the module's own callers pass for this parameter, if they agree.

        Only consulted when the name is genuinely a parameter here. A local
        that happens to share a parameter's name elsewhere must not pick up
        its value.
        """
        if not self.parameters or self.scope is None:
            return None
        binding = self.scope.resolve(node.id)
        if binding is None or binding.kind is not BindingKind.PARAMETER:
            return None
        return self.parameters.get(id(binding.node))

    def _through_assignment(self, node: ast.Name) -> ast.expr | None:
        """One hop back along the def-use chain, when exactly one definition
        reaches and it is an assignment we can read."""
        use = self.chains.of(node)
        if use is None or not use.unambiguous:
            return None
        binding = use.definition
        if binding is None or binding.value is None:
            return None
        if binding.kind not in {BindingKind.ASSIGNMENT, BindingKind.WALRUS}:
            return None
        return binding.value


def _unwrap_call(node: ast.Call) -> tuple[str, ast.expr] | None:
    """``list(qs)`` -> ``("list", qs)``. Only for calls that preserve rows."""
    if isinstance(node.func, ast.Name):
        if node.func.id in ELEMENT_PRESERVING and node.args:
            return node.func.id, node.args[0]
        return None
    if isinstance(node.func, ast.Attribute) and node.func.attr in ROW_ITERATORS:
        return node.func.attr, node.func.value
    return None


def _shape(node: ast.expr) -> tuple[str, list[ast.expr]] | None:
    """``enumerate(qs)`` and ``zip(a, b)`` build tuples whose positions come
    from known places. Everything else does not."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    if node.func.id in {"enumerate", "zip"} and node.args:
        return node.func.id, list(node.args)
    return None


def _names(node: ast.expr) -> list[ast.Name]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Name)]


def _prefetch_conflict(value: QuerysetValue | None, iterable: ast.expr) -> bool:
    """``prefetch_related(...).iterator()`` with no ``chunk_size``.

    Only the synchronous ``iterator()`` raises: ``aiterator()`` defaults
    ``chunk_size`` to 2000 and prefetches happily. Both facts are read off
    ``django/db/models/query.py``, because the plausible guess -- that the
    prefetch is silently dropped, making this an N+1 -- is wrong, and would
    have filed this under the wrong rule with the wrong remediation.
    """
    if value is None or not analyse(value).prefetch_related:
        return False
    for inner in ast.walk(iterable):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "iterator"
        ):
            return not inner.args and not inner.keywords
    return False


class _Finder:
    def __init__(self, resolver: _Resolver) -> None:
        self.resolver = resolver
        self.found: list[Loop] = []

    def visit(self, nodes: Sequence[ast.stmt], depth: int) -> None:
        for node in nodes:
            self.statement(node, depth)

    def statement(self, node: ast.stmt, depth: int) -> None:
        if isinstance(node, NESTED_SCOPES):
            # A separate scope with its own chains. It gets its own call.
            return
        if isinstance(node, ast.For | ast.AsyncFor):
            self.loop(node, depth)
            return
        for expr in _expressions(node):
            self.comprehensions(expr, depth)
        self.visit(_child_statements(node), depth)

    def loop(self, node: ast.For | ast.AsyncFor, depth: int) -> None:
        self.comprehensions(node.iter, depth)
        kind = LoopKind.FOR if isinstance(node, ast.For) else LoopKind.ASYNC_FOR
        self.found.append(self._build(node, kind, node.target, node.iter, depth))
        self.visit(node.body, depth + 1)
        self.visit(node.orelse, depth)

    def comprehensions(self, node: ast.expr, depth: int) -> None:
        """Comprehension generators, at the depth they actually run at.

        Descent stops at a nested scope, so a comprehension inside a lambda is
        not attributed to the function containing the lambda.
        """
        if isinstance(node, NESTED_SCOPES):
            return
        if isinstance(node, COMPREHENSIONS):
            for offset, gen in enumerate(node.generators):
                self.comprehensions(gen.iter, depth + offset)
                self.found.append(
                    self._build(
                        gen,
                        LoopKind.COMPREHENSION,
                        gen.target,
                        gen.iter,
                        depth + offset,
                    )
                )
                for test in gen.ifs:
                    self.comprehensions(test, depth + offset + 1)
            inner = depth + len(node.generators)
            for part in _result_parts(node):
                self.comprehensions(part, inner)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.comprehensions(child, depth)

    def _build(
        self,
        node: ast.For | ast.AsyncFor | ast.comprehension,
        kind: LoopKind,
        target: ast.expr,
        iterable: ast.expr,
        depth: int,
    ) -> Loop:
        shape = _shape(iterable)
        if shape is not None:
            targets, wrappers, value = self._structured(target, *shape)
        else:
            value, wrappers = self.resolver.iterated(iterable)
            targets = self._flat(target, value)

        spec = analyse(value) if value is not None else None
        return Loop(
            node=node,
            kind=kind,
            iterable=iterable,
            targets=targets,
            depth=depth,
            wrappers=wrappers,
            unbounded_rows=spec is None or not spec.sliced,
            prefetch_conflict=_prefetch_conflict(value, iterable),
        )

    def _flat(self, target: ast.expr, value: QuerysetValue | None) -> tuple[Target, ...]:
        if isinstance(target, ast.Name):
            spec = analyse(value) if value is not None else None
            return (Target(target.id, target, Bind.ELEMENT, value, spec),)
        # Unpacking a row. A model instance does not unpack, so whatever these
        # names hold, it is not the row -- saying otherwise invents a model.
        return tuple(Target(name.id, name, Bind.OTHER) for name in _names(target))

    def _structured(
        self, target: ast.expr, builder: str, parts: list[ast.expr]
    ) -> tuple[tuple[Target, ...], tuple[str, ...], QuerysetValue | None]:
        """``enumerate`` and ``zip`` put known iterables at known positions."""
        sources = parts if builder == "zip" else parts[:1]
        resolved = [self.resolver.iterated(part) for part in sources]

        if not isinstance(target, ast.Tuple):
            # `for pair in enumerate(qs)` -- the name holds a tuple, not a row.
            return (
                tuple(Target(n.id, n, Bind.OTHER) for n in _names(target)),
                (),
                None,
            )

        slots: list[QuerysetValue | None] = []
        if builder == "enumerate":
            slots = [None, resolved[0][0] if resolved else None]
        else:
            slots = [value for value, _ in resolved]

        targets: list[Target] = []
        for index, item in enumerate(target.elts):
            found = slots[index] if index < len(slots) else None
            if not isinstance(item, ast.Name):
                targets.extend(Target(n.id, n, Bind.OTHER) for n in _names(item))
                continue
            if builder == "enumerate" and index == 0:
                targets.append(Target(item.id, item, Bind.COUNTER))
                continue
            if found is None:
                targets.append(Target(item.id, item, Bind.OTHER))
                continue
            targets.append(Target(item.id, item, Bind.ELEMENT, found, analyse(found)))

        primary = slots[1] if builder == "enumerate" and len(slots) > 1 else None
        wrappers = resolved[0][1] if builder == "enumerate" and resolved else ()
        return tuple(targets), wrappers, primary


def _child_statements(node: ast.stmt) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for name in ("body", "orelse", "finalbody"):
        out.extend(getattr(node, name, []) or [])
    for handler in getattr(node, "handlers", []) or []:
        out.extend(handler.body)
    for case in getattr(node, "cases", []) or []:
        out.extend(case.body)
    return out


def _result_parts(node: ast.expr) -> list[ast.expr]:
    """What a comprehension builds per iteration."""
    if isinstance(node, ast.DictComp):
        return [node.key, node.value]
    if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        return [node.elt]
    return []


def _expressions(node: ast.stmt) -> Iterator[ast.expr]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            yield child


def find_loops(
    scope: Scope,
    chains: DefUse,
    graph: ModelGraph,
    *,
    app_label: str | None = None,
    parameters: Mapping[int, QuerysetValue] | None = None,
) -> list[Loop]:
    """Every loop in ``scope``, with its targets resolved to rows where known.

    Nested function, class and lambda bodies are skipped: they are their own
    scopes with their own chains, and resolving their names against this
    scope's is how a resolver starts confidently reporting the wrong variable.

    A comprehension scope returns nothing, because the scope lexically
    containing it already reported its generators -- and reported them at the
    right nesting depth, which a comprehension looked at alone cannot know.
    Counting them in both places would double every comprehension in the
    corpus, and the duplicate would look exactly like a real second finding.

    The explicit guard below is defence in depth, not the thing that makes
    this true: a comprehension node has no ``body``, so the generic path
    already returns nothing, and deleting the guard alone changes no result.
    What actually holds the invariant is the fallback declining to walk the
    whole subtree -- verified by reinstating that walk and watching six tests
    fail. The guard stays because the accident it duplicates is fragile.
    """
    if scope.kind is ScopeKind.COMPREHENSION:
        return []

    tracker = QuerysetTracker(scope, chains, graph, app_label=app_label)
    finder = _Finder(_Resolver(tracker, chains, parameters or {}, scope))

    body = getattr(scope.node, "body", None)
    if isinstance(scope.node, ast.Lambda) or not isinstance(body, list):
        if isinstance(body, ast.expr):
            finder.comprehensions(body, 1)
        return finder.found

    finder.visit(body, 1)
    return finder.found
