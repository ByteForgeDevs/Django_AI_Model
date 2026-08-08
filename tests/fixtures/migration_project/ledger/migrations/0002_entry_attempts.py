"""The `DJM-001` control: the same column, added the way that works.

Identical to `billing.0002_invoice_retries` in every respect the rule does not
speak about -- same field type, same leaf position, same populated table -- and
differs only in carrying a `default`. On Postgres 11 and later that is a
catalogue-only change, so it is both correct and fast.

The `DJM-002` control is the `AlterField` below: it widens `reference` from
`max_length=64` to 128, which Postgres has skipped rewriting since 9.2. Its
twin in `billing` narrows the same column to 32.

`scripts/fixture_controls_probe.py` removes each fix in turn -- the `default=0`,
and the widened limit -- and requires the matching rule to then report this
file, so both controls are known to be reachable rather than assumed to be.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
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
    ]
