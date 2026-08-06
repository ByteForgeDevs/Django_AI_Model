"""DJP-003 -- the N+1 that has no loop in it.

Every other performance rule in this family starts from a `for`. This one
cannot, because the loop belongs to DRF: a serializer used with `many=True`
calls each `SerializerMethodField` getter once per row, and nothing in the
serializer's source says so. The getter reads like a function that runs once.

That is what makes it the most common real-world N+1 and the one existing
tools miss. It also makes it the only rule here whose defect and whose fix
live in different files: the traversal is written in the serializer, and the
`select_related` that repairs it has to go on the view's queryset. Reporting
the serializer without naming the view would leave the reader with nowhere to
go, so the view is carried into the evidence.

That link is also the reason the rule can speak at all. A serializer on its
own has no queryset and therefore no answer to "was this already fetched" --
an unattached serializer is silent here, and one attached to several views is
judged against every one of them.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from djaudit.dataflow.chaining import ChainSpec, analyse
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
from djaudit.rules.performance import (
    Traversal,
    accesses,
    evaluations,
    many_hop,
    reassigned,
    traversal,
)

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.api.serializers import SerializerNode
    from djaudit.api.views import ViewNode
    from djaudit.context import ProjectContext
    from djaudit.graph.inheritance import ClassIndex, ClassRecord
    from djaudit.graph.nodes import ModelGraph

METHOD_FIELD = "SerializerMethodField"


@dataclass(frozen=True)
class Supply:
    """A view that hands rows to a serializer, and what its queryset fetched."""

    view: ViewNode
    spec: ChainSpec | None
    """``None`` when the view offers a queryset we could not read."""


def queryset_expressions(node: ast.ClassDef) -> Iterator[ast.expr]:
    """Every expression a view offers as its queryset.

    Both spellings, because they are equally common and a rule that read only
    the class attribute would go quiet on every viewset that scopes its rows
    to the request -- which is most of the interesting ones.
    """
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "queryset":
                    yield stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "queryset" and stmt.value:
                yield stmt.value
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and (
            stmt.name == "get_queryset"
        ):
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Return) and inner.value is not None:
                    yield inner.value


def guaranteed(specs: list[ChainSpec]) -> ChainSpec:
    """What *every* path through a view's queryset fetched.

    The intersection rather than the union, because a `get_queryset` with two
    returns only guarantees what both of them did. A union would let the
    branch that remembered `select_related` silence a finding about the branch
    that forgot it, which is precisely the branch worth reporting.
    """
    first, *rest = specs
    select = first.select_related
    prefetch = first.prefetch_related
    unreadable = first.unreadable
    for spec in rest:
        select &= spec.select_related
        prefetch &= spec.prefetch_related
        unreadable |= spec.unreadable
    return replace(
        first,
        select_related=select,
        prefetch_related=prefetch,
        unreadable=unreadable,
    )


def specs_in(ctx: ProjectContext, path: Path) -> dict[int, ChainSpec]:
    """Every queryset expression in one file, keyed by ``id(node)``.

    Built per file rather than per class because scoping and def-use are the
    expensive half and both are already per file. Only files defining a view
    that names a serializer ever reach here.
    """
    root = ctx.scopes(path)
    if root is None:
        return {}
    return {
        key: analyse(value)
        for scope in root.walk()
        for key, value in ctx.tracked(path, scope).items()
    }


def getter(
    record: ClassRecord, field: str, index: ClassIndex
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, Path] | None:
    """The `get_<field>` method, the name it binds the row to, and its file.

    The ancestry is searched, not just the class itself: a `SerializerMethodField`
    is very often declared on a shared base and implemented there too, and 177 of
    NetBox's 187 reachable method fields resolve that way. Looking only at the
    subclass found ten of them.

    The defining class's path comes back with the function because the finding
    belongs where the traversal is written, which is frequently a different file
    from the serializer the view names.

    `None` rather than an exception: the absent-method and the no-row-parameter
    cases both return it, and signalling them with `LookupError` meant an
    `IndexError` from the argument list -- which is a `LookupError` subclass --
    was caught by the caller's handler. The guard below then looked dead under
    defect injection because a bug was quietly producing its result for it.
    """
    wanted = f"get_{field}"
    for owner in (record, *index.ancestry(record)):
        for stmt in owner.node.body:
            if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) or stmt.name != wanted:
                continue
            args = stmt.args.posonlyargs + stmt.args.args
            if len(args) < 2:
                # `get_x(self)` is not reading a row, so no per-row cost can be
                # attributed to it.
                return None
            return stmt, args[1].arg, owner.path
    return None


@register
class SerializerMethodTraversal(Rule):
    """DJP-003 -- a relation walked once per serialized row."""

    meta = RuleMeta(
        id="DJP-003",
        title="Serializer method walks a relation the view's queryset did not fetch",
        family=Family.DJP,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "A `SerializerMethodField` getter runs once per row serialized, so a "
            "relation walked inside it costs one query per row of every list the "
            "serializer appears in. Nothing in the serializer says so: the getter "
            "is an ordinary-looking function with no loop in it, and the repetition "
            "lives in DRF rather than in the project's own code. That is why this "
            "one survives review, and why it is worth reporting statically -- it is "
            "invisible at the place it is written, and the fix belongs to a "
            "different file than the mistake."
        ),
        remediation=(
            "Fetch the relation on the view's queryset rather than in the getter: "
            "`select_related` for a forward foreign key or one-to-one, "
            "`prefetch_related` for a reverse relation or many-to-many. The "
            "serializer method itself does not change -- it simply stops being the "
            "thing that triggers a query. Where the getter needs filtered or "
            "ordered related rows, pass a `Prefetch` object so the narrowing "
            "happens inside that single query."
        ),
        limitations=(
            "Only `get_<field>` is matched. A `SerializerMethodField` given an "
            "explicit `method_name=` is not followed.",
            "A serializer no view attaches is not reported. Without a queryset "
            "there is no answer to whether the relation was already fetched, and "
            "guessing would report every serializer in the project.",
            "A serializer selected by a `get_serializer_class` override is not "
            "linked to that view, so a getter reached only that way is silent.",
            "A nested serializer is judged against the outer view's queryset; the "
            "nesting is not itself treated as a fetch requirement.",
            "A view whose queryset we cannot read downgrades the finding to "
            "tentative rather than silencing it, since the traversal is still "
            "per-row and only the coverage question is unanswered.",
        ),
        references=(
            "https://www.django-rest-framework.org/api-guide/fields/#serializermethodfield",
            "https://docs.djangoproject.com/en/stable/topics/db/optimization/#retrieve-everything-at-once-if-you-know-you-will-need-it",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        surface = ctx.api_surface
        index = surface.index
        if index is None:
            return
        graph = ctx.model_graph
        for label, supply in self.suppliers(ctx, surface.views, index, graph).items():
            serializer = surface.get(label)
            if serializer is None or serializer.model is None:
                continue
            record = index.lookup(label)
            if record is None:
                continue
            yield from self.serializer(ctx, serializer, record, index, supply, graph)

    def suppliers(
        self,
        ctx: ProjectContext,
        views: dict[str, ViewNode],
        index: ClassIndex,
        graph: ModelGraph,
    ) -> dict[str, list[Supply]]:
        """Serializer label to the views that hand it rows."""
        cache: dict[Path, dict[int, ChainSpec]] = {}
        out: dict[str, list[Supply]] = {}
        for view in views.values():
            if view.serializer_ref is None:
                continue
            record = index.lookup(view.label)
            if record is None:
                continue
            if view.path not in cache:
                cache[view.path] = specs_in(ctx, view.path)
            found = cache[view.path]
            specs = [found[id(e)] for e in queryset_expressions(record.node) if id(e) in found]
            label = index.resolve_name(view.module, view.serializer_ref)
            target = index.lookup(label)
            if target is not None:
                # `resolve_name` stops at the name as written, which for a
                # package that re-exports (`from .circuits import *` in
                # NetBox's `circuits/api/serializers/__init__.py`) is the
                # re-export path, not where the class is defined. `lookup`
                # follows the star; its record carries the label the API
                # surface is actually keyed by. Skipping this step cost 136 of
                # NetBox's 137 serializer-bearing views.
                label = target.dotted
            out.setdefault(label, []).append(Supply(view, guaranteed(specs) if specs else None))
        return out

    def serializer(  # noqa: PLR0913, PLR0917
        self,
        ctx: ProjectContext,
        serializer: SerializerNode,
        record: ClassRecord,
        index: ClassIndex,
        supply: list[Supply],
        graph: ModelGraph,
    ) -> Iterator[Finding]:
        model = serializer.model
        if model is None:
            return
        for field, declared in serializer.declared.items():
            if declared.kind != METHOD_FIELD:
                continue
            found = getter(record, field, index)
            if found is None:
                continue
            func, row, where = found
            body = tuple(func.body)
            if reassigned(body, row):
                continue
            yield from self.walk(
                ctx, serializer, field, body, row, where, Rows(supply, graph, model)
            )

    def walk(  # noqa: PLR0913, PLR0917
        self,
        ctx: ProjectContext,
        serializer: SerializerNode,
        field: str,
        body: tuple[ast.AST, ...],
        row: str,
        where: Path,
        rows: Rows,
    ) -> Iterator[Finding]:
        called = evaluations(body)
        reported: set[str] = set()
        for node, parts in accesses(body, row):
            found = rows.missing(node, parts, called)
            if found is None or found.path in reported:
                continue
            reported.add(found.path)
            yield self.report(ctx, serializer, field, node, found, rows.model, where)

    def report(  # noqa: PLR0913, PLR0917
        self,
        ctx: ProjectContext,
        serializer: SerializerNode,
        field: str,
        node: ast.Attribute,
        found: Missing,
        model: str,
        where: Path,
    ) -> Finding:
        views = ", ".join(sorted(s.view.label for s in found.views))
        hops = " -> ".join(f"{e.source}.{e.field_name} ({e.kind})" for e in found.walk.edges)
        unread = not found.sure
        return self.finding(
            location=ctx.location(where, node),
            message=(
                f"`{ast.unparse(node)}` walks `{found.path}` once per serialized "
                f"row of `{model}`, and the queryset feeding it did not fetch that "
                f"relation, costing one query per row."
            ),
            confidence=Confidence.TENTATIVE if unread else None,
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"{serializer.name}.get_{field} runs once per row of {model}\n"
                        f"served by: {views}\n"
                        f"missing: {found.fetch}('{found.path}')"
                        + (f"\nrelation walked: {hops}" if hops else "")
                    ),
                    source=f"{ctx.rel(serializer.path)}:{serializer.lineno}",
                ),
            ),
            properties={
                "model": model,
                "path": found.path,
                "fetch": found.fetch,
                "field": field,
            },
        )


@dataclass(frozen=True)
class Missing:
    """A relation the serializer walks and no supplying queryset fetched."""

    path: str
    fetch: str
    views: tuple[Supply, ...]
    walk: Traversal
    sure: bool = True
    """Whether every supplier's fetch calls were readable.

    `prefetch_related(GenericPrefetch("cable__terminations__termination", ...))`
    in NetBox's `InterfaceViewSet` names a path we cannot read, and prefetching
    through a forward foreign key does populate it -- measured at 3 queries
    against a 5-query baseline, so a prefix prefetch removes the per-row cost
    just as `select_related` would. Treating an unreadable argument as "did not
    fetch" made that a firm false positive.
    """


@dataclass(frozen=True)
class Rows:
    """The rows a serializer is handed, and what every supplier fetched."""

    supply: list[Supply]
    graph: ModelGraph
    model: str

    def missing(self, node: ast.Attribute, parts: list[str], called: set[int]) -> Missing | None:
        """The unfetched relation this access walks, if there is one."""
        walk = traversal(self.graph, self.model, parts)
        reached = walk.edges[-1].target if walk.edges else self.model
        rest = parts[len(walk.steps) :]
        hop = (
            many_hop(self.graph, reached, rest[0])
            if id(node) in called and reached is not None and rest
            else None
        )
        if hop is not None:
            path = "__".join([*walk.steps, hop.accessor])
            short = [s for s in self.supply if s.spec is None or not s.spec.prefetches(path)]
            fetch = "prefetch_related"
            blinding = {"prefetch_related"}
        elif walk:
            # No `len(walk.steps) < len(parts)` condition, deliberately: a bare
            # `obj.author` with nothing after it still loads the author row once
            # per serialized row. DJP-001 reports that case, and the two rules
            # must not disagree about what a forward relation costs.
            path = walk.path
            short = [s for s in self.supply if s.spec is None or not s.spec.covers(path)]
            fetch = "select_related"
            # `covers` reads both sets, so either being unreadable leaves the
            # question open.
            blinding = {"select_related", "prefetch_related"}
        else:
            return None
        if not short:
            return None
        sure = not any(s.spec is None or s.spec.unreadable & blinding for s in short)
        return Missing(path, fetch, tuple(short), walk, sure)
