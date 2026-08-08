"""`DJM-006` -- a data operation with no reverse, taking a schema change down with it.

The naive form of this rule -- "`RunPython` without `reverse_code`" -- is a bad
rule, and the corpus says so plainly. Across the three benchmark projects there
are 162 `RunPython` operations and only 11 lack a reverse; the single one of
those at any leaf is pretix's
`sendmail.0011_remove_cross_event_scheduled_mails`, whose forward pass is two
`.delete()` calls against rows that should never have existed. Nothing can undo
a delete. Telling that author to add `reverse_code` would be asking for a
function that cannot be written, and telling them to add
`reverse_code=RunPython.noop` would be worse: `noop` does not mean "this cannot
be undone", it means "undoing this is a no-op", so it *permits* `migrate` to
walk backwards past the deletion and leave the older release facing data it
already decided was corrupt. An honestly irreversible operation should say so by
having no reverse, which is exactly what Django's default expresses.

So irreversibility on its own is not the defect. The defect is irreversibility
placed where it revokes somebody else's reverse.

Read `Migration.unapply` (`db/migrations/migration.py`) for why. It runs in two
phases, and the check is in the first one:

    for operation in self.operations:
        if not operation.reversible:
            raise IrreversibleError(...)

`RunPython.reversible` is `self.reverse_code is not None` and `RunSQL.reversible`
is `self.reverse_sql is not None`, both plain properties on the operation. Phase
1 walks *every* operation before Phase 2 executes any of them, so one
reverse-less data operation aborts the unapply of the entire migration before a
single statement is issued. A perfectly reversible `AddField` sitting in the
same `operations` list never gets its `database_backwards` called.

That is the finding. The author of the `AddField` wrote something Django can
undo, and a `RunPython` three lines below took that away. `migrate app 0041`
does not partially unwind and does not warn -- it raises, and the schema stays
where it is. During a failed deploy that is the difference between a scripted
rollback and hand-writing DDL against production at speed.

The remediation is therefore *not* "add a reverse". It is **split the data
operation into its own migration**, which is correct whether or not the data
pass could have been reversed: the schema migration then unapplies cleanly on
its own, and the irreversibility is confined to a migration that changes no
schema and so has nothing anyone needs to unwind. Where the data pass only
fills a column the schema half adds -- four of the corpus's eleven are exactly
that, and their file names say so (`0051_..._kind`, `0209_..._denorm_site_location`,
`0004_rule_restrict_to_status`, `0252_logentry_organizer`) -- `RunPython.noop`
is *also* honest, because unapplying drops the column the backfill wrote. That
is offered second because it is only sometimes true, and this rule does not read
the body closely enough to know when.

One nesting case is verified rather than assumed. `SeparateDatabaseAndState`
does not override `reversible`, and `Operation.reversible` is a plain class
attribute set to `True`, so a reverse-less `RunPython` *inside* the wrapper
passes Phase 1 untouched and raises from Phase 2 instead, out of
`SeparateDatabaseAndState.database_backwards`. By then Phase 2 has already
unapplied every operation listed after the wrapper, because it iterates the
reverse of file order. Under the default `atomic = True` that partial unwind is
rolled back with the transaction and the outcome matches the flat case; under
`atomic = False` it is not, and the database is left half-way back. Nested
operations are reported for that reason rather than in spite of it.

Quiet cases, in the order they are tested:

* **The data operation has a reverse**, including `RunPython.noop` and
  `RunSQL.noop`. The author answered the question.
* **The migration changes no schema.** Irreversibility is then confined to
  itself: there is no reverse being revoked, and the previous release still
  matches the schema on disk because nothing about the schema moved. This is
  the guard that keeps pretix's leaf `.delete()` quiet, and it is the whole
  reason the rule reports nothing across 875 real migrations.
* **The only co-located operations are state-only.** `AlterModelOptions` and
  `AlterModelManagers` emit no DDL, so an unapply that never reaches them
  changes nothing on disk. An operation this tool could not classify is left
  alone for the same reason in reverse -- we cannot say it emits DDL either.
"""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.migrations.nodes import DATA_KINDS, MigrationNode, Operation, OperationKind
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import MigrationRule

