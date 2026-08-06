"""Counting idioms: `DJP-005` on `len(queryset)` and `DJP-006` on `.count()`.

Both rules are about asking the database for a number and paying more than the
number is worth, and both are mostly about what they decline to report, because
each idiom is correct more often than it is wrong.

`len(qs)` evaluates the queryset, populating its result cache, so code that
counts the rows *and then reads them* is right to call it: one query for both
beats `.count()` plus an iteration, which is two. The mistake is narrower --
counting rows nobody looks at. `SELECT *` over a million rows, every column
deserialised into a model instance, so that the answer can be thrown away and
an integer kept. So `DJP-005` reports only the cases where the rows provably
cannot be read again: the queryset written inline inside the `len()`, which no
name holds, and the queryset held by a name that is loaded exactly once in its
scope.

`.count()` is right whenever the number itself is wanted. `DJP-006` reports it
only where the number is immediately thrown away for a yes-or-no answer, which
`.exists()` gives with `LIMIT 1`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import TYPE_CHECKING, NamedTuple, TypeGuard

from djaudit.dataflow.chaining import analyse
from djaudit.dataflow.querysets import Origin, QuerysetValue
from djaudit.dataflow.scopes import Scope
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Severity,
    Tier,
)
from djaudit.registry import Rule, RuleMeta, register
from djaudit.rules.performance import MULTI_VALUED_REVERSE

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.graph.nodes import ModelGraph

FRESH = frozenset({Origin.MANAGER, Origin.DEFAULT_MANAGER})
"""Origins whose rows cannot already be in memory.

A related accessor is excluded, and measurably so: `scripts/prefetch_cache_probe.py`
counts `len(vm.interfaces.all())` at 2 queries under `prefetch_related`, the
same as `.count()`. There the author has already paid for the rows and `len()`
reads them out of the prefetch cache -- advising `.count()` would be advice to
change nothing. `Model.objects` has no such cache to read.
"""


class Count(NamedTuple):
    """One `len()` whose rows are never read."""

    call: ast.Call
    value: QuerysetValue
    name: str | None
    """The variable holding the queryset, or `None` when written inline."""

    compared_to_zero: bool
    """The count is only ever tested for emptiness, so `.exists()` is better."""


def is_len_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """Whether `node` is a one-argument `len(x)`.

    The arity check is not defensive: `len()` and `len(a, b)` are `TypeError`
    at runtime but parse cleanly, so a rule that read `args[0]` unguarded would
    crash on a file it should have ignored.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
        and not node.keywords
    )


def loads(scope: Scope, name: str) -> int:
    """How many times `name` is read in `scope`'s own node."""
    return sum(
        1
        for node in ast.walk(scope.node)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    )


def zero_test(parent: ast.AST | None) -> bool:
    """Whether the enclosing expression only asks whether the count is zero."""
    if isinstance(parent, ast.Compare) and len(parent.ops) == 1:
        other = parent.comparators[0]
        if isinstance(other, ast.Constant) and other.value == 0:
            return isinstance(parent.ops[0], ast.Eq | ast.NotEq | ast.Gt | ast.LtE)
    return isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not)


