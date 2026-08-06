"""DJP-004 -- a query issued once per iteration of a loop.

DJP-001 and DJP-002 are about a relation the queryset could have fetched: the
loop reads an attribute, and the fix is one more argument on the chain that
produced the rows. This rule is about the other half. Here the loop body starts
a query of its own -- `Author.objects.get(pk=row.author_id)`, or the
`values_list` form DJP-002 declines because prefetching it makes things *worse*
-- and no `select_related` argument can help, because the query is not a
relation being followed. The fix is to stop issuing it per row: hoist it, join
it, or fetch the whole set once and index it in memory.

Keeping the two apart matters for the remediation more than for the detection.
A rule that told you to add `prefetch_related` to a `values_list` in a loop
would be making the code slower, which is exactly the mistake DJP-002's
measured `CACHE_READS` allowlist exists to prevent. This rule takes what that
allowlist excludes.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import TYPE_CHECKING, NamedTuple

from djaudit.dataflow.querysets import TERMINAL, Origin, QuerysetValue, Step
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

if TYPE_CHECKING:
    from djaudit.context import ProjectContext
    from djaudit.dataflow.inventory import LoopSite

FRESH = frozenset({Origin.MANAGER, Origin.DEFAULT_MANAGER})
"""Origins that start a new query rather than reading a fetched relation.

`Model.objects` and `Model._default_manager` cannot come out of any cache: the
row being iterated has no connection to them, so nothing the outer queryset
fetched could serve this. A related accessor is different -- it may already be
prefetched -- which is why those are admitted only when the method excludes
them from the cache.
"""

WRITES = frozenset({"create", "get_or_create", "update_or_create", "update", "delete"})
"""Terminal steps that change data, which belong to DJP-007 (substep 3.3.3).

A write in a loop is also one query per iteration, but its remediation is a
different rule's: `bulk_create`, `bulk_update`, or a single `update()` with an
`F()` expression, none of which are drop-in -- they skip signals and, on some
backends, leave primary keys unset. Reporting reads and writes under one
message would give half of them advice that silently changes behaviour, so the
families are kept disjoint the same way DJP-001 and DJP-002 are.
"""

CHUNKED = frozenset({"bulk_create", "bulk_update", "in_bulk"})
"""Bulk operations, which in a loop are the *fix* rather than the defect.

