"""`DJM-008` -- a schema change and a data pass in one transaction.

The claim is not that mixing schema and data operations is untidy. It is that
Postgres holds every lock until the transaction ends, which the documentation
states plainly:

    Once acquired, a lock is normally held until the end of the transaction.

Every `ALTER TABLE` takes `ACCESS EXCLUSIVE` on the table it touches, and that
mode conflicts with every other mode including `ACCESS SHARE` -- so while it is
held, the table serves no reads and no writes at all. Taking it is usually
fine, because on a modern Postgres most `ALTER TABLE` forms are catalogue-only
and the lock is measured in milliseconds.

What makes it an outage is what comes next in the same transaction. Django runs
a migration inside one transaction unless `atomic = False`, so a `RunPython`
backfill placed after a schema operation does not merely take its own time: it
extends the window during which the earlier operation's `ACCESS EXCLUSIVE` is
still held. A backfill that takes four minutes on production data turns a
millisecond lock into a four-minute one, and every query against that table
queues behind it. Worse, those queued queries hold their own locks while they
wait, so the stall spreads past the table the migration named.

**The order is the whole rule.** The same two operations the other way round
are close to harmless: the data pass runs first taking only row locks, the
schema change happens at the end, and the strong lock is held for whatever is
left of the transaction. So this rule asks whether a lock-taking operation
appears *before* a data operation, not merely whether both are present.

That is also what separates it from `DJM-006`, which looks at the same pair of
operation kinds in the same migration. `DJM-006` is about rollback: a data
operation with no reverse aborts `Migration.unapply` before any schema
operation gets to move back. It does not care about order and does not care
about `atomic`. This rule cares about both and does not care about reverses.
Measured at the leaf across the three corpora, they share no findings.

Two things deliberately do not trigger it:

* **`atomic = False`.** Each operation then commits on its own and its locks
  go with it, so a slow backfill delays only itself. This is one of the two
  remediations, and the fixture's control migration is built on it.
* **A data operation with nothing lock-taking before it.** 8 of the 75 mixed
  migrations across the corpora are written this way, so the ordering test is
  doing real work rather than describing every migration that mixes the two.

`OperationKind.STATE` is not lock-taking -- `AlterModelOptions` emits no SQL,
so it cannot be holding anything. `OperationKind.UNKNOWN` is also excluded,
which is the quiet direction: a third-party operation may emit no DDL at all,
and reporting one on the chance that it does would put the burden of proof on
the wrong side.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from djaudit.context import ProjectContext
from djaudit.migrations.nodes import DATA_KINDS, MigrationNode, Operation, OperationKind
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import MigrationRule

LOCKING_KINDS = frozenset({OperationKind.SCHEMA, OperationKind.INDEX, OperationKind.CONSTRAINT})
"""Operation kinds that take a table-level lock Postgres holds until commit.

The same three members as `DJM-006`'s `REVERSIBLE_SCHEMA_KINDS`, and kept
separate on purpose: that set answers "does unapplying this move the database
back", this one answers "does running this take a lock". They agree today
because the kinds that emit DDL are the kinds that can be undone, but a change
to either question should not silently move the other rule.
"""


TABLE_CREATING = frozenset({"CreateModel"})
"""Operations that bring a table into existence rather than change one.

