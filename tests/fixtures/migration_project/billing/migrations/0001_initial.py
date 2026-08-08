"""Creates the table. Already applied, and not a leaf, so nothing here reports."""

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Invoice",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ("reference", models.CharField(db_index=True, max_length=64)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("settled", models.BooleanField(default=False)),
            ],
        ),
    ]
