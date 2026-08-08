"""Creates the control table, mirroring `billing.0001_initial`."""

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Entry",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ("reference", models.CharField(db_index=True, max_length=64)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("posted", models.BooleanField(default=False)),
            ],
        ),
    ]
