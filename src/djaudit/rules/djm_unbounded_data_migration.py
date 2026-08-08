"""`DJM-007` -- a data migration that reads the whole table into memory at once.

This is the second rule to look at a loop that walks a queryset, and the first
question to settle is why it is not `DJP-007` under another name. Both look at
the same lines. They report different harms, they are silent in different
places, and -- this is the part that matters -- **their remediations pull in
opposite directions.**

`DJP-007` is about round trips. A `save()` per row costs one `UPDATE` per row,
and its fix is to collect the rows and issue one `bulk_update`. That fix
*requires* holding every row in memory at once, which is precisely the thing
this rule is about.

This rule is about the fetch. `for row in Model.objects.filter(...)` evaluates
the queryset into a list before the first iteration, so the migration's peak
memory is the size of the result set. In application code that is bounded by a
request's page size and recoverable by a retry. In a migration it is bounded by
nothing: `migrate` runs once, usually in a deploy container with a modest limit,
against production row counts rather than a developer's fifty, and the default
`atomic = True` means a process killed two-thirds of the way through rolls the
whole thing back and leaves the deploy stuck between releases. The fix is
`.iterator(chunk_size=...)`, which is server-side and holds one chunk.

Neither fix is complete on its own, so the honest advice is both: iterate in
chunks, and bulk-write within each chunk.

The two rules do not overlap in practice, and that was measured rather than
hoped for. Across the three corpora `DJP-007` reports 21 findings inside
migration files and **neither of the two leaf loops this rule reports is among
them**:

* pretix's `banktransfer.0012_org_level_plugin` loops over
  `Organizer.objects.filter(Exists(...))` and calls `org.save()`. `DJP-007`
  declines it because `Organizer` has a hand-written `save()`, which no bulk
  write would call -- its first and largest documented blocker, covering 53 of
  pretix's 68 writing loops. The advice it withholds is the *write* advice. The
  fetch is still unbounded and nothing else says so.
* pretix's `returnurl.0002_auto_20240301_1355` loops over
  `EventSettingsStore.objects.filter(key='returnurl_prefix')`, where the model
  comes from `apps.get_model('pretixbase', 'Event_SettingsStore')` and is a
  hierarkey table absent from `models.py`. `DJP-007` requires a named model and
  this rule does not, because the row count does not depend on knowing what the
  rows are called.

So this rule fires where `DJP-007` deliberately goes quiet. That is its whole
justification, and if a future change to `DJP-007` closed those two gaps, this
rule would be reporting duplicates and should be reconsidered.

The loop analysis itself is `djaudit.dataflow`'s, not this module's.
`ctx.loops` already resolves `qs = Model.objects.filter(...)` through the
def-use chains and hands back the queryset for `for row in qs`, which is one of
the two shapes above. Re-deriving that here would have been a second, worse
copy of a component with its own tests.

Two ways a loop can be quiet, both measured on real code:

* **`.iterator()` or `.aiterator()`** is the fix, and pretix uses it 20 times
  across its own migrations -- so the guard is exercised by real history rather
  than only by fixtures.
* **A sliced queryset** caps the row count, which caps the damage.

And one way it is quiet that is not about querysets at all: an iterable this
tool cannot show to be a queryset is left alone. NetBox's leaf
`dcim.0241_nullify_empty_cable_end` loops over `CABLED_MODELS`, a module-level
tuple of model classes with six entries, and a rule that assumed every loop in
a migration walked a table would report it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import TYPE_CHECKING

from djaudit.context import ProjectContext
from djaudit.dataflow.loops import ROW_ITERATORS, Bind
from djaudit.dataflow.querysets import TERMINAL
from djaudit.migrations.nodes import MigrationNode, Operation
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import MigrationRule

if TYPE_CHECKING:
    from djaudit.dataflow.inventory import LoopSite
    from djaudit.dataflow.querysets import QuerysetValue

MANAGER_SOURCES = frozenset({"objects", "_default_manager", "_base_manager"})
"""Attributes that hand back a fresh queryset over a whole table.

