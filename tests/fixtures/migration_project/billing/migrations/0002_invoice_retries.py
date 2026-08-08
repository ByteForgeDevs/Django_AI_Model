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
    ]
