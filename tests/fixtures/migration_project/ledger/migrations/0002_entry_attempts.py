"""The `DJM-001` control: the same column, added the way that works.

Identical to `billing.0002_invoice_retries` in every respect the rule does not
speak about -- same field type, same leaf position, same populated table -- and
differs only in carrying a `default`. On Postgres 11 and later that is a
catalogue-only change, so it is both correct and fast.

`scripts/fixture_controls_probe.py` removes the `default=0` and requires
`DJM-001` to then report this file, so the control is known to be reachable
rather than assumed to be.
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
    ]