Used only by the fallback below, for the case where the dataflow could not name
the model. A custom manager attribute is not here and so is not reported, which
is the quiet direction: `Model.published.all()` is as unbounded as
`Model.objects.all()`, and this rule would rather miss it than guess that every
attribute followed by `.filter()` is a manager.
"""


def _row_value(site: LoopSite) -> QuerysetValue | None:
    """The queryset the dataflow says this loop draws its rows from.

    `None` covers two different situations and the caller has to separate
    them: an iterable that is not a queryset at all, which is where NetBox's
    loop over a tuple of model classes lands, and a queryset the def-use
    chains could not follow. The syntactic fallback below decides the second.
    """
    for target in site.loop.targets:
        if target.binds is Bind.ELEMENT and target.value is not None and not target.value.terminal:
            return target.value
    return None


def _chain_names(node: ast.expr) -> tuple[str, ...]:
    """Every attribute name on an expression's call chain.

    Both callers ask only whether a given name is present, so the order is not
    a promise. Stops at anything that is neither an attribute nor a call, which is the
    behaviour `list(qs.iterator())` needs: the `list` call is not an attribute
    access, so the walk ends there rather than reaching the `.iterator` behind
    it and concluding that a fully materialised list streams.
    """
    names: list[str] = []
    current = node
    while True:
        if isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Attribute):
            names.append(current.attr)
            current = current.value
        else:
            return tuple(names)


def _manager_chain(node: ast.expr) -> bool:
    """Whether an expression is written as a query over a whole table.

    The fallback for a queryset the dataflow declined to name. `DJP-007` needs
    the model because its advice is about that model's `save()`; this rule
    needs only the row count, so a queryset over a table absent from
    `models.py` -- a `django-hierarkey` store, say -- is still worth reporting.

    A terminal step disqualifies the chain even though `.objects` is right
    there, because `Model.objects.aggregate(...)` returns one dict and
    `Model.objects.count()` returns one integer. Looping over either is
    strange but bounded, and an earlier draft of this rule reported both.
    `QuerysetValue.terminal` disqualifies them on the other path for the same
    reason, and this reuses its set rather than keeping a second copy.
    """
    names = _chain_names(node)
    return any(name in MANAGER_SOURCES for name in names) and not any(
        name in TERMINAL for name in names
    )


def _bounded(node: ast.expr) -> bool:
    """Whether the chain already holds rows one at a time.

    `.iterator()` is the remediation, so a loop that has it is the shape this
    rule asks for rather than a shape it reports.
    """
    return any(name in ROW_ITERATORS for name in _chain_names(node))


def _forward_names(migration: MigrationNode) -> dict[str, Operation]:
    """The forward function of every `RunPython` in this migration, by name.

    Keyed on the last component of the dotted name, because that is what a
    function's scope is called. A `RunPython` pointed at a method rather than a
    module-level function would match on the method's own name, which is the
    right scope to look in.
    """
    found: dict[str, Operation] = {}
    for op in [*migration.operations, *(i for o in migration.operations for i in o.inner)]:
        if op.callable_name:
            found.setdefault(op.callable_name.rsplit(".", 1)[-1], op)
    return found


@register
class UnboundedQuerysetInDataMigration(MigrationRule):
    """A `RunPython` whose loop evaluates a whole table into memory."""

    meta = RuleMeta(
        id="DJM-007",
        title="Data migration iterates a queryset with no bound on the rows fetched",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        rationale=(
            "A queryset is evaluated into a list before the loop's first "
            "iteration, so this migration's peak memory is the size of the "
            "whole result set. A request that does the same is bounded by a "
            "page size and recovered by a retry; `migrate` is bounded by "
            "nothing, runs against production row counts rather than a "
            "developer's, and under the default `atomic = True` a process "
            "killed part-way through rolls everything back and leaves the "
            "deploy stranded between releases."
        ),
        remediation=(
            "Iterate with `.iterator(chunk_size=...)`, which streams rows from "
            "the server and holds one chunk at a time. Where the loop also "
            "writes each row, combine the two: collect a chunk, issue one "
            "`bulk_update` for it, and move on -- `DJP-007`'s advice on its "
            "own asks for every row in memory at once, which is the problem "
            "this rule is about. A loop that only ever assigns the same value "
            "needs no loop: a single `queryset.update()` does it in one "
            "statement and fetches nothing."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#iterator",
            "https://docs.djangoproject.com/en/stable/topics/db/optimization/"
            "#retrieve-individual-objects-using-a-unique-filtering-attribute",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported. The live tier "
            "reads `django_migrations` and does not have to guess.",
            "The row count is not knowable from source. A table with a "
            "thousand rows loads fine and is reported the same as one with a "
            "hundred million, so the finding is about the shape rather than a "
            "prediction that this particular migration will fail.",
            "An iterable this tool cannot show to be a queryset is left alone, "
            "including a queryset reached through a custom manager attribute "
            "rather than `objects`.",
            "Only loops written directly in the `RunPython` forward function "
            "are considered. A loop in a helper it calls is not attributed "
            "back to the migration.",
        ),
    )

    def _sites(self, ctx: ProjectContext, migration: MigrationNode) -> list[LoopSite]:
        return [site for site in ctx.loops if site.path == migration.path]

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        op = applied.operation
        if not op.callable_name:
            return
        name = op.callable_name.rsplit(".", 1)[-1]
        # One `Applied` per operation, so a migration with two `RunPython`s
        # naming the same function would otherwise report its loops twice. The
        # first operation naming a given function owns it. This also stands in
        # for a check on the operation kind: only `RunPython` carries a
        # callable, so an operation reaching here is one either way.
        if _forward_names(applied.migration).get(name) is not op:
            return

        migration = applied.migration
        for site in self._sites(ctx, migration):
            if site.scope.name != name:
                continue
            loop = site.loop
            if not loop.unbounded_rows or _bounded(loop.iterable):
                continue
            value = _row_value(site)
            if value is None and not _manager_chain(loop.iterable):
                continue

            source = ast.unparse(loop.iterable)
            # `for org in qs` unparses to `qs`, which names nothing a reader
            # can act on. When the def-use chains got as far as the model,
            # that is the useful thing to print.
            model = value.model if value is not None else None
            subject = f"rows of `{model}`" if model else f"`{source}`"
            yield self.finding(
                severity=Severity.MEDIUM,
                location=ctx.location(migration.path, loop.anchor),
                message=(
                    f"`{migration.app}.{migration.name}` loops over "
                    f"{subject} in `{name}`, which evaluates every matching "
                    f"row into memory before the first iteration. Use "
                    f"`.iterator(chunk_size=...)` so the migration holds one "
                    f"chunk rather than the whole table."
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=ctx.snippet(migration.path, loop.lineno),
                        source=f"{ctx.rel(migration.path)}:{loop.lineno}",
                    ),
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"queryset reached by "
                            f"{'dataflow' if value is not None else 'manager attribute'}; "
                            f"no iterator() and no slice"
                        ),
                        source=f"{ctx.rel(migration.path)}:{loop.lineno}",
                    ),
                ),
            )
