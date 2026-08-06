"""DJP-008 -- a whole table pulled into memory at once.

This rule is narrow, and the measurement is why. The obvious version of it --
"report `list(Model.objects.all())`" -- was run across the three corpora first
and found 23 sites, of which **17 were test files** and 4 more were tables
bounded by their nature (content types, custom fields, tags). Roughly two were
worth reporting. The premise cannot be established from source: whether holding
a table in memory is a bug depends on how many rows it has, and the row count
is not in the code.

So the rule does not try to guess table size. It requires a context where the
row count is unbounded *by construction* -- a data migration, a management
command, or a background task, each of which runs against production data --
and within that, only an explicit materialisation of a queryset that was never
narrowed. That is the one shape where "this holds every row" is a fact rather
than a guess.

The cost is measured in `scripts/prefetch_cache_probe.py`. Over 20000 rows,
`list(qs)` peaks at 11.0 MB with 20000 rows in the result cache; the same read
through `.iterator()` peaks at 1.2 MB with an empty cache. The streaming figure
is about one `chunk_size` worth, which is the whole point: one form grows with
the table and the other does not.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, NamedTuple

from djaudit.dataflow.chaining import analyse
from djaudit.dataflow.querysets import QuerysetValue
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Severity, Tier
from djaudit.registry import Rule, RuleMeta, register

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.dataflow.scopes import Scope
    from djaudit.models import Finding

MATERIALISERS = frozenset({"list", "set", "tuple", "sorted", "frozenset"})
"""Builtins that consume an iterable whole.

`reversed` is absent deliberately: it raises on a queryset rather than
materialising one, so reporting it would be reporting a crash as a slow read.
"""

NARROWING = frozenset({"filter", "exclude", "none", "in_bulk"})
"""Methods that say the author meant a subset.

Not a proof of boundedness -- `filter(active=True)` can still match every row --
but the rule's claim is "this reads the *whole table*", and after a `filter`
that claim is no longer something the source supports.
"""

STREAMING = frozenset({"iterator", "count", "exists", "aggregate", "values_list", "values"})
"""Chains that either already stream or never build model instances."""

TASK_MARKERS = ("task", "periodic", "cron")
"""Substrings identifying a decorator that schedules background work.

Matched against the unparsed decorator so `@shared_task`,
`@app.task(bind=True)` and `@periodic_task` all count.
"""


class Materialisation(NamedTuple):
    """A whole-table read, and where it happens."""

    node: ast.expr
    value: QuerysetValue
    kind: str
    context: str


CONTEXT_MARKERS = ("RunPython", "Command", "task", "periodic", "cron")
"""Source substrings without which no production context can exist.

A file-level text test, checked before the file is walked. Most of a Django
project is neither a migration nor a command nor a task, and walking 1225 files
to learn that cost pretix 7 seconds against a 20 second budget -- the same
three-walks-per-file mistake 3.6.3 removed from `build_route_graph`.
"""


def named_base(base: ast.expr, name: str) -> bool:
    """Whether a class base is written `name` or ends in it -- `BaseCommand`."""
    if isinstance(base, ast.Name):
        return name in base.id
    return isinstance(base, ast.Attribute) and name in base.attr


def named(func: ast.expr, name: str) -> bool:
    """Whether this callee is written `name` or `something.name`.

    Structural rather than `ast.unparse(func) == name`. Unparsing every call in
    every file to test one string cost pretix roughly 4 seconds against a 20
    second budget, which is most of what a whole extra rule is allowed.
    """
    if isinstance(func, ast.Name):
        return func.id == name
    return isinstance(func, ast.Attribute) and func.attr == name


def decorated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a scheduling decorator is attached."""
    return any(
        marker in ast.unparse(deco).lower()
        for deco in node.decorator_list
        for marker in TASK_MARKERS
    )


def production_contexts(tree: ast.Module) -> dict[str, str]:
    """Function name to the reason it runs against an unbounded table.

    Deliberately does **not** walk the whole tree. A production context is
    always established at module or class level -- `operations = [RunPython(f)]`
    is a class attribute of `Migration`, `handle` is a method of `Command`, and
    a scheduling decorator is attached where the function is defined. Function
    *bodies* are the bulk of a codebase and can establish none of these, so
    they are never descended into.

    That matters: this rule's first version walked every node of every file and
    unparsed every call, and cost pretix 7 seconds against a 20 second budget.

    Checking the `RunPython` call rather than the path is what makes the
    migration case a structural claim instead of a path heuristic: a function
    Django will run against the production table is one that `RunPython` was
    handed. Only bare names are collected -- an earlier version also took the
    attribute of `RunPython(helpers.backfill)`, which looked like extra
    coverage and was not, since the name is recorded per module and in that
    form the function is defined in a different one.
    """
    contexts: dict[str, str] = {}
    statements: list[ast.stmt] = list(tree.body)
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            statements.extend(statement.body)
            if any(named_base(base, "Command") for base in statement.bases):
                contexts.update(
                    (stmt.name, "a management command")
                    for stmt in statement.body
                    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
                    and stmt.name.startswith("handle")
                )
    for statement in statements:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            if decorated(statement):
                contexts.setdefault(statement.name, "a scheduled task")
            continue
        if isinstance(statement, ast.ClassDef):
            continue
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call) or not named(node.func, "RunPython"):
                continue
            for arg in (*node.args, *(keyword.value for keyword in node.keywords)):
                if isinstance(arg, ast.Name):
                    contexts[arg.id] = "a data migration"
    return contexts


