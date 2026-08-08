"""Two planted defects, both in the leaf of `billing`'s history.

`retries` is an `IntegerField` with no `default` and `null` left at False, so
`ADD COLUMN ... NOT NULL` has no value for the rows `0001_initial` created.
Postgres rejects the statement and the migration aborts. This is the leaf of
`billing`'s history, which is where a migration being written now lands, so it
is in scope for the static tier.

Hand-written on purpose: `makemigrations` prompts for a one-off default and so
cannot produce this, which is exactly why the rule has to catch it here.

`DJM-002` is the second: narrowing `reference` from `max_length=64` to 32 makes
Postgres re-verify every existing value, which rewrites the table under
`ACCESS EXCLUSIVE`. Its twin in `ledger` widens the same column instead, which
Postgres has skipped rewriting since 9.2 -- the pair differs only in the
direction of the change.
`DJM-003` is the third: `AddIndex` on `invoice`, a table `0001_initial`
created and filled, so Postgres builds the index under a `SHARE` lock and
blocks every write until it finishes. Its twin in `ledger` uses
`AddIndexConcurrently` on a non-atomic migration, which is the same index built
without blocking anything.

`DJM-004` is the fourth: dropping `settled` in one step. Django names every
concrete column in its `SELECT`, so for the length of a rolling deploy every
query the previous release makes against `invoice` fails -- not only those
reading `settled`. Its twin in `ledger` drops the equivalent column through the
`SeparateDatabaseAndState` half that says the state change already shipped, and
the two are otherwise the same operation on the same kind of column.

`DJM-005` is the fifth: renaming `created` to `opened`, which moves the column
the previous release still names in every `SELECT` it makes. Its twin in
`ledger` renames the same column and then pins it with `db_column`, so the
attribute moves and the column does not.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="retries",
            field=models.IntegerField(),
        ),
        migrations.AlterField(
            model_name="invoice",
            name="reference",
            field=models.CharField(db_index=True, max_length=32),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(fields=["reference"], name="billing_invoice_ref_idx"),
        ),
        migrations.RemoveField(
            model_name="invoice",
            name="settled",
        ),
        migrations.RenameField(
            model_name="invoice",
            old_name="created",
            new_name="opened",
        ),
    ]