REVERSIBLE_SCHEMA_KINDS = frozenset(
    {OperationKind.SCHEMA, OperationKind.INDEX, OperationKind.CONSTRAINT}
)
"""Operation kinds whose reverse actually moves the database back.

`OperationKind.STATE` is excluded because `AlterModelOptions` and
`AlterModelManagers` emit no DDL in either direction, so an unapply that aborts
before reaching them has cost nothing. `OperationKind.UNKNOWN` is excluded
because a third-party operation might emit no DDL, and this rule declines to
assume it does -- the same reasoning that makes :func:`classify` refuse to call
an unrecognised operation harmless, applied in the direction that stays quiet.
"""


def _all_operations(migration: MigrationNode) -> list[Operation]:
    """Every operation in the file, including the halves of a wrapper.

    A `SeparateDatabaseAndState` holding an `AddField` in its
    `database_operations` still emits the DDL, so its reverse is still a reverse
    somebody loses.
    """
    return [*migration.operations, *(inner for op in migration.operations for inner in op.inner)]


def _schema_operations(migration: MigrationNode) -> list[Operation]:
    """The operations in this migration whose reverse would change the database."""
    return [op for op in _all_operations(migration) if op.kind in REVERSIBLE_SCHEMA_KINDS]


@register
class IrreversibleDataOperationBlocksRollback(MigrationRule):
    """A reverse-less `RunPython`/`RunSQL` beside a schema change it cannot unwind."""

    meta = RuleMeta(
        id="DJM-006",
        title="Irreversible data operation blocks rollback of a schema change",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        rationale=(
            "`Migration.unapply` checks every operation's `reversible` before "
            "running any of them, so one data operation with no reverse aborts "
            "the unapply of the whole migration with `IrreversibleError`. The "
            "schema change beside it is reversible on its own and never gets "
            "the chance -- a failed deploy has no scripted way back."
        ),
        remediation=(
            "Move the data operation into a migration of its own, so the "
            "schema migration can be unapplied by itself and the "
            "irreversibility is confined to a migration that changes no "
            "schema. If the data pass only fills a column this same migration "
            "adds, `reverse_code=RunPython.noop` is also honest, because "
            "unapplying drops that column anyway. Do not add a `noop` reverse "
            "to an operation that destroyed data -- it does not record that "
            "the change is irreversible, it lets `migrate` walk backwards past "
            "it."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/migration-operations/#runpython",
            "https://docs.djangoproject.com/en/stable/howto/writing-migrations/"
            "#migrations-that-add-unique-fields",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported. The live tier "
            "reads `django_migrations` and does not have to guess.",
            "A data operation that is irreversible on its own is not reported. "
            "That is usually the honest state for a pass that deletes or "
            "overwrites, and the rule does not read the forward function's "
            "body closely enough to tell an oversight from a decision.",
            "An explicit `reverse_code=None` or `reverse_sql=None` reads as a "
            "supplied reverse and is not reported, although Django treats it "
            "as irreversible. The parser records whether the argument was "
            "written, not what it evaluates to.",
            "A reverse-less operation nested in `SeparateDatabaseAndState` "
            "fails in `unapply`'s second phase rather than its first, so a "
            "migration with `atomic = False` is left partly unwound instead of "
            "wholly unmoved. Both are reported the same way.",
        ),
    )

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        op = applied.operation
        if op.kind not in DATA_KINDS or op.reverse is not False:
            return

        migration = applied.migration
        blocked = _schema_operations(migration)
        if not blocked:
            return

        first = blocked[0]
        subject = f"`{op.name}`" if op.callable_name is None else f"`{op.name}({op.callable_name})`"

        yield self.finding(
            severity=Severity.MEDIUM,
            location=self.locate(ctx, migration, op),
            message=(
                f"`{migration.app}.{migration.name}` has no reverse for this "
                f"{subject}, so unapplying the migration raises "
                f"`IrreversibleError` before any operation runs. The "
                f"`{first.name}` at line {first.lineno} is reversible on its "
                f"own and cannot be unwound while it shares a migration with "
                f"this one."
            ),
            evidence=(
                self.excerpt(ctx, migration, op),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"no reverse supplied; blocks the reverse of "
                        f"{', '.join(dict.fromkeys(blk.name for blk in blocked))}"
                    ),
                    source=f"{ctx.rel(migration.path)}:{op.lineno}",
                ),
            ),
        )