@register
class LenOfQueryset(Rule):
    """DJP-005 -- counting rows by fetching them."""

    meta = RuleMeta(
        id="DJP-005",
        title="`len()` on a queryset whose rows are never read",
        family=Family.DJP,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "`len(qs)` evaluates the queryset. Django issues `SELECT <every "
            "column> FROM <table>`, streams every matching row back, and builds "
            "a model instance for each one, so that `len()` can return the "
            "length of the list. When the rows are then read, that is the right "
            "trade -- the cache is populated and a separate `.count()` would be "
            "a second query. When they are not, the entire result set has been "
            "transferred and deserialised to produce one integer that "
            "`SELECT COUNT(*)` would have computed in the database. The cost "
            "scales with the table, not with the answer, and memory scales with "
            "it too: a count of a million rows holds a million model instances."
        ),
        remediation=(
            "Use `.count()`, which compiles to `SELECT COUNT(*)` and returns "
            "the integer without materialising anything. Where the count is "
            "only compared against zero, `.exists()` is better still: it adds "
            "`LIMIT 1` and stops at the first row instead of counting all of "
            "them. Keep `len()` only where the same rows are iterated "
            "afterwards, since there it saves a query rather than costing one."
        ),
        limitations=(
            "A queryset whose name is read more than once is never reported, "
            "even when the other reads cannot use the cache. That is deliberate: "
            "distinguishing a second read that reuses the rows from one that "
            "discards them needs more than a name count, and the safe direction "
            "for a rule about a correct idiom is silence.",
            "A related accessor -- `author.books` -- is not reported, because it "
            "may already be prefetched, in which case `len()` costs nothing and "
            "`.count()` would not be an improvement.",
            "A sliced queryset is not reported. `len(qs[:10])` fetches at most "
            "ten rows, so the cost does not scale with the table.",
            "Row size is not modelled. `len()` over five rows and over five "
            "million are reported in the same terms.",
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#count",
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#exists",
            "https://docs.djangoproject.com/en/stable/topics/db/optimization/#don-t-retrieve-things-you-don-t-need",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or "len" not in source:
                # A `len(...)` call needs the token `len` in the source, so
                # this skips the AST walk on a clear majority of files with no
                # false negative: 747 of pretix's 1225, where walking all of
                # them was measured at 9.3s of the rule's 26.3s under cProfile.
                continue
            tree = ctx.parse(path)
            if tree is None:
                continue
            for found in self.counts(ctx, path, tree):
                yield self.report(ctx, path, found)

    def counts(self, ctx: ProjectContext, path: Path, tree: ast.Module) -> Iterator[Count]:
        """Every discarded `len()` in the module, resolved scope by scope.

        `def_use` deliberately stops at a nested scope, so tracking the module
        alone would see `Book.objects.all()` inside a function but never learn
        that `books` refers to it. Each `len()` is therefore read against the
        innermost scope containing it -- the one whose bindings its argument
        could name -- and only the scopes that own one are tracked at all.
        Tracking every scope of every file instead was measured at 43s on
        NetBox against a 20s budget, because each `track` call walks its whole
        subtree and nested scopes walk the same nodes again.

        One descent does all three jobs -- find the `len()` calls, record each
        one's parent expression, and label it with its innermost scope -- since
        a separate `ast.walk` to find candidates first was measured at 1.4s of
        pretix's run, walking 478 files to learn that only 206 held a call.
        """
        root = ctx.scopes(path)
        if root is None:
            return
        scopes = {id(scope.node): scope for scope in root.walk()}
        wanted: dict[int, ast.Call] = {}
        owner: dict[int, Scope] = {}
        parents: dict[int, ast.AST] = {}
        stack: list[tuple[ast.AST, Scope]] = [(tree, root)]
        while stack:
            node, scope = stack.pop()
            current = scopes.get(id(node), scope)
            for child in ast.iter_child_nodes(node):
                if is_len_call(child):
                    wanted[id(child)] = child
                    owner[id(child)] = current
                    parents[id(child)] = node
                stack.append((child, current))
        if not wanted:
            return

        tracked: dict[int, dict[int, QuerysetValue]] = {}
        for key, call in wanted.items():
            scope = owner[key]
            if id(scope.node) not in tracked:
                tracked[id(scope.node)] = ctx.tracked(path, scope)
            value = tracked[id(scope.node)].get(id(call.args[0]))
            if value is None or value.origin not in FRESH or value.terminal:
                continue
            if analyse(value).sliced:
                continue
            discarded, name = self.discarded(value, scopes)
            if not discarded:
                continue
            yield Count(call, value, name, zero_test(parents.get(key)))

    def discarded(self, value: QuerysetValue, scopes: dict[int, Scope]) -> tuple[bool, str | None]:
        """Whether the rows are unreachable after the call, and what held them.

        A queryset written inside the `len()` is bound to nothing, so its rows
        are unreachable the moment the call returns. A queryset reached through
        a name is only safe to report when that name is read exactly once,
        which is this call.
        """
        if not value.indirect:
            return True, None
        binding = value.via[0]
        for scope in scopes.values():
            if any(binding is bound for bound in scope.bindings.get(binding.name, ())):
                return loads(scope, binding.name) == 1, binding.name
        # Unreachable: every binding in `via` was recorded by `build_scopes`
        # on this same tree, so some scope in `scopes` holds it. Declining is
        # the safe answer if that ever stops being true.
        return False, None

    def report(self, ctx: ProjectContext, path: Path, found: Count) -> Finding:
        call, value, name, exists = found
        held = f"`{name}` holds " if name else ""
        better = ".exists()" if exists else ".count()"
        why = (
            "the count is only compared against zero"
            if exists
            else ("the name is read nowhere else" if name else "no name holds the result")
        )
        return self.finding(
            location=ctx.location(path, call),
            message=(
                f"`{ast.unparse(call)}` fetches every row of "
                f"`{value.model or 'the queryset'}` to count them, and {why}, so "
                f"the rows are never read. Use `{better}`."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"{ast.unparse(call)}\n"
                        f"{held}origin: {value.origin} on {value.model or 'an unresolved model'}\n"
                        f"chain: {'.'.join(value.methods) or '(none)'}\n"
                        f"suggested: {better}"
                    ),
                    source=str(path),
                ),
            ),
            properties={
                "model": value.model or "",
                "suggested": better,
                "binding": name or "",
            },
        )


