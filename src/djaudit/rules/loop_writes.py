"""DJP-007 -- one write per row, where a bulk write would do.

DJP-004 takes reads issued once per iteration and stops deliberately short of
writes, because the remediation is not the same shape. A read in a loop can
often be hoisted or joined; a write in a loop has to become `bulk_update` or
`bulk_create`, and neither is a drop-in. Both skip `save()` overrides, both
send no `pre_save`/`post_save` signals, and `bulk_update` does not refresh an
`auto_now` column. Advice that ignores any of those silently changes what the
code does.

So this rule is defined by its blockers, and every one of them is measured
rather than assumed. `scripts/prefetch_cache_probe.py` writes 20 rows both
ways and records the result as a CI gate:

- `save()` in a loop costs 21 queries; `bulk_update` costs 4. `create()` in a
  loop costs 20; `bulk_create` costs 1. The claim is the *shape* -- one grows
  with the row count, the other does not.
- `post_save` fires 20 times for the loop and **0** times for `bulk_update`.
- an `auto_now` column advances under `save()` and **does not** under
  `bulk_update`.

Measured against the three benchmark corpora, those blockers are most of the
rule: pretix has 68 loops that write their iteration target, and 53 of them are
on a model with a hand-written `save()`. Reporting the whole 68 would have been
telling most of the codebase to break itself.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import TYPE_CHECKING, NamedTuple

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
from djaudit.rules.loop_queries import per_iteration

if TYPE_CHECKING:
    from djaudit.context import ProjectContext
    from djaudit.dataflow.inventory import LoopSite
    from djaudit.graph.nodes import ModelGraph

SIGNALS = ("pre_save", "post_save")
"""The two signals a bulk write does not send.

`pre_delete`/`post_delete` are deliberately absent: this rule reports no
deletion, because `bulk_delete` does not exist and the replacement for a
`delete()` loop is a queryset `delete()`, whose cascade behaviour is a
different question.
"""

INSERT_MARKERS = frozenset({"pk", "id"})
"""Attributes whose assignment to `None` turns a save into an insert.