`MigrationRule.populated` cannot answer for these and must not be asked. It
reports the state that *preceded* the operation, and before a `CreateModel`
there is no creator on record for the table, so it reads as one that has been
there all along -- the exact opposite of the truth. Naming the case here rather
than reusing `model_tracked`, which is about whether the replay can be trusted
on a model's columns and happens to be false in the same place for an unrelated
reason.
"""


def _label(op: Operation) -> str:
    return f"{op.name}({op.model_name})" if op.model_name else op.name


def _tables(ops: Sequence[Operation]) -> list[str]:
    names = [op.model_name for op in ops if op.model_name is not None]
    return list(dict.fromkeys(names))


@register
class SchemaAndDataInOneTransaction(MigrationRule):
    """A data operation that runs while an earlier schema change still holds its lock."""

    meta = RuleMeta(
        id="DJM-008",
        title="Data operation runs in the same transaction as an earlier schema change",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        rationale=(
            "Postgres holds a lock until the end of the transaction, and every "
            "`ALTER TABLE` takes `ACCESS EXCLUSIVE`, which blocks reads as well "
            "as writes. Django wraps a migration in one transaction unless "
            "`atomic = False`, so a data pass placed after a schema change does "
            "not merely take its own time -- it extends the window in which "
            "that earlier lock is still held. A backfill of a few minutes turns "
            "a millisecond lock into a few-minute outage on the table, and "
            "queries that queue behind it hold their own locks while they wait."
        ),
        remediation=(
            "Move the data operation into a migration of its own, so the schema "
            "transaction commits and releases its locks before the backfill "
            "starts. Where the two genuinely must ship together, `atomic = "
            "False` on the migration lets each operation commit separately -- at "
            "the cost of partial application if one of them fails, so the "
            "operations then have to be individually safe to re-run. Reordering "
            "so the data pass comes first is only a fix when the backfill does "
            "not depend on the schema change."
        ),
        references=(
            "https://www.postgresql.org/docs/current/explicit-locking.html",
            "https://docs.djangoproject.com/en/stable/howto/writing-migrations/"
            "#non-atomic-migrations",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported. The live tier "
            "reads `django_migrations` and does not have to guess.",
            "How long the lock is held is how long the data operation takes, "
            "which is not knowable from source. A backfill over an empty table "
            "costs nothing and is reported the same as one over a hundred "
            "million rows.",
            "The lock modes named here are Postgres's. SQLite serialises "
            "writers regardless and has no comparable `ACCESS EXCLUSIVE`, so "
            "on SQLite this reports a shape that costs less than it says.",
            "An operation this tool does not recognise is not assumed to emit "
            "DDL, so a third-party lock-taking operation before a backfill is "
            "not reported.",
        ),
    )

    def _stream(self, ctx: ProjectContext, migration: MigrationNode) -> list[Applied]:
        """The operations of one migration in the order they reach the database.

        Taken from the replay rather than from `migration.operations`, because
        the two differ exactly where this rule is most likely to be wrong:
        `SeparateDatabaseAndState` contributes its database half, so a nested
        `AddField` takes its lock and a nested `RunPython` extends the hold,
        while the wrapper itself never reaches the database at all. Deriving
        the order a second time here would be a copy of `state.replay` that
        could drift away from the thing that runs.
        """
        return [a for a in ctx.migration_history if a.migration.key == migration.key]

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        migration = applied.migration
        if not migration.atomic:
            return
        op = applied.operation
        if op.kind not in DATA_KINDS:
            return

        stream = self._stream(ctx, migration)
        # One finding per migration: the window opens at the first data
        # operation that follows a lock, and moving that one is the fix.
        position = next(i for i, other in enumerate(stream) if other.operation is op)
        if any(other.operation.kind in DATA_KINDS for other in stream[:position]):
            return
        # A lock on a table this same deploy creates blocks nobody: no other
        # session can see the table until the transaction commits, and by then
        # the lock is gone. Without this a migration that creates its tables
        # and then seeds them -- the most common reason to mix the two at all
        # -- would be reported, and its remediation would be pure cost.
        blocking = [
            other.operation
            for other in stream[:position]
            if other.operation.kind in LOCKING_KINDS
            and other.operation.name not in TABLE_CREATING
            and self.populated(other)
        ]
        if not blocking:
            return

        tables = _tables(blocking)
        yield self.finding(
            severity=Severity.HIGH,
            location=self.locate(ctx, migration, op),
            message=(
                f"`{migration.app}.{migration.name}` runs `{_label(op)}` in the "
                f"same transaction as {len(blocking)} earlier schema "
                f"operation{'s' if len(blocking) != 1 else ''}, so the "
                f"`ACCESS EXCLUSIVE` lock on "
                f"{', '.join(f'`{table}`' for table in tables)} is held for as "
                f"long as this data pass takes. Move the data operation into "
                f"its own migration, or set `atomic = False`."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=" -> ".join(
                        [*(_label(other) for other in blocking), f"{_label(op)}  <-- data"]
                    ),
                    source=f"{ctx.rel(migration.path)}:{op.lineno}",
                ),
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=(
                        f"atomic = {migration.atomic}; Postgres holds a lock until the "
                        f"end of the transaction"
                    ),
                    source=f"{ctx.rel(migration.path)}:{op.lineno}",
                ),
            ),
        )
