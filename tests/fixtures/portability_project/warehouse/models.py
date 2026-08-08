"""The control half. Each model is `catalog`'s twin, portable where it is not.

The pairs differ only in the thing the matching rule is about: `Item` carries
the same columns as `Product` without the Postgres-only field type and without
the deferred constraint, and nothing here is a general improvement on the
defect. A control that fixed two things at once would leave the rule's claim
untested for one of them.
"""

from django.db import models


class Item(models.Model):
    sku = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=200)
    tags = models.TextField(default="")
    attributes = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["sku"], name="warehouse_item_sku_unique")
        ]


class Vendor(models.Model):
    name = models.CharField(max_length=200)
    region = models.CharField(max_length=40)
