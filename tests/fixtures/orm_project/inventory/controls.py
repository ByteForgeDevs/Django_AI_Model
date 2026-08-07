"""Every defect in `views.py`, written correctly.

Nothing in this file may ever be reported, and the manifest says so by naming
the whole file in `must_not_report` rather than particular lines. That is a
deliberate structural choice: line-numbered controls rot the moment anyone adds
an import, and a control that quietly stops pointing at the thing it guards has
become decoration.

These pairs are what make the performance family worth having. Detecting that a
loop mentions a relation is easy and useless -- the question is whether the
fetch that covers it is there, and every function below is the same query as its
twin in `views.py` with that fetch present.

Reachability was measured rather than assumed. Removing the fix from each
function here makes the matching rule report it, so each control is a rule
declining to speak about code it can see, not a rule that never looked.
"""

from django.db.models import Prefetch
from django.http import JsonResponse

from inventory.models import AuditEvent, Device, Interface, Site


def device_sites_fetched(request):
    """Control for DJP-001: the same loop, with the relation fetched."""
    rows = []
    for device in Device.objects.select_related("site"):
        rows.append({"device": device.name, "site": device.site.name})
    return JsonResponse({"rows": rows})


def device_interfaces_fetched(request):
    """Control for DJP-002: prefetched, so the loop costs two queries."""
    rows = []
    for device in Device.objects.prefetch_related("interfaces"):
        rows.append({"device": device.name, "ports": len(device.interfaces.all())})
    return JsonResponse({"rows": rows})


def device_tags_fetched(request):
    """Control for DJP-002 through a many-to-many, fetched by `Prefetch`."""
    rows = []
    for device in Device.objects.prefetch_related(Prefetch("tags")):
        rows.append({"device": device.name, "tags": [tag.slug for tag in device.tags.all()]})
    return JsonResponse({"rows": rows})


def interface_sites(request):
    """Control: a `Prefetch` whose queryset reaches a second level.

    Reading `interface.device.site` from a plain `prefetch_related("device")`
    would still cost one query per interface. The nested `select_related`
    inside the `Prefetch` queryset is what covers it, so a rule that reads only
    the outer relation name will report this and be wrong.
    """
    rows = []
    fetched = Interface.objects.prefetch_related(
        Prefetch("device", queryset=Device.objects.select_related("site"))
    )
    for interface in fetched:
        rows.append({"mac": interface.mac, "site": interface.device.site.name})
    return JsonResponse({"rows": rows})


def enabled_interfaces(request):
    """Control: `to_attr` renames the prefetched set and the loop reads it.

    Measured in `scripts/prefetch_cache_probe.py`: reading the *original*
    relation after a `to_attr` prefetch is not served by it and costs a query
    per row, so the name the loop reads is the whole question here.
    """
    rows = []
    fetched = Device.objects.prefetch_related(
        Prefetch("interfaces", queryset=Interface.objects.filter(enabled=True), to_attr="live")
    )
    for device in fetched:
        rows.append({"device": device.name, "live": len(device.live)})
    return JsonResponse({"rows": rows})


def device_names(request):
    """Control for DJP-005: `len()` where the rows are read afterwards."""
    devices = Device.objects.filter(site__region="eu")
    total = len(devices)
    return JsonResponse({"total": total, "names": [device.name for device in devices]})


def site_has_devices_exists(request, pk):
    """Control for DJP-006: the same question asked with `.exists()`."""
    if Device.objects.filter(site_id=pk).exists():
        return JsonResponse({"populated": True})
    return JsonResponse({"populated": False})


def rename_region_bulk(request):
    """Control for DJP-007: one UPDATE instead of one per row."""
    sites = list(Site.objects.filter(region="emea"))
    for site in sites:
        site.region = "eu"
    Site.objects.bulk_update(sites, ["region"])
    return JsonResponse({"ok": True})


def export_events(request):
    """Control for DJP-008: the same whole-table read, in a request handler.

    DJP-008 speaks only inside migrations, management commands and scheduled
    tasks, reasoning that a table small enough to render in a response is small
    enough to hold. The reported case is the management command; this is the
    line either side of that judgement.
    """
    lines = [event.action for event in AuditEvent.objects.all()]
    return JsonResponse({"lines": lines})


def device_serials_fetched(request):
    """Control for DJP-009: `only()` naming every column the loop reads."""
    rows = []
    for device in Device.objects.only("name", "serial"):
        rows.append({"name": device.name, "serial": device.serial})
    return JsonResponse({"rows": rows})
