"""The planted defect: a non-nullable column with nothing to fill it.

`retries` is an `IntegerField` with no `default` and `null` left at False, so
`ADD COLUMN ... NOT NULL` has no value for the rows `0001_initial` created.
Postgres rejects the statement and the migration aborts. This is the leaf of
`billing`'s history, which is where a migration being written now lands, so it
is in scope for the static tier.

Hand-written on purpose: `makemigrations` prompts for a one-off default and so
cannot produce this, which is exactly why the rule has to catch it here.
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
    ]
