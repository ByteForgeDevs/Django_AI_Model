"""The control half's queries. Same questions, asked portably.

`latest_per_item` keeps the ordering and the intent of `distinct("sku")` and
pays for portability with a subquery, rather than dropping the requirement --
a control that simply asked a weaker question would not be the same query.
"""

from django.db import models, transaction
from django.db.models.functions import Trim

from warehouse.models import Item, Vendor


def latest_per_item():
    newest = (
        Item.objects.filter(sku=models.OuterRef("sku")).order_by("-id").values("pk")[:1]
    )
    return Item.objects.filter(pk__in=models.Subquery(newest))


def search(term):
    # Case-insensitivity asked for explicitly, so both backends agree.
    return Item.objects.filter(name__icontains=term)


def with_attribute(key):
    # `has_key` is supported on both backends; `contains` is not.
    return Item.objects.filter(attributes__has_key=key)


def reserve(sku):
    # The same read-modify-write, done as one statement. Both backends apply
    # it atomically, so the guarantee does not depend on a clause one of them
    # discards. The defect's lock is not merely weaker under SQLite -- it is
    # absent, and absent without an error.
    with transaction.atomic():
        return Item.objects.filter(sku=sku).update(name=Trim("name"))


def vendors_in(region):
    return Vendor.objects.filter(region=region)