EMPTY = frozenset({("Eq", 0), ("LtE", 0), ("Lt", 1)})
"""Comparisons that ask whether a count is zero."""

NONEMPTY = frozenset({("Gt", 0), ("NotEq", 0), ("GtE", 1)})
"""Comparisons that ask whether a count is non-zero."""


class Emptiness(NamedTuple):
    """A `.count()` whose value is discarded for a yes-or-no answer."""

    call: ast.Call
    holder: ast.expr
    """The expression the count is buried in -- what the fix replaces."""

    negated: bool
    """The question is "is it empty", so the fix reads `not ....exists()`."""

    model: str | None
    """`None` when the receiver is a relation the graph names but cannot type."""


def multi_valued_names(graph: ModelGraph) -> frozenset[str]:
    """Every attribute name in the project that yields many related rows.

    Both directions answer to one name. Forward is a relation the model
    declares; reverse is the accessor Django adds to somebody else's target,
    which is why it is read out of the incoming index. Collected across the
    whole graph rather than per model because the receiver this is asked about
    -- `ctx['item'].bundled_with` -- is an expression whose type is not
    knowable, so the question is only ever "is this a relation name at all".
    """
    names = {
        edge.accessor
        for edges in graph.incoming.values()
        for edge in edges
        if edge.accessor and edge.kind in MULTI_VALUED_REVERSE
    }
    names.update(
        edge.field_name for model in graph for edge in model.relations if edge.is_multi_valued
    )
    return frozenset(names)


def asks_emptiness(parent: ast.AST, node: ast.expr) -> tuple[ast.expr, bool] | None:
    """The expression testing `node` for emptiness, and whether it means empty.

    Returns `None` when the count's value is used as a number -- compared
    against a real bound, assigned, returned, formatted, or added to something
    -- since all of those need the number that `.exists()` cannot give.
    """
    if isinstance(parent, ast.Compare) and len(parent.ops) == 1:
        # A chained comparison is excluded because only part of it would be
        # replaced: `qs.count() == 0 == n` also asserts `0 == n`, which
        # `not qs.exists()` silently drops.
        other = parent.comparators[0]
        if isinstance(other, ast.Constant):
            key = (type(parent.ops[0]).__name__, other.value)
            if key in EMPTY:
                return parent, True
            if key in NONEMPTY:
                return parent, False
        return None
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return parent, True
    if isinstance(parent, ast.If | ast.While | ast.IfExp) and parent.test is node:
        return node, False
    if isinstance(parent, ast.comprehension):
        return (node, False) if any(test is node for test in parent.ifs) else None
    if isinstance(parent, ast.BoolOp) and parent.values[-1] is not node:
        # Only the last operand's *value* survives a `and`/`or`; the others are
        # consumed as truth values, so `qs.count() and x` discards the number
        # while `x and qs.count()` returns it.
        return node, False
    return None


