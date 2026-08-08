"""Models for the migration fixture.

Deliberately ordinary. The defects in this project are in `migrations/`, and a
model that also tripped a `DJD` rule would make the manifest ambiguous about
which family found what.
"""

from django.db import models


class Invoice(models.Model):
    reference = models.CharField(max_length=64, db_index=True)
    created = models.DateTimeField(auto_now_add=True)
    settled = models.BooleanField(default=False)
    retries = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created"]
        indexes = [models.Index(fields=["reference", "created"])]
