"""Models for the injection recall fixture.

Deliberately dull. This project measures what happens to request data on its
way into a query, a shell, a template or a path, and the shape of the schema is
not part of that question. Every model declares `Meta.ordering` and no foreign
key cascades from the user, so the `DJD` family has nothing to say here.
"""

from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]


class Product(models.Model):
    name = models.CharField(max_length=200, db_index=True)
    sku = models.CharField(max_length=64, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")

    class Meta:
        ordering = ["name"]
