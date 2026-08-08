"""The `DJM-001` control: the same column, added the way that works.

The `DJM-009` control is the `AddConstraintNotValid` at the end. It records the
same check constraint `billing` adds outright, but with `NOT VALID`, so the
lock is held only long enough to write the catalogue entry; a later
`ValidateConstraint` runs the scan under `SHARE UPDATE EXCLUSIVE`. Rows written
after it exists are checked from that moment, so only the pre-existing rows are
ever deferred.


Identical to `billing.0002_invoice_retries` in every respect the rule does not
speak about -- same field type, same leaf position, same populated table -- and
differs only in carrying a `default`. On Postgres 11 and later that is a
catalogue-only change, so it is both correct and fast.

The `DJM-002` control is the `AlterField` below: it widens `reference` from
`max_length=64` to 128, which Postgres has skipped rewriting since 9.2. Its
twin in `billing` narrows the same column to 32.

The `DJM-003` control is the `AddIndexConcurrently` below, which builds the
same index `billing` builds with `AddIndex` -- but under `SHARE UPDATE
EXCLUSIVE`, which blocks neither reads nor writes. It is why this migration
declares `atomic = False`: the operation refuses to run inside a transaction,
so the flag is part of the fix rather than an unrelated detail.

The `DJM-004` control is the `SeparateDatabaseAndState` below. It drops
`posted`, the twin of the column `billing` drops outright, but wrapped in the
`database_operations` half -- which says the matching `state_operations` half
already shipped in an earlier release, so nothing still running selects the
column. The replay flattens that wrapper away, so this arrives at the rule as a
bare `RemoveField` and is told apart from the reckless version only by its
provenance. That makes it the control the rule most needs.

The `DJM-005` control is the `RenameField` below, with the `AlterField` that
follows it. It renames `created` to `opened` exactly as `billing` does, then
pins the column with `db_column="created"` -- so the attribute moves and the
data does not, and Django emits no rename statement at all because
`_alter_field` guards it with `old_field.column != new_field.column`. The pin
is written after the rename because that is the order `makemigrations`
produces: the autodetector runs `generate_renamed_fields()` before
`generate_altered_fields()`.

The `DJM-006` control is the `RunPython` below. It is the same backfill
`billing` runs, carrying `reverse_code=migrations.RunPython.noop` -- which is
honest here rather than a formality, because unapplying this migration drops
the column the backfill wrote, so undoing the data pass really is a no-op. Note
what the control is *not*: it is not a bare `RunPython` moved into its own
migration, because the rule's other remediation is exactly that, and a control
built from it would be measuring a different fix from the one named here.

The `DJM-007` control is `recount_attempts`, which does the same work as
`billing`'s `recount_retries` behind `.iterator(chunk_size=500)` and flushes
each chunk rather than accumulating the whole table. Both halves matter: the
iterator bounds the fetch, and the flush bounds the list the fetch feeds.

The `DJM-008` control is `atomic = False` on the line below, which is already
here because `AddIndexConcurrently` cannot run inside a transaction. One line
therefore serves two rules, and the probe un-fixes it once and checks both:
without it Postgres would hold every lock these schema operations take until
the backfill at the end of the list finished.

`scripts/fixture_controls_probe.py` removes each fix in turn -- the `default=0`,
the widened limit, the concurrency, the wrapper, the pin, the reverse and the iterator -- and
requires the matching rule to then report this file, so every control is known
to be reachable rather than assumed to be.
"""

from django.contrib.postgres.operations import AddConstraintNotValid, AddIndexConcurrently
from django.db import migrations, models


def backfill_attempts(apps, schema_editor):
    """Fill the column this migration adds, for the rows `0001_initial` created."""
    Entry = apps.get_model("ledger", "Entry")
    Entry.objects.filter(attempts__isnull=True).update(attempts=0)


def recount_attempts(apps, schema_editor):
    """Reset the counter for every row, holding one chunk at a time."""
    Entry = apps.get_model("ledger", "Entry")
    batch = []
    for entry in Entry.objects.filter(attempts__isnull=True).iterator(chunk_size=500):
        entry.attempts = 0
        batch.append(entry)
        if len(batch) >= 500:
            Entry.objects.bulk_update(batch, ["attempts"])
            batch = []
    Entry.objects.bulk_update(batch, ["attempts"])


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("ledger", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="entry",
            name="attempts",
            field=models.IntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="entry",
            name="reference",
            field=models.CharField(db_index=True, max_length=128),
        ),
        AddIndexConcurrently(
            model_name="entry",
            index=models.Index(fields=["reference"], name="ledger_entry_ref_idx"),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RemoveField(model_name="entry", name="posted")],
        ),
        migrations.RenameField(
            model_name="entry",
            old_name="created",
            new_name="opened",
        ),
        migrations.AlterField(
            model_name="entry",
            name="opened",
            field=models.DateTimeField(auto_now_add=True, db_column="created"),
        ),
        migrations.RunPython(
            code=backfill_attempts,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RunPython(
            code=recount_attempts,
            reverse_code=migrations.RunPython.noop,
        ),
        AddConstraintNotValid(
            model_name="entry",
            constraint=models.CheckConstraint(
                condition=models.Q(attempts__gte=0),
                name="ledger_entry_attempts_nonneg",
            ),
        ),
    ]
