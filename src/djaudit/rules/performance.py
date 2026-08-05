"""Performance and ORM efficiency rules.

The family exists because the ORM makes the expensive thing and the cheap thing
look identical. `book.author.name` is an attribute access whether the author
was fetched with the book or costs a round trip, and the source gives no
indication which. The database does, in production, at scale, on the day the
table grows.

Every rule here reports a *query count*, not a slow query. A slow query shows
up in monitoring with its own SQL attached; a thousand fast ones show up as a
view that got slower for no visible reason.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass

from djaudit.context import ProjectContext
from djaudit.dataflow.chaining import ChainSpec
from djaudit.dataflow.inventory import LoopSite
from djaudit.graph.nodes import ModelGraph, RelationEdge
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

FORWARD = frozenset({"ForeignKey", "OneToOneField"})
"""Relations whose column lives on the row being iterated, so `select_related`
can fetch them in the same query with a join. A `ManyToManyField` cannot be
joined into one row and needs `prefetch_related`, which is DJP-002."""


def relations_of(graph: ModelGraph, label: str) -> dict[str, RelationEdge]:
    """Forward relations declared or inherited by a model, by attribute name."""
    node = graph.get(label)
    if node is None:
        return {}
    return {edge.field_name: edge for edge in node.relations}


def attribute_path(node: ast.Attribute, root: str) -> list[str] | None:
    """The attribute names in ``root.a.b.c``, or ``None`` if not rooted there.

    Written as a loop rather than recursion because the nesting is the wrong
    way round: `a.b.c` parses as `Attribute(Attribute(Name))`, so the names
    come out innermost first and have to be reversed.
    """
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name) or current.id != root:
        return None
    parts.reverse()
    return parts


@dataclass(frozen=True)
class Rows:
    """A loop over rows of a known model, and what its queryset fetched."""

    site: LoopSite
    model: str
    name: str
    """The variable each row is bound to."""

    spec: ChainSpec


@dataclass(frozen=True)
class Traversal:
    """A relation path walked on a row, and the edges it crosses."""

    steps: tuple[str, ...]
    edges: tuple[RelationEdge, ...]

    @property
    def path(self) -> str:
        """The lookup a `select_related` argument would have to name."""
        return "__".join(self.steps)

    def __bool__(self) -> bool:
        return bool(self.steps)


def traversal(graph: ModelGraph, model: str, parts: list[str]) -> Traversal:
    """The longest prefix of ``parts`` that walks forward relations.

    `b.author.publisher.name` returns `["author", "publisher"]`: `name` is a
    column on the publisher row, not another hop.
    """
    steps: list[str] = []
    edges: list[RelationEdge] = []
    current: str | None = model
    for part in parts:
        if current is None:
            break
        edge = relations_of(graph, current).get(part)
        if edge is None or edge.kind not in FORWARD:
            break
        steps.append(part)
        edges.append(edge)
        current = edge.target
    return Traversal(tuple(steps), tuple(edges))


def reassigned(site: LoopSite, name: str) -> bool:
    """Whether the loop body rebinds the element name.

    `for b in books: b = b.parent` means the attribute read later is not on a
    row of the queryset at all, and the chain says nothing about it.
    """
    for node in _per_iteration(site):
        for child in ast.walk(node):
            targets: list[ast.expr] = []
            if isinstance(child, ast.Assign):
                targets = list(child.targets)
            elif isinstance(child, ast.AugAssign | ast.AnnAssign | ast.For | ast.AsyncFor):
                targets = [child.target]
            for target in targets:
                for named in ast.walk(target):
                    if isinstance(named, ast.Name) and named.id == name:
                        return True
    return False


def _per_iteration(site: LoopSite) -> tuple[ast.AST, ...]:
    """Everything that runs once per row, whichever loop form was written."""
    return (*site.loop.body, *site.loop.per_iteration)


def accesses(site: LoopSite, root: str) -> Iterator[tuple[ast.Attribute, list[str]]]:
    """Attribute chains on the element, outermost first.

    Only the longest chain at each site is yielded: `b.author.name` contains
    `b.author` as a sub-expression, and reporting both would charge one query
    twice.
    """
    seen: set[int] = set()
    for node in _per_iteration(site):
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute) or id(child) in seen:
                continue
            parts = attribute_path(child, root)
            if parts is None:
                continue
            for inner in ast.walk(child):
                if isinstance(inner, ast.Attribute):
                    seen.add(id(inner))
            yield child, parts


class LoopRule(Rule):
    """Base for rules that read the shared loop inventory.

    Each subclass sees only loops whose rows are instances of a model we know,
    since a rule that cannot name the model cannot name the relation either.
    """

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for site in ctx.loops:
            model = site.model
            element = site.loop.element
            if model is None or element is None or element.spec is None:
                continue
            if reassigned(site, element.name):
                continue
            yield from self.inspect(ctx, Rows(site, model, element.name, element.spec))

    def inspect(self, ctx: ProjectContext, rows: Rows) -> Iterator[Finding]:
        raise NotImplementedError


@register
class UnfetchedForwardRelation(LoopRule):
    """DJP-001 -- a join that was available and was not taken."""

    meta = RuleMeta(
        id="DJP-001",
        title="Forward relation followed in a loop without select_related",
        family=Family.DJP,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Reading a foreign key attribute on a row that did not fetch it issues "
            "one query per row. The loop runs a hundred times in development with "
            "twenty rows and nobody notices; it runs a hundred thousand times in "
            "production and the view times out. Nothing in the source distinguishes "
            "the two -- `book.author.name` is written identically whether the author "
            "arrived in the original query or costs a round trip -- which is why "
            "this is worth reporting statically rather than waiting for it to appear "
            "in a trace. The database is doing the same join either way; the only "
            "question is whether it does it once or once per row."
        ),
        remediation=(
            "Add `select_related` for the path this loop walks, naming the whole "
            "path when it crosses more than one relation: "
            "`Book.objects.select_related('author__publisher')`. A forward foreign "
            "key or one-to-one is fetched with a join, so this costs one query "
            "regardless of row count. If the relation is nullable and usually "
            "absent, `select_related` still pays -- the join is a `LEFT OUTER` and "
            "returns nulls rather than extra queries."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#select-related",
            "https://docs.djangoproject.com/en/stable/topics/db/optimization/#retrieve-everything-at-once-if-you-know-you-will-need-it",
        ),
        limitations=(
            "A queryset built in one function and iterated in another is not "
            "followed, so a loop over a parameter is not reported here.",
            "A relation reached through a property or a model method is invisible: "
            "the attribute read in the loop is the property name, and what it "
            "touches is a question about the model's own code rather than the loop.",
            "Django caches a fetched relation on the instance, so a second read of "
            "the same path costs nothing. Only the first access in a loop is a "
            "query, and this rule reports the path once however often it is read.",
        ),
    )

    def inspect(self, ctx: ProjectContext, rows: Rows) -> Iterator[Finding]:
        graph = ctx.model_graph
        reported: set[str] = set()
        for node, parts in accesses(rows.site, rows.name):
            walk = traversal(graph, rows.model, parts)
            if not walk or walk.path in reported or rows.spec.covers(walk.path):
                continue
            reported.add(walk.path)
            yield self.report(ctx, rows, node, walk)

    def report(
        self,
        ctx: ProjectContext,
        rows: Rows,
        node: ast.Attribute,
        walk: Traversal,
    ) -> Finding:
        site, spec, model, path = rows.site, rows.spec, rows.model, walk.path
        hops = " -> ".join(f"{e.source}.{e.field_name} ({e.kind})" for e in walk.edges)
        written = ", ".join(sorted(spec.select_related)) or "nothing"
        return self.finding(
            location=ctx.location(site.path, node),
            message=(
                f"`{ast.unparse(node)}` follows `{path}` on each row of a "
                f"`{model}` queryset that did not select it, costing one query "
                f"per row."
            ),
            confidence=None if spec.confident else Confidence.TENTATIVE,
            severity=Severity.HIGH if site.loop.nested else None,
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"loop at line {site.lineno} over {model}\n"
                        f"select_related: {written}\n"
                        f"relation walked: {hops}"
                    ),
                    source=f"{ctx.rel(site.path)}:{site.lineno}",
                ),
            ),
            properties={
                "model": model,
                "path": path,
                "depth": str(site.loop.depth),
                "loop_line": str(site.lineno),
            },
        )