`row.pk = None; row.save()` is how a row is cloned, and it is the single most
common write-in-a-loop in the corpora after a plain update. It matters because
the remediation is the other function: `bulk_create`, not `bulk_update`.
"""


class Write(NamedTuple):
    """A write the loop performs once per row."""

    call: ast.Call
    model: str
    inserts: bool
    """True when the row is created rather than updated, which decides whether
    the advice is `bulk_create` or `bulk_update`."""

    blocked: str | None = None


def overridden_saves(graph: ModelGraph) -> frozenset[str]:
    """Every model whose `save()` a bulk write would skip.

    Ancestors count: a model inherits its base's `save`, and `bulk_update`
    bypasses that one just as thoroughly as one written in the class itself.
    """
    written = {
        model.name
        for model in graph
        if model.node is not None
        and any(
            isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and stmt.name == "save"
            for stmt in model.node.body
        )
    }
    return frozenset(
        {
            model.label
            for model in graph
            if model.name in written or any(base.split(".")[-1] in written for base in model.mro)
        }
    )


def signal_receivers(ctx: ProjectContext) -> frozenset[str]:
    """Model names named as the `sender` of a `pre_save`/`post_save` hookup.

    Matched on the bare class name rather than the label, because a sender is
    written as `Device`, `"dcim.Device"` or `settings.AUTH_USER_MODEL` and only
    the first is resolvable here. Over-matching is the safe direction: it can
    only silence the rule.
    """
    senders: set[str] = set()
    for path in ctx.python_files:
        source = ctx.source(path)
        if source is None or not any(name in source for name in SIGNALS):
            continue
        tree = ctx.parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # The signal is the callee in `post_save.connect(f, sender=X)` but
            # a positional argument in `@receiver(post_save, sender=X)`, which
            # is the form the corpora actually use.
            written = " ".join(ast.unparse(part) for part in (node.func, *node.args))
            if not any(name in written for name in SIGNALS):
                continue
            for keyword in node.keywords:
                if keyword.arg == "sender":
                    senders.add(ast.unparse(keyword.value).strip("'\"").split(".")[-1])
    return frozenset(senders)


def auto_now_models(graph: ModelGraph) -> frozenset[str]:
    """Models with a column `save()` refreshes and `bulk_update` leaves alone."""
    return frozenset(
        model.label
        for model in graph
        if any(
            field.auto_now or field.auto_now_add
            for field in (*model.fields.values(), *model.inherited.values())
        )
    )


def assigned_none(body: tuple[ast.AST, ...], name: str) -> bool:
    """Whether the loop body sets `<name>.pk` or `<name>.id` to `None`."""
    for stmt in body:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if node.value.value is not None:
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in INSERT_MARKERS
                    and isinstance(target.value, ast.Name)
                    and target.value.id == name
                ):
                    return True
    return False


def forced_insert(call: ast.Call) -> bool:
    return any(
        keyword.arg == "force_insert"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


@register
class WriteInLoop(Rule):
    """DJP-007 -- saving one row at a time inside a loop over rows."""

    meta = RuleMeta(
        id="DJP-007",
        title="A row written once per iteration where a bulk write would do",
        family=Family.DJP,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "`save()` inside a loop issues one `UPDATE` per row and each one "
            "is a separate round trip. Measured over 20 rows: 21 queries for "
            "the loop against 4 for `bulk_update`, and 20 against 1 for "
            "`bulk_create`. The number that matters is not the ratio but the "
            "shape -- the loop grows with the table and the bulk call does "
            "not, so a form that is imperceptible on a developer's 50 rows is "
            "an outage on production's 50 million. In a data migration it is "
            "the difference between a deploy that pauses and one that has to "
            "be abandoned halfway."
        ),
        remediation=(
            "Collect the rows into a list, mutate them in the loop, and issue "
            "one `bulk_update(rows, [...])` after it -- or build the instances "
            "and call `bulk_create(rows)` where the loop inserts. Pass "
            "`batch_size` on a table large enough that one statement would be "
            "unreasonable. Where the loop only ever assigns the same value, a "
            "single `queryset.update()` is shorter still, and an arithmetic "
            "update can go to the database whole with an `F()` expression."
        ),
        limitations=(
            "A model with a hand-written `save()` is never reported, because "
            "no bulk write calls it. This is not a corner case: of pretix's 68 "
            "loops that save their iteration target, 53 are on such a model.",
            "A model named as the `sender` of a `pre_save` or `post_save` "
            "receiver is never reported. Measured: a 20-row loop delivers 20 "
            "`post_save` signals and `bulk_update` delivers none, so the "
            "advice would silently stop the receiver from running.",
            "A model with an `auto_now` or `auto_now_add` column is never "
            "reported. Measured: the column advances under `save()` and does "
            "not under `bulk_update`, so the naive replacement would quietly "
            "stop maintaining a timestamp.",
            "Only a loop whose rows come from a known model is considered. A "
            "loop over a list, a range or an unresolved call is skipped, since "
            "a bulk write needs rows of one model to write back.",
            "A `delete()` in a loop is not reported. There is no `bulk_delete`, "
            "and replacing it with a queryset `delete()` changes which "
            "cascades and signals run.",
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#bulk-update",
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#bulk-create",
            "https://docs.djangoproject.com/en/stable/topics/db/optimization/#use-bulk-methods",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        graph = ctx.model_graph
        if not len(graph):
            return
        blocked = overridden_saves(graph) | auto_now_models(graph)
        senders = signal_receivers(ctx)
        for site in ctx.loops:
            for write in self.writes(site, blocked, senders):
                yield self.report(ctx, site, write)

    def writes(
        self, site: LoopSite, blocked: frozenset[str], senders: frozenset[str]
    ) -> Iterator[Write]:
        """Every `save()` the loop performs on the row it is iterating.

        The loop's own model is the gate. `bulk_update` writes rows of one
        model back to one table, so a loop whose rows we cannot name is not a
        candidate no matter what its body does.
        """
        model = site.model
        if model is None or not site.loop.unbounded_rows:
            return
        if model in blocked or model.split(".")[-1] in senders:
            return
        targets = {target.name for target in site.loop.targets if target.name}
        if not targets:
            return
        body = per_iteration(site)
        for node in body:
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not isinstance(func, ast.Attribute) or func.attr != "save":
                    continue
                if not isinstance(func.value, ast.Name) or func.value.id not in targets:
                    continue
                name = func.value.id
                yield Write(call, model, forced_insert(call) or assigned_none(body, name))

    def report(self, ctx: ProjectContext, site: LoopSite, write: Write) -> Finding:
        call, model, inserts, _ = write
        bulk = "bulk_create" if inserts else "bulk_update"
        doing = "inserts a row" if inserts else "writes a row back"
        return self.finding(
            location=ctx.location(site.path, call),
            message=(
                f"This loop {doing} for every `{model}` it iterates. Collect "
                f"them and call `{bulk}` once instead."
            ),
            properties={"model": model, "suggested": bulk, "loop_line": str(site.lineno)},
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"loop at line {site.lineno} over {model}\n"
                        f"{ast.unparse(call)} at line {call.lineno}\n"
                        f"measured over 20 rows: 21 queries this way, "
                        f"{'1' if inserts else '4'} via {bulk}\n"
                        f"{model} has no save() override, no pre/post_save "
                        f"receiver and no auto_now column, so {bulk} is "
                        f"behaviour-preserving here"
                    ),
                    source=str(site.path),
                ),
            ),
        )