def materialisers(node: ast.AST) -> Iterator[tuple[ast.expr, ast.expr, str]]:
    """`(reported node, the iterable, how it was consumed)` for one node."""
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in MATERIALISERS
            and len(node.args) == 1
            and not node.keywords
        ):
            yield node, node.args[0], f"{node.func.id}()"
    elif isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp):
        kind = {ast.ListComp: "a list", ast.SetComp: "a set"}.get(type(node), "a dict")
        for generator in node.generators:
            if not generator.is_async:
                yield node, generator.iter, f"{kind} comprehension"


@register
class WholeTableInMemory(Rule):
    """DJP-008 -- reading an unbounded table into a list."""

    meta = RuleMeta(
        id="DJP-008",
        title="A whole table read into memory where it could be streamed",
        family=Family.DJP,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Materialising a queryset builds every row before the first one is "
            "used. Measured over 20000 rows: `list(qs)` peaks at 11.0 MB and "
            "holds 20000 rows in the result cache, while the same read through "
            "`.iterator()` peaks at 1.2 MB and holds none -- about one "
            "`chunk_size` at a time. In a migration, a management command or a "
            "background task the row count is whatever production has, so the "
            "memory is bounded by the table rather than by the code, and the "
            "failure mode is the process being killed partway through a "
            "deploy rather than anything that looks like a slow query."
        ),
        remediation=(
            "Iterate with `.iterator()` instead of building a list, passing "
            "`chunk_size` if the default 2000 rows is still too much. Where "
            "the rows are only used to build other rows, keep the batching "
            "explicit -- `bulk_create(..., batch_size=...)` consumes a "
            "generator. Where only a column is needed, `values_list(...)` "
            "avoids building model instances at all."
        ),
        limitations=(
            "A migration that reaches its model through `apps.get_model()` -- "
            "the documented idiom, and what almost every real data migration "
            "does -- gives the static graph no model to name, so nothing in it "
            "is reported. This is the rule's largest recall gap and the reason "
            "it is quiet on two of the three benchmark corpora.",
            "A `RunPython` target defined in another module is not reported. "
            "The context is established per file, so only a function written "
            "beside the `RunPython` call that names it is covered.",
            "Only migrations, management commands and scheduled tasks are "
            "considered, because they are the contexts whose row count is "
            "unbounded by construction. A request handler reading a whole "
            "table is not reported, since a table small enough to render is "
            "small enough to hold.",
            "A queryset narrowed by `filter`, `exclude` or `none` is never "
            "reported. Narrowing does not prove the result is small, but it "
            "removes the rule's evidence that the read covers the whole table.",
            "Table size is not knowable from source, so a whole-table read of "
            "a table that is small by nature -- content types, choices, "
            "feature flags -- is reported on the same footing as one of an "
            "unbounded table. The context requirement is what keeps that rare.",
            "A bare `for` loop over an unnarrowed queryset is not reported "
            "even though it also fills the result cache, because in the "
            "corpora those loops are overwhelmingly the ones DJP-007 already "
            "speaks about, and two findings on one loop help nobody.",
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#iterator",
            "https://docs.djangoproject.com/en/stable/topics/db/optimization/#retrieve-everything-at-once-if-you-know-you-will-need-it",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or not any(marker in source for marker in CONTEXT_MARKERS):
                continue
            tree = ctx.parse(path)
            if tree is None:
                continue
            contexts = production_contexts(tree)
            if not contexts:
                continue
            for found in self.reads(ctx, path, tree, contexts):
                yield self.report(ctx, path, found)

    def reads(
        self,
        ctx: ProjectContext,
        path: Path,
        tree: ast.AST,
        contexts: dict[str, str],
    ) -> Iterator[Materialisation]:
        """Whole-table reads inside a function that runs against production.

        The walk carries both the innermost scope (for the tracker) and the
        nearest enclosing production function, so a helper defined inside a
        `RunPython` target is covered and a sibling function is not.
        """
        root = ctx.scopes(path)
        if root is None:
            return
        scopes = {id(scope.node): scope for scope in root.walk()}
        wanted: list[tuple[ast.expr, ast.expr, str, Scope, str]] = []
        stack: list[tuple[ast.AST, Scope, str | None]] = [(tree, root, None)]
        while stack:
            node, scope, where = stack.pop()
            current = scopes.get(id(node), scope)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                where = contexts.get(node.name, where)
            if where is not None:
                for reported, iterable, kind in materialisers(node):
                    wanted.append((reported, iterable, kind, current, where))
            for child in ast.iter_child_nodes(node):
                stack.append((child, current, where))
        if not wanted:
            return

        tracked: dict[int, dict[int, QuerysetValue]] = {}
        for reported, iterable, kind, scope, where in wanted:
            key = id(scope.node)
            if key not in tracked:
                tracked[key] = ctx.tracked(path, scope)
            value = tracked[key].get(id(iterable))
            if value is None or value.terminal or value.model is None:
                continue
            methods = set(value.methods)
            if methods & NARROWING or methods & STREAMING:
                continue
            if analyse(value).sliced:
                continue
            yield Materialisation(reported, value, kind, where)

    def report(self, ctx: ProjectContext, path: Path, found: Materialisation) -> Finding:
        node, value, kind, where = found
        return self.finding(
            location=ctx.location(path, node),
            message=(
                f"This builds {kind} holding every `{value.model}` row, inside "
                f"{where}. Stream it with `.iterator()` instead."
            ),
            properties={"model": value.model or "", "context": where, "materialiser": kind},
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"{kind} over {value.model} at line {node.lineno}\n"
                        f"chain: {'.'.join(value.methods) or 'objects'}\n"
                        f"measured over 20000 rows: 11.0 MB held this way, "
                        f"1.2 MB through .iterator()"
                    ),
                    source=str(path),
                ),
            ),
        )
