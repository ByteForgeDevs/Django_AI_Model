"""Views for the injection recall fixture. Every function here is a defect.

Twelve rules, twelve planted defects, one per rule and no more, so a finding
here identifies exactly which rule found it. The sanitised near-miss for each
one lives in `shop/controls.py`, and the manifest forbids any finding in that
file at all.

This is the whole recall story for the `DJI` family. Before this fixture
existed, all twelve rules had their recall measured only by unit tests and by
mutation -- and four of them (`DJI-009` through `DJI-012`) report nothing on any
of the three benchmark corpora, by construction, because mature projects do not
leave these open. A rule whose only evidence is its own test file has been
checked against the author's idea of the defect rather than against the defect.
"""

import os
import pickle
import subprocess

import requests
from django.db import connection
from django.db.models.expressions import RawSQL
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils.safestring import mark_safe

from shop.models import Product

MEDIA_ROOT = "/srv/shop/media"


def product_search(request):
    """DJI-001: request data interpolated into a SQL statement."""
    term = request.GET["q"]
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, name FROM shop_product WHERE name LIKE '%{term}%'")
        rows = cursor.fetchall()
    return JsonResponse({"rows": rows})


def product_by_sku(request):
    """DJI-002: request data interpolated into a .raw() query."""
    sku = request.GET["sku"]
    found = Product.objects.raw("SELECT * FROM shop_product WHERE sku = '%s'" % sku)
    return JsonResponse({"names": [p.name for p in found]})


def products_over(request):
    """DJI-003: request data interpolated into an .extra() clause."""
    floor = request.GET["min_price"]
    found = Product.objects.extra(where=["price > " + floor])
    return JsonResponse({"names": [p.name for p in found]})


def products_ranked(request):
    """DJI-004: request data interpolated into a RawSQL expression."""
    column = request.GET["weight"]
    found = Product.objects.annotate(rank=RawSQL(f"price * {column}", ()))
    return JsonResponse({"names": [p.name for p in found]})


def product_filter(request):
    """DJI-005: request data expanded into queryset keyword arguments."""
    found = Product.objects.filter(**request.GET.dict())
    return JsonResponse({"names": [p.name for p in found]})


def product_sorted(request):
    """DJI-006: the sort column chosen by request data with no allowlist."""
    found = Product.objects.all().order_by(request.GET["sort"])
    return JsonResponse({"names": [p.name for p in found]})


def restore_cart(request):
    """DJI-007: request data handed to an executing loader."""
    blob = request.POST["cart"]
    cart = pickle.loads(bytes.fromhex(blob))
    return JsonResponse({"items": len(cart)})


def convert_image(request):
    """DJI-008: request data used to build a shell command."""
    name = request.GET["name"]
    subprocess.run(f"convert /srv/shop/media/{name} -resize 100x100 /tmp/out.png", shell=True)
    return JsonResponse({"ok": True})


def fetch_feed(request):
    """DJI-009: request data chooses the host of an outbound request."""
    answer = requests.get(request.GET["feed"], timeout=5)
    return JsonResponse({"status": answer.status_code})


def after_login(request):
    """DJI-010: the redirect target chosen by request data, unchecked."""
    return redirect(request.GET["next"])


def product_banner(request):
    """DJI-011: request data marked as trusted HTML."""
    return HttpResponse(mark_safe(f"<b>{request.GET['label']}</b>"))


def download_invoice(request):
    """DJI-012: the file path chosen by request data.

    The constant base is no defence, measured by construction: an absolute
    later part discards it and `..` climbs out of it.
    """
    with open(os.path.join(MEDIA_ROOT, request.GET["file"])) as handle:
        return HttpResponse(handle.read())
