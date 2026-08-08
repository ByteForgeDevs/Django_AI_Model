"""The defect half. Every model here is written against Postgres only."""

from django.contrib.postgres.fields import ArrayField
from django.db import models


class Product(models.Model):
    sku = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=200)
    tags = ArrayField(models.CharField(max_length=40), default=list)
    attributes = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["sku"],
                name="catalog_product_sku_deferred",
                deferrable=models.Deferrable.DEFERRED,
            )
        ]


class Supplier(models.Model):
    name = models.CharField(max_length=200)
    region = models.CharField(max_length=40)