def is_count_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """Whether `node` is a no-argument `obj.count()`.

    The arity matters for more than crash-safety: `str.count` and `list.count`
    both *require* an argument, so demanding none of them is what keeps this
    rule off every non-Django `.count(x)` in a project.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "count"
        and not node.args
        and not node.keywords
    )


@register
class CountForEmptiness(Rule):
    """DJP-006 -- counting every row to find out whether there is one."""

    meta = RuleMeta(
        id="DJP-006",
        title="`.count()` used only to test whether rows exist",
        family=Family.DJP,
        severity=Severity.LOW,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "`.count()` compiles to `SELECT COUNT(*)`, which the database "
            "answers by visiting every row that matches the filter. "
            "`.exists()` compiles to `SELECT 1 ... LIMIT 1`, which stops at "
            "the first one. Both are a single query, so the cost does not show "
            "up as a query count -- it shows up in the statement, and it grows "
            "with the size of the table while the answer stays one bit. On a "
            "filtered scan without a covering index the difference is the "
            "whole table against one row."
        ),
        remediation=(
            "Replace the comparison with `.exists()`, or `not ....exists()` "
            "where the question is whether the set is empty. Keep `.count()` "
            "wherever the number itself is used -- shown to a user, compared "
            "against a real bound, or reported in an assertion failure."
        ),
        limitations=(
            "A related accessor may already be prefetched, in which case both "
            "forms read the prefetch cache and cost the same. That case is "
            "reported at `tentative` rather than excluded, because `.exists()` "
            "is measurably never worse than `.count()` here: equal under "
            "`prefetch_related` and cheaper without it.",
            "A `.count()` inside an `assert` is never reported. The number is "
            "what the failure message shows, and an assertion that a set is "
            "empty is cheapest exactly when it passes, since there are no rows "
            "to count. This is why the rule is silent on test suites, which is "
            "where the idiom overwhelmingly occurs.",
            "A count bound to a name and only then compared is not reported; "
            "the rule reads the expression the call is written in, not the "
            "later uses of a variable.",
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#exists",
            "https://docs.djangoproject.com/en/stable/topics/db/optimization/#don-t-retrieve-things-you-don-t-need",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        relations: frozenset[str] | None = None
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or ".count()" not in source:
                # `.count()` written with no argument is the only shape this
                # reports, and it cannot appear in a file whose text lacks it.
                continue
            tree = ctx.parse(path)
            if tree is None:
                continue
            if relations is None:
                relations = multi_valued_names(ctx.model_graph)
            for found in self.emptiness_tests(ctx, path, tree, relations):
                yield self.report(ctx, path, found)

    def emptiness_tests(
        self, ctx: ProjectContext, path: Path, tree: ast.Module, relations: frozenset[str]
    ) -> Iterator[Emptiness]:
        """Every `.count()` in the module whose number is thrown away.

        Walked parent-first so the enclosing expression is in hand when the
        call is reached; `ast.walk` alone gives no parent, and the parent is
        the entire question. The same descent records the tests of `assert`
        statements, which is sound because a stack descent always pops a node
        before its children, so an `assert` is seen before the count buried
        inside it is examined.
        """
        root = ctx.scopes(path)
        if root is None:
            return
        scopes = {id(scope.node): scope for scope in root.walk()}
        asserted: set[int] = set()
        stack: list[tuple[ast.AST, Scope]] = [(tree, root)]
        while stack:
            node, scope = stack.pop()
            current = scopes.get(id(node), scope)
            if isinstance(node, ast.Assert):
                asserted.add(id(node.test))
            for child in ast.iter_child_nodes(node):
                stack.append((child, current))
                if not is_count_call(child):
                    continue
                asked = asks_emptiness(node, child)
                if asked is None:
                    continue
                holder, negated = asked
                if id(holder) in asserted:
                    # Measured, and it is the difference between a rule that
                    # reports one thing and one that reports eighty-three: of
                    # every `.count()` emptiness test across Healthchecks,
                    # NetBox and pretix, all but one is
                    # `assert Model.objects.count() == 0` in a test suite.
                    continue
                counts, model = self.receiver(ctx, path, current, child, relations)
                if not counts:
                    continue
                yield Emptiness(child, holder, negated, model)

    def receiver(
        self,
        ctx: ProjectContext,
        path: Path,
        scope: Scope,
        call: ast.Call,
        relations: frozenset[str],
    ) -> tuple[bool, str | None]:
        """Whether the count is over rows, and which model's if that is known.

        Declining is the answer for every receiver the tracker cannot resolve
        and the model graph does not name. Without that the rule would report
        any object in the project exposing a no-argument `.count()`.
        """
        value = ctx.tracked(path, scope).get(id(call))
        if value is not None and value.origin in FRESH:
            return True, value.model
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
            # `ctx['item'].bundled_with.count()` -- a receiver no tracker can
            # type, ending in an attribute the project declares as a relation,
            # which is enough to know that rows are being counted.
            return func.value.attr in relations, None
        return False, None

    def report(self, ctx: ProjectContext, path: Path, found: Emptiness) -> Finding:
        call, holder, negated, model = found
        receiver = call.func.value if isinstance(call.func, ast.Attribute) else call.func
        fix = f"{ast.unparse(receiver)}.exists()"
        better = f"not {fix}" if negated else fix
        subject = f"every row of `{model}`" if model else "every related row"
        return self.finding(
            location=ctx.location(path, holder),
            confidence=Confidence.FIRM if model else Confidence.TENTATIVE,
            message=(
                f"`{ast.unparse(holder)}` counts {subject} to learn whether "
                f"there are any. Use `{better}`, which stops at the first row."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"{ast.unparse(holder)}\n"
                        f"counts: {model or 'a relation the graph names but cannot type'}\n"
                        f"emitted: SELECT COUNT(*) with no bound\n"
                        f"suggested: {better}  (SELECT 1 ... LIMIT 1)"
                    ),
                    source=str(path),
                ),
            ),
            properties={"model": model or "", "suggested": better},
        )