`for chunk in batched(rows, 500): Model.objects.bulk_create(chunk)` is the
recommended way to insert more rows than fit in one statement, and NetBox and
pretix both write it. A rule that reported it would be telling people to undo
the optimisation they had already made.
"""


def per_iteration(site: LoopSite) -> tuple[ast.AST, ...]:
    """Everything that runs once per row, whichever loop form was written."""
    return (*getattr(site.loop.node, "body", ()), *site.loop.per_iteration)


def evaluated(value: QuerysetValue) -> Step | None:
    """The step that spends a round trip, or `None` if the chain stays lazy.

    A queryset is not a query. `Book.objects.filter(x=1)` inside a loop costs
    nothing on its own -- it is a description that has not been asked for rows
    yet -- and reporting it would be reporting the wrong line. Only a terminal
    step actually goes to the database.

    The `Step` comes back rather than its name because `Step.call` is the node
    where the round trip is *written*, which is the only node that identifies
    one query. `track` also classifies the names a result is bound to, so
    keying on the call is what lets a queryset built above the loop and merely
    read inside it be recognised as the same single round trip.
    """
    for step in value.chain:
        if step.name in CHUNKED or step.name in WRITES:
            return None
        if step.name in TERMINAL and step.call is not None:
            return step
    return None


def per_row(value: QuerysetValue) -> bool:
    """Whether this expression is a fresh query rather than a relation walk.

    The DJP family divides by origin. A traversal off a row already in hand --
    `author.books.count()`, `book.author.name` -- is DJP-002's and DJP-001's,
    because those rules can name the `prefetch_related` or `select_related`
    that fixes it. What is left for this rule is a query that starts over at
    the manager, which no fetch on the outer queryset can serve because its
    parameters change each time round.

    Anything else -- `self.get_queryset()`, a name we could not resolve -- is a
    query too, but the finding could not say which model to fetch instead, so
    it is not reported. Verified against all three benchmark corpora: every one
    of the 83 findings has a manager origin.
    """
    return value.origin in FRESH


class Query(NamedTuple):
    """One round trip written inside a loop body."""

    value: QuerysetValue
    call: ast.Call
    name: str
    """The terminal step that spends the round trip."""


@register
class QueryInLoop(Rule):
    """DJP-004 -- a round trip issued once per iteration."""

    meta = RuleMeta(
        id="DJP-004",
        title="Query executed inside a loop",
        family=Family.DJP,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "A query written inside a loop body runs once per iteration. Unlike a "
            "missing `select_related`, no argument on the outer queryset can fix "
            "it: the loop is not following a relation, it is starting a new query "
            "whose parameters change each time round. The cost is invisible in "
            "development, where the loop runs over five rows, and the same code "
            "issues fifty thousand round trips against production data. Round "
            "trip latency dominates -- each query may be sub-millisecond in the "
            "database and still take the request minutes in aggregate."
        ),
        remediation=(
            "Fetch the whole set once before the loop and index it in memory: "
            "`by_id = Author.objects.in_bulk(ids)`, then read `by_id[row.author_id]` "
            "inside. Where the loop reads a relation of the row, `select_related` "
            "or `prefetch_related` on the queryset that produced the rows is the "
            "shorter fix. Where the query does not depend on the iteration at all, "
            "hoist it above the loop."
        ),
        limitations=(
            "A lazy chain is not reported. `Book.objects.filter(...)` built in a "
            "loop and never evaluated there costs nothing, and the round trip "
            "belongs to whatever consumes it.",
            "Writes are left to DJP-007, whose remediation -- `bulk_create`, "
            "`bulk_update`, a single `update()` -- is not a drop-in replacement "
            "and deserves its own message.",
            "A related-manager read is left to DJP-002 and a forward-relation "
            "read to DJP-001, which can name the fetch that fixes them. This "
            "rule reports only queries that start over at the manager.",
            "A query rooted at `self.get_queryset()` or at a name we cannot "
            "resolve is not reported, because the finding could not say which "
            "model to fetch instead.",
            "Loop bounds are not considered. A loop over three constants issues "
            "three queries, which is reported in the same terms as a loop over a "
            "table.",
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/topics/db/optimization/#retrieve-everything-at-once-if-you-know-you-will-need-it",
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#in-bulk",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for site, deepest in self.sites(ctx).values():
            yield from self.report(ctx, site, deepest)

    def sites(self, ctx: ProjectContext) -> dict[int, tuple[LoopSite, list[Query]]]:
        """Evaluating queries per loop, attributed to the innermost loop only.

        A nested loop's body is part of its parent's body, so every candidate
        would otherwise be reported once per enclosing loop. The innermost is
        the right owner: it is where the query is written, and its `depth`
        already carries how much the nesting multiplies the cost.
        """
        owner: dict[int, tuple[LoopSite, QuerysetValue, ast.Call, str]] = {}
        specs: dict[object, dict[int, QuerysetValue]] = {}
        for site in ctx.loops:
            body = per_iteration(site)
            if not body:
                continue
            key = (site.path, id(site.scope.node))
            if key not in specs:
                specs[key] = self.queries(ctx, site)
            found = specs[key]
            inside = {id(child) for node in body for child in ast.walk(node)}
            for node in body:
                for child in ast.walk(node):
                    value = found.get(id(child))
                    if value is None:
                        continue
                    step = evaluated(value)
                    if step is None or step.call is None or not per_row(value):
                        continue
                    if id(step.call) not in inside:
                        # The round trip is written outside this loop; only the
                        # name that holds its result is read inside.
                        continue
                    seen = owner.get(id(step.call))
                    if seen is None or site.loop.depth > seen[0].loop.depth:
                        owner[id(step.call)] = (site, value, step.call, step.name)
        out: dict[int, tuple[LoopSite, list[Query]]] = {}
        for site, value, call, name in owner.values():
            entry = out.setdefault(id(site), (site, []))
            entry[1].append(Query(value, call, name))
        return out

    def queries(self, ctx: ProjectContext, site: LoopSite) -> dict[int, QuerysetValue]:
        """Every queryset expression in the loop's own scope."""
        return ctx.tracked(site.path, site.scope)

    def report(self, ctx: ProjectContext, site: LoopSite, found: list[Query]) -> Iterator[Finding]:
        # Every reported value has a manager origin, and `_from_manager` only
        # builds one after resolving the model, so the model is always known
        # and the rule is always firm. The fallback below is for the type
        # checker, not for a case that can arise.
        for value, call, name in sorted(found, key=lambda q: q.call.lineno):
            model = value.model or "an unresolved model"
            depth = site.loop.depth
            times = "once per row" if depth == 1 else f"once per row of {depth} nested loops"
            yield self.finding(
                location=ctx.location(site.path, call),
                message=(
                    f"`{ast.unparse(call)}` runs `{name}()` {times} of the "
                    f"loop at line {site.loop.lineno}, issuing one query per "
                    f"iteration against `{model}`."
                ),
                severity=Severity.CRITICAL if depth > 1 else None,
                evidence=(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"loop at line {site.loop.lineno} (depth {depth})\n"
                            f"query: {ast.unparse(call)}\n"
                            f"origin: {value.origin} on {model}\n"
                            f"evaluated by: {name}()"
                        ),
                        source=str(site.path),
                    ),
                ),
                properties={
                    "loop_line": str(site.loop.lineno),
                    "depth": str(depth),
                    "step": name,
                    "model": value.model or "",
                },
            )
