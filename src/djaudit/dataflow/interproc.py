"""What a parameter holds, when the module itself says so.

3.1.5 leaves a large population unresolved: a loop whose iterable is a
parameter. On the benchmark targets that is 15 / 118 / 221 loops, against 31 /
135 / 179 that resolve without help -- so on pretix the parameters outnumber
everything the intraprocedural analysis can see. Leaving them out is not a
neutral choice; it is a silent false-negative rate concentrated in exactly the
helper functions people extract *because* the loop was getting complicated::

    def render(books):          # what is `books`?
        for book in books:
            print(book.author)  # N+1, or already prefetched? unanswerable

    def view(request):
        return render(Book.objects.select_related("author"))

**One hop, one module, and a budget.** The call site above is the only new
fact needed. Following it further -- callee to callee, module to module --
buys progressively less and costs progressively more, and an unbounded
interprocedural walk on a repository this size produces confident nonsense
faster than it produces findings. So: the caller must be in the same module,
the chain is not followed transitively, and the work is capped.

**Disagreement is not a tie to be broken.** A function called twice with two
different models has no single answer, and picking either one is a coin flip
that will be reported as `firm`. The parameter is left unresolved instead.
The same goes for a function with no call site in the module: nothing is
known, which is different from knowing it is not a queryset.

**Positional shifts are the trap that quietly moves every argument.** A method
called as ``self.render(qs)`` binds ``qs`` to the *second* parameter, because
the first is ``self``. A ``staticmethod`` has no such parameter, and a
``classmethod``'s is ``cls``. Getting this wrong does not fail loudly -- it
attributes the model to the neighbouring parameter, which is a wrong answer
wearing the same confidence as a right one.

**Where we refuse.** ``*args`` and ``**kwargs`` at either end make the mapping
from argument to parameter unknowable, so nothing is claimed. Two functions
with the same name in one module -- the conditional-definition idiom -- are
ambiguous by construction. Recursion is guarded rather than followed.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

from djaudit.dataflow.chains import DefUse
from djaudit.dataflow.querysets import QuerysetTracker, QuerysetValue
from djaudit.dataflow.scopes import BindingKind, Scope, ScopeKind
from djaudit.graph.nodes import ModelGraph

#: Decorators that change which parameter the first argument lands on.
_STATIC = "staticmethod"
_CLASSMETHOD = "classmethod"


@dataclass(frozen=True)
class Budget:
    """Explicit limits, so the cost of this step is a number and not a hope."""

    max_call_sites: int = 12
    """Beyond this a function is a utility called from everywhere, and the
    odds of every caller agreeing are slim enough that the scan is wasted."""

    max_functions: int = 400
    """Modules larger than this are generated or vendored; the analysis stops
    rather than degrading the whole run for one file."""


@dataclass(frozen=True)
class ParamValue:
    """A parameter resolved to the queryset its callers pass."""

    arg: ast.arg
    function: str
    value: QuerysetValue
    call_sites: int
    """How many calls agreed. One is enough to be unambiguous, and is by far
    the common case for an extracted helper."""


@dataclass
class _Target:
    """A function in this module, with everything needed to bind arguments."""

    node: ast.FunctionDef | ast.AsyncFunctionDef
    scope: Scope
    is_method: bool
    implicit_first: bool
    """True when the first parameter is ``self`` or ``cls`` and is supplied by
    the call rather than written at it."""

    @property
    def positional(self) -> list[ast.arg]:
        args = [*self.node.args.posonlyargs, *self.node.args.args]
        return args[1:] if self.implicit_first else args

    @property
    def by_keyword(self) -> dict[str, ast.arg]:
        named = {a.arg: a for a in self.positional}
        named.update({a.arg: a for a in self.node.args.kwonlyargs})
        return named

    @property
    def takes_star_args(self) -> bool:
        return self.node.args.vararg is not None or self.node.args.kwarg is not None


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    found: set[str] = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            found.add(target.id)
        elif isinstance(target, ast.Attribute):
            found.add(target.attr)
    return found


def _enclosing_class(scope: Scope) -> Scope | None:
    current: Scope | None = scope
    while current is not None:
        if current.kind is ScopeKind.CLASS:
            return current
        current = current.parent
    return None


class _Index:
    """Functions defined in a module, and the calls that reach them."""

    def __init__(self, module: Scope, budget: Budget) -> None:
        self.budget = budget
        self.module = module
        self.functions: dict[int, _Target] = {}
        self.by_name: dict[str, _Target | None] = {}
        self.methods: dict[tuple[int, str], _Target | None] = {}
        self._collect(module)

    def _collect(self, scope: Scope) -> None:
        for child in scope.children:
            if child.kind is ScopeKind.FUNCTION and isinstance(
                child.node, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                self._add(child, scope)
            if child.kind in (ScopeKind.FUNCTION, ScopeKind.CLASS):
                self._collect(child)

    def _add(self, scope: Scope, parent: Scope) -> None:
        if len(self.functions) >= self.budget.max_functions:
            return
        node = scope.node
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        decorators = _decorators(node)
        is_method = parent.kind is ScopeKind.CLASS
        target = _Target(
            node=node,
            scope=scope,
            is_method=is_method,
            implicit_first=is_method and _STATIC not in decorators,
        )
        self.functions[id(node)] = target

        if is_method:
            key = (id(parent.node), node.name)
            # A second definition of the same name means a call could reach
            # either one, so the entry becomes a None that `resolve` reads as
            # "no single target". Storing a flag on the first definition would
            # not work: the call site never looks at the second one.
            self.methods[key] = None if key in self.methods else target
        else:
            self.by_name[node.name] = None if node.name in self.by_name else target

    def resolve(self, call: ast.Call, caller: Scope) -> _Target | None:
        """The function a call reaches, when the module alone can say."""
        func = call.func
        if isinstance(func, ast.Name):
            binding = caller.resolve(func.id)
            if binding is None or binding.kind is not BindingKind.FUNCTION_DEF:
                # A name that is not a function definition here -- an import, a
                # parameter holding a callable, a reassignment. Out of scope.
                return None
            return self.by_name.get(func.id)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in {"self", "cls"}
        ):
            owner = _enclosing_class(caller)
            if owner is None:
                return None
            # Only this class, never a base class: the base may live in another
            # module, and guessing the MRO is how a method gets attributed to
            # a body it does not have.
            return self.methods.get((id(owner.node), func.attr))
        return None


def _bind(target: _Target, call: ast.Call) -> dict[int, ast.expr] | None:
    """Which expression each parameter receives, or ``None`` if unknowable."""
    if target.takes_star_args:
        return None
    if any(isinstance(a, ast.Starred) for a in call.args):
        return None
    if any(k.arg is None for k in call.keywords):
        return None

    positional = target.positional
    if len(call.args) > len(positional):
        return None

    bound: dict[int, ast.expr] = {}
    for param, value in zip(positional, call.args, strict=False):
        bound[id(param)] = value

    named = target.by_keyword
    for keyword in call.keywords:
        assert keyword.arg is not None
        slot = named.get(keyword.arg)
        if slot is None:
            return None
        if id(slot) in bound:
            return None
        bound[id(slot)] = keyword.value
    return bound


def _same_queryset(left: QuerysetValue, right: QuerysetValue) -> bool:
    """Whether two call sites agree closely enough to speak for the parameter.

    The model has to match, and so does what was already fetched: one caller
    passing a prefetched queryset and another passing a bare one is precisely
    the case where a rule must not choose, because one of the two answers is
    a false positive and the other is a false negative.
    """
    return (
        left.model == right.model and left.origin == right.origin and left.methods == right.methods
    )


class _Agreement:
    """What the call sites so far say each parameter holds.

    Kept as its own object because the interesting state is not "the value" but
    "whether anyone has disagreed yet", and a single ``None`` sentinel doing
    both jobs inline is how a disagreement gets read as an absence.
    """

    def __init__(self) -> None:
        self.agreed: dict[int, QuerysetValue | None] = {}
        self.counts: dict[int, int] = {}
        self.owners: dict[int, tuple[ast.arg, str]] = {}

    def contradict(self, param: ast.arg) -> None:
        """A caller that cannot speak for this parameter silences all of them."""
        self.agreed[id(param)] = None

    def offer(self, param: ast.arg, function: str, value: QuerysetValue) -> None:
        key = id(param)
        self.owners[key] = (param, function)
        if key not in self.agreed:
            self.agreed[key] = value
            self.counts[key] = 1
            return
        previous = self.agreed[key]
        if previous is None:
            return
        if _same_queryset(previous, value):
            self.counts[key] += 1
        else:
            self.agreed[key] = None

    def resolved(self) -> dict[int, ParamValue]:
        out: dict[int, ParamValue] = {}
        for key, value in self.agreed.items():
            if value is None or key not in self.owners:
                continue
            arg, function = self.owners[key]
            out[key] = ParamValue(
                arg=arg, function=function, value=value, call_sites=self.counts[key]
            )
        return out


def propagate(
    module: Scope,
    chains: Mapping[int, DefUse],
    graph: ModelGraph,
    *,
    app_label: str | None = None,
    budget: Budget | None = None,
) -> dict[int, ParamValue]:
    """Parameters that the module's own call sites resolve, keyed by ``id`` of
    the :class:`ast.arg` node.

    ``chains`` is the per-scope def-use map from
    :func:`~djaudit.dataflow.chains.def_use_all`, because an argument is an
    expression in the *caller's* scope and has to be read with the caller's
    chains, not the callee's.
    """
    limits = budget or Budget()
    index = _Index(module, limits)
    if not index.functions:
        return {}

    trackers: dict[int, QuerysetTracker] = {}
    agreement = _Agreement()
    seen_calls: dict[int, int] = {}

    def tracker_for(scope: Scope) -> QuerysetTracker | None:
        chain = chains.get(id(scope.node))
        if chain is None:
            return None
        found = trackers.get(id(scope.node))
        if found is None:
            found = QuerysetTracker(scope, chain, graph, app_label=app_label)
            trackers[id(scope.node)] = found
        return found

    for scope in module.walk():
        if scope.kind is ScopeKind.CLASS:
            continue
        # The tracker is built only once a call in this scope is known to reach
        # a function we were asked about. Building one per scope up front is
        # the whole cost of the pass, and on real code most scopes never
        # resolve a single call.
        tracker: QuerysetTracker | None = None
        for call in _calls_in(scope):
            target = index.resolve(call, scope)
            if target is None or target.scope is scope:
                # The last case is recursion: it tells us nothing we do not
                # already have, and following it is how a resolver stops
                # terminating.
                continue
            used = seen_calls.get(id(target.node), 0)
            if used >= limits.max_call_sites:
                continue
            seen_calls[id(target.node)] = used + 1
            if tracker is None:
                tracker = tracker_for(scope)
                if tracker is None:
                    break
            _absorb(agreement, target, call, tracker)

    return agreement.resolved()


def _absorb(
    agreement: _Agreement,
    target: _Target,
    call: ast.Call,
    tracker: QuerysetTracker,
) -> None:
    """Fold one call site's arguments into what is known about the parameters."""
    bound = _bind(target, call)
    if bound is None:
        return
    for param in [*target.positional, *target.node.args.kwonlyargs]:
        expression = bound.get(id(param))
        value = tracker.classify(expression) if expression is not None else None
        if value is None or value.terminal:
            # No value, or one that has left queryset-land. Either way this
            # caller cannot speak for the parameter, and a caller that cannot
            # speak must not be counted as agreeing.
            agreement.contradict(param)
            continue
        agreement.offer(param, target.scope.qualname, value)


def _calls_in(scope: Scope) -> list[ast.Call]:
    """Calls written directly in a scope, not in a nested one."""
    node = scope.node
    found: list[ast.Call] = []

    def walk(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            if isinstance(
                child,
                ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda,
            ):
                continue
            if isinstance(child, ast.Call):
                found.append(child)
            walk(child)

    walk(node)
    return found
