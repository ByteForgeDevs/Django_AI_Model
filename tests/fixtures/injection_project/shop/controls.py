"""Every defect in `views.py`, written safely. Nothing here may be reported.

These are the near-misses that decide whether the family is worth shipping. The
tainted value reaches the same sink in every one of them -- what differs is
that it arrives as a parameter, through an allowlist, or after a sanitiser. A
rule that reports these has not detected injection; it has detected that
request data and a dangerous call appear in the same function, which describes
most of every Django project ever written.

Reachability is measured rather than assumed by
`scripts/fixture_controls_probe.py`: removing the guard from each function
below makes the matching rule report it.
"""

import json
import subprocess
from urllib.parse import urlencode

import requests
from django.core.files.storage import default_storage
from django.db import connection
from django.db.models.expressions import RawSQL
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils.html import escape, format_html
from django.utils.http import url_has_allowed_host_and_scheme

from shop.models import Product

SORTABLE = {"name": "name", "price": "price", "newest": "-id"}
FEED_HOST = "https://feeds.example.test"


def product_search(request):
    """Control for DJI-001: the term is a parameter, not part of the text."""
    term = request.GET["q"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, name FROM shop_product WHERE name LIKE %s", [f"%{term}%"])
        rows = cursor.fetchall()
    return JsonResponse({"rows": rows})


def product_by_sku(request):
    """Control for DJI-002: `.raw()` takes params of its own."""
    found = Product.objects.raw(
        "SELECT * FROM shop_product WHERE sku = %s", [request.GET["sku"]]
    )
    return JsonResponse({"names": [p.name for p in found]})


def products_over(request):
    """Control for DJI-003: `.extra()` has a `params` argument for this."""
    found = Product.objects.extra(
        where=["price > %s"], params=[request.GET["min_price"]]
    )
    return JsonResponse({"names": [p.name for p in found]})


def products_ranked(request):
    """Control for DJI-004: `RawSQL` takes its parameters separately."""
    found = Product.objects.annotate(
        rank=RawSQL("price * %s", (request.GET["weight"],))
    )
    return JsonResponse({"names": [p.name for p in found]})


def product_filter(request):
    """Control for DJI-005: the project chooses the lookups, not the caller."""
    criteria = {}
    if "name" in request.GET:
        criteria["name__icontains"] = request.GET["name"]
    if "sku" in request.GET:
        criteria["sku"] = request.GET["sku"]
    found = Product.objects.filter(**criteria)
    return JsonResponse({"names": [p.name for p in found]})


def product_sorted(request):
    """Control for DJI-006: the caller names a key, the project picks the column.

    This is the shape that matters. The caller still decides the sort, so a
    rule keyed on "the request influences order_by" reports it -- but the value
    that reaches the ORM came out of a dict this module wrote, and nothing the
    caller sends can produce a column that is not in it.
    """
    column = SORTABLE.get(request.GET.get("sort", "name"), "name")
    found = Product.objects.all().order_by(column)
    return JsonResponse({"names": [p.name for p in found]})


def product_sorted_checked(request):
    """Control for DJI-006 again, in the shape the corpus actually writes.

    Both benchmark projects that let a request choose an ordering guard it this
    way -- compare against a container, substitute a default -- so the rule has
    to accept it as well as the mapping lookup above. Two idioms, one rule, and
    the fixture carries both because only one of them was ever exercised by
    real code.
    """
    column = request.GET.get("sort", "name")
    if column not in SORTABLE:
        column = "name"
    found = Product.objects.all().order_by(column)
    return JsonResponse({"names": [p.name for p in found]})


def restore_cart(request):
    """Control for DJI-007: a parser that only ever parses."""
    cart = json.loads(request.POST["cart"])
    return JsonResponse({"items": len(cart)})


def convert_image(request):
    """Control for DJI-008: an argument list, and no shell to interpret it."""
    name = request.GET["name"]
    subprocess.run(
        ["convert", f"/srv/shop/media/{name}", "-resize", "100x100", "/tmp/out.png"],
        check=True,
    )
    return JsonResponse({"ok": True})


def fetch_feed(request):
    """Control for DJI-009: the host is ours and the request names the path.

    Measured while building the rule: nothing appended to a URL can move its
    authority once the authority has been written, so a constant host followed
    by caller-supplied path or query is not an SSRF.
    """
    answer = requests.get(f"{FEED_HOST}/feeds?{urlencode({'id': request.GET['feed']})}", timeout=5)
    return JsonResponse({"status": answer.status_code})


def after_login(request):
    """Control for DJI-010: Django ships the host check this needs."""
    target = request.GET["next"]
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect("/")


def product_banner(request):
    """Control for DJI-011: two ways of not trusting the same value.

    `format_html` escapes its *arguments* and not its format string, measured
    against the installed Django rather than assumed -- so the argument
    position a naive lint flags is the safe one.
    """
    label = escape(request.GET["label"])
    return HttpResponse(format_html("<b>{}</b> {}", request.GET["label"], label))


def download_invoice(request):
    """Control for DJI-012: Django's storage API refuses traversal itself.

    Measured by construction: `safe_join` raises `SuspiciousFileOperation` on
    both `..` and an absolute name, and `FileSystemStorage.path` is literally
    `safe_join(self.location, name)`.
    """
    with default_storage.open(request.GET["file"]) as handle:
        return HttpResponse(handle.read())
