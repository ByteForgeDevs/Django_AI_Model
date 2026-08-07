"""DJP-009 -- the optimisation that became the defect.

`only()` and `defer()` are written by someone who is already thinking about
query cost. That is what makes this rule different from the rest of the family:
it does not report carelessness, it reports an optimisation that a *later* edit
outgrew. The `only('name')` was right when it was written; the `obj.email` two
releases further down was added by someone who had no reason to look at the
queryset, and Django says nothing -- the attribute resolves, the tests pass,
and the page issues one extra query per row forever.

Measured on twenty rows, in `scripts/prefetch_cache_probe.py`: reading a field
`only()` loaded costs **1** query, and reading one it left out costs **21**.

Three things that look like the defect and are not, each established by
measurement rather than by argument, and each of which the first version of
this rule got wrong:

- **Assigning an excluded field costs nothing.** `obj.email = x` does not load
  the column, because nothing needs the old value. Counting a write as a read
  is what made the first probe flag a netbox migration that only ever assigns.
- **A rebound name is a different object.** `for o in qs.only(...)` followed by
  `o = Model.objects.get(pk=o.pk)` leaves every later read on a fully loaded
  instance. That single shape accounted for ten of the first probe's eleven
  hits; both loops were in the same pretix module.
- **The primary key is always loaded**, whatever `only()` says.

The rule found nothing on healthchecks, netbox or pretix -- all fourteen
restricted loops and all seven restricted view querysets in those three
projects are correct, and netbox's `defer('data')` is paired with a serializer
that deliberately omits `data`. It is shipped as a regression guard rather than
on the strength of a corpus finding, which is the honest description of a rule
whose target population is code written by people already being careful.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Severity,
    Tier,
)
from djaudit.registry import RuleMeta, register
from djaudit.rules.performance import LoopRule, Rows, accesses
from djaudit.rules.serializer_performance import queryset_expressions, specs_in

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.api.serializers import SerializerNode
    from djaudit.api.views import ViewNode
    from djaudit.context import ProjectContext
    from djaudit.dataflow.chaining import ChainSpec
    from djaudit.graph.nodes import FieldNode, ModelGraph

RESTRICTORS = (".only(", ".defer(")
"""Text prefilter. Only 44 call sites exist across the three benchmark
corpora, so without this the rule would pay for 3,091 files to speak about
fourteen loops."""


def restricted(spec: ChainSpec) -> tuple[frozenset[str], frozenset[str]] | None:
    """What the chain guarantees is loaded and what it dropped.

    `None` when the chain says nothing usable: rows that are not model
    instances, or a restriction we could not read statically. An *absent*
    restriction is not `None` but a pair of empty sets, which `missing_field`
    already reads as "nothing was deferred" -- collapsing the two would have
    made `agreed` treat an unrestricted branch as agreeing with a restricted
    one.

    Names are compared as written, including join paths. `only('author__name')`
    is left whole because the only names containing `__` are relations, and a
    relation is declined before the comparison could matter.
    """
    if spec.unreadable or not spec.yields_instances:
        return None
    return spec.only, spec.defer


def missing_field(attr: str, loaded: frozenset[str], dropped: frozenset[str]) -> bool:
    """Whether reading `attr` would go back to the database.

    `only()` and `defer()` are asymmetric on purpose: `only` is a whitelist, so
    anything absent from it is deferred, while `defer` is a blacklist and says
    nothing about names it does not mention.
    """
    return (bool(loaded) and attr not in loaded) or attr in dropped


def reads(body: Iterable[ast.AST], name: str) -> Iterator[tuple[str, ast.Attribute]]:
    """Direct attribute *reads* on the row, one per attribute.

    Only the *first* hop off the row is considered, since that is the column
    the restriction speaks about: `o.title.upper()` reads the deferred `title`
    just as surely as `o.title` does, and requiring a single-segment chain
    would have missed it. Where the first hop is a relation the read belongs to
    DJP-001 instead, which `concrete_field` decides.

    Writes are excluded because assigning a deferred field is measurably free,
    and an attribute written anywhere in the body is excluded entirely -- once
    assigned it is resident, so a later read costs nothing. That set is the
    only write test needed: a single-hop store puts the column in it, while
    `o.title.attr = x` is a genuine *read* of `title`, since the column has to
    be loaded before anything can be set on it. A write to some other object's
    `title` says nothing about this row and is not collected.
    """
    written = {
        node.attr
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Attribute)
        and not isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id == name
    }
    seen: set[str] = set()
    for node, parts in accesses(tuple(body), name):
        attr = parts[0]
        if attr in written or attr in seen:
            continue
        seen.add(attr)
        yield attr, node


def columns(graph: ModelGraph, model: str) -> dict[str, FieldNode]:
    """Every stored column the model answers to, parents included.

    Abstract bases arrive in `inherited` already, but multi-table inheritance
    does not: a child's `inherited` holds only the `parent_ptr`, and the
    parent's own columns are reachable only by walking `mro`. Both forms were
    measured to reload -- `only('year')` on a multi-table child costs 11
    queries for 10 rows when a parent column is read -- so both are collected.
    """
    found: dict[str, FieldNode] = {}
    seen: set[str] = set()
    pending = [model]
    while pending:
        label = pending.pop()
        if label in seen:
            continue
        seen.add(label)
        node = graph.get(label)
        if node is None:
            continue
        found = {**node.inherited, **node.fields, **found}
        pending.extend(node.mro)
    return found


def concrete_field(graph: ModelGraph, model: str, attr: str) -> bool:
    """Whether `attr` is a stored column that `only()` could have named.

    Relations are excluded because their cost is a join, not a reload, and the
    primary key because it is loaded whatever the queryset asked for.
    """
    field = columns(graph, model).get(attr)
    return field is not None and not field.is_relation and not field.primary_key


def agreed(specs: list[ChainSpec]) -> tuple[frozenset[str], frozenset[str]] | None:
    """The restriction every path through a view's queryset applies.

    A `get_queryset` with two returns only defers a column if *both* of them
    do; a branch that forgot the `only()` loads everything and makes the read
    free. So this insists the paths agree exactly rather than intersecting,
    because the intersection of `only('a')` with no restriction at all is not
    `only('a')` -- it is no restriction, and treating it as the former is how a
    rule invents a finding out of a branch that is already correct.

    A branch whose restriction could not be read is `None`, and `None` equals
    only itself, so this same comparison rejects it without a separate guard.
    """
    if not specs:
        return None
    limits = [restricted(spec) for spec in specs]
    first = limits[0]
    return first if all(limit == first for limit in limits) else None


def exposed(serializer: SerializerNode, graph: ModelGraph) -> frozenset[str]:
    """Model columns the serializer reads for every row it renders.

    A declared field with a `source` is followed, since `email = CharField(
    source='contact_email')` reads a column whose name never appears in
    `fields`. Anything the serializer could not be read statically makes this
    empty rather than partial -- a half-known field set would report the half
    it happened to parse.
    """
    if serializer.unreadable or serializer.model is None:
        return frozenset()
    stored = columns(graph, serializer.model)
    if not stored:
        return frozenset()
    if serializer.mode == "all":
        names = set(stored)
    elif serializer.mode == "exclude":
        names = set(stored) - set(serializer.exclude)
    elif serializer.mode == "explicit":
        names = set(serializer.fields)
    else:
        return frozenset()
    for name, declared in serializer.declared.items():
        if declared.source and name in names:
            names.discard(name)
            names.add(declared.source)
    return frozenset(names)


def describe(loaded: frozenset[str], dropped: frozenset[str]) -> str:
    """The restriction as the reader would have written it."""
    if loaded:
        return f"only({', '.join(repr(n) for n in sorted(loaded))})"
    return f"defer({', '.join(repr(n) for n in sorted(dropped))})"


@register
class DeferredFieldRead(LoopRule):
    """DJP-009 -- a field the queryset was told not to fetch."""

    meta = RuleMeta(
        id="DJP-009",
        title="Field read on a queryset that deferred it",
        family=Family.DJP,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "`only()` and `defer()` do not make a column unavailable -- they make it "
            "expensive. Reading a deferred field issues a fresh `SELECT` for that one "
            "column on that one row, so a loop over a restricted queryset costs one "
            "query per row: measured at 21 queries for 20 rows against 1 when the "
            "field was loaded. Nothing in the source marks the read as different from "
            "any other attribute access, and no exception is raised, which is why this "
            "survives review and testing. It is also self-inflicted in a particular "
            "way: the restriction was added deliberately to save work, and the read "
            "that defeats it is usually written later by someone who never saw the "
            "queryset. The net result is slower than having written no `only()` at all."
        ),
        remediation=(
            "Add the field to `only()`, or drop it from `defer()`. If the field is "
            "large and genuinely wanted only sometimes -- a text blob or a JSON "
            "document -- keep the restriction and fetch the column separately for the "
            "rows that need it, rather than reading it per row. If the restriction no "
            "longer earns its keep, remove it: an unrestricted query that fetches one "
            "extra column once beats a restricted one that fetches it N times."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#only",
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#defer",
        ),
        limitations=(
            "Reports only reads written directly in the loop body. A deferred field "
            "read inside a function the loop calls, or in a template rendered from "
            "these rows, costs exactly the same and is not reported.",
            "Declines any loop that rebinds the row variable, because a name "
            "reassigned mid-body no longer refers to a row of the restricted "
            "queryset. This is deliberately conservative and will hide a genuine "
            "defect written before the rebinding.",
            "Assignment to a deferred field is not reported, having been measured to "
            "cost no query, and an attribute assigned anywhere in the body is ignored "
            "entirely because it is resident from that point on.",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        """Both shapes: the loop the reader can see, and the one DRF hides.

        The view half matters more than the loop half in practice. A restricted
        queryset on a `ModelViewSet` is read once per row of every page, by a
        serializer in a different file that has no reason to mention the
        restriction -- which is exactly netbox's `DataFileViewSet`, correct
        today because its serializer omits `data`, and one `fields` edit away
        from a reload on every row it returns.
        """
        yield from super().check(ctx)
        yield from self.views(ctx)

    def views(self, ctx: ProjectContext) -> Iterator[Finding]:
        surface = ctx.api_surface
        index = surface.index
        if index is None:
            return
        graph = ctx.model_graph
        cache: dict[Path, dict[int, ChainSpec]] = {}
        for view in surface.views.values():
            source = ctx.source(view.path)
            if source is None or not any(marker in source for marker in RESTRICTORS):
                continue
            if view.serializer_ref is None:
                continue
            record = index.lookup(view.label)
            if record is None:
                continue
            if view.path not in cache:
                cache[view.path] = specs_in(ctx, view.path)
            found = cache[view.path]
            limits = agreed(
                [found[id(e)] for e in queryset_expressions(record.node) if id(e) in found]
            )
            if limits is None:
                continue
            target = index.lookup(index.resolve_name(view.module, view.serializer_ref))
            serializer = surface.get(target.dotted) if target is not None else None
            if serializer is None or serializer.model is None:
                continue
            yield from self.serialized(ctx, view, record.node, serializer, limits, graph)

    def serialized(  # noqa: PLR0913, PLR0917
        self,
        ctx: ProjectContext,
        view: ViewNode,
        node: ast.ClassDef,
        serializer: SerializerNode,
        limits: tuple[frozenset[str], frozenset[str]],
        graph: ModelGraph,
    ) -> Iterator[Finding]:
        loaded, dropped = limits
        restriction = describe(loaded, dropped)
        for attr in sorted(exposed(serializer, graph)):
            if not missing_field(attr, loaded, dropped):
                continue
            if not concrete_field(graph, serializer.model or "", attr):
                continue
            yield self.finding(
                location=ctx.location(view.path, node),
                message=(
                    f"`{view.name}` restricts its queryset with `{restriction}` but "
                    f"`{serializer.name}` serialises `{attr}`, so every row of every "
                    f"page costs an extra query."
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"{view.label} queryset: `{restriction}`\n"
                            f"{serializer.name} reads `{serializer.model}.{attr}`"
                        ),
                        source=str(view.path),
                    ),
                ),
            )

    def inspect(self, ctx: ProjectContext, rows: Rows) -> Iterator[Finding]:
        source = ctx.source(rows.path)
        if source is None or not any(marker in source for marker in RESTRICTORS):
            return
        limits = restricted(rows.spec)
        if limits is None:
            return
        loaded, dropped = limits
        for attr, node in reads(rows.body, rows.name):
            if not missing_field(attr, loaded, dropped):
                continue
            if not concrete_field(ctx.model_graph, rows.model, attr):
                continue
            restriction = describe(loaded, dropped)
            yield self.finding(
                location=ctx.location(rows.path, node),
                message=(
                    f"`{ast.unparse(node)}` reads a field the queryset deferred; "
                    f"`{restriction}` left it out, so each row costs an extra query."
                ),
                confidence=None if rows.spec.confident else Confidence.TENTATIVE,
                severity=Severity.HIGH if rows.repeated else None,
                evidence=(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"{rows.origin}\n`{restriction}` does not fetch `{rows.model}.{attr}`"
                        ),
                        source=str(rows.path),
                    ),
                ),
            )
