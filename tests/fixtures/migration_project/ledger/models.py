"""The control app's models, twins of `billing`'s and equally ordinary."""

from django.db import models


class Entry(models.Model):
    reference = models.CharField(max_length=64, db_index=True)
    created = models.DateTimeField(auto_now_add=True)
    posted = models.BooleanField(default=False)
    attempts = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created"]
        indexes = [models.Index(fields=["reference", "created"])]
