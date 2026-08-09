"""The defect half's queries. Each one runs differently on the two backends."""

from django.db import transaction

from catalog.models import Product, Supplier


def latest_per_supplier():
    # DJX-003: DISTINCT ON is Postgres-only and SQLite raises at query time.
    return Product.objects.order_by("sku", "-id").distinct("sku")


def search(term):
    # DJX-004: LIKE is case-insensitive on SQLite and case-sensitive on
    # Postgres, so this matches more rows in development than in production.
    return Product.objects.filter(name__contains=term)


def with_attribute(fragment):
    # DJX-002: `contains` on a JSONField is unsupported on SQLite.
    return Product.objects.filter(attributes__contains=fragment)


def reserve(sku):
    # DJX-007: no FOR UPDATE is emitted on SQLite, so this lock does nothing
    # in development and nothing raises to say so.
    with transaction.atomic():
        product = Product.objects.select_for_update().get(sku=sku)
        product.name = product.name.strip()
        product.save()
        return product


def priced_in_usd():
    # DJX-008: `\b` is a word boundary to Python's `re`, which is what SQLite
    # uses, and a literal backspace to Postgres' POSIX engine. Neither engine
    # raises; they just match different rows.
    return Product.objects.filter(name__regex=r"\bUSD\b")


def suppliers_in(region):
    return Supplier.objects.filter(region=region)
