"""Views for the ORM recall fixture. Every function here is a planted defect.

The correctly-written twin of each one lives in `inventory/controls.py`, and
the manifest forbids any finding in that file at all. Keeping the two apart is
what lets `must_not_report` name a file instead of a line: a control pinned to
a line number stops guarding anything the first time someone adds an import,
and does it silently.

The viewsets are the exception -- their defect and its control are two classes
rather than two functions, so both live here and the manifest names their
lines.
"""

from django.http import JsonResponse
from rest_framework import viewsets
from rest_framework.filters import OrderingFilter

from inventory.models import AuditEvent, Device, Interface, Site
from inventory.serializers import (
    AuditEventSerializer,
    DeviceSerializer,
    FetchedDeviceSerializer,
)


def device_sites(request):
    """DJP-001: a forward relation followed once per row."""
    rows = []
    for device in Device.objects.all():
        rows.append({"device": device.name, "site": device.site.name})
    return JsonResponse({"rows": rows})


def device_interfaces(request):
    """DJP-002: a reverse relation evaluated once per row."""
    rows = []
    for device in Device.objects.all():
        rows.append({"device": device.name, "ports": len(device.interfaces.all())})
    return JsonResponse({"rows": rows})


def device_tags(request):
    """DJP-002 again, through a many-to-many rather than a reverse FK."""
    rows = []
    for device in Device.objects.all():
        rows.append({"device": device.name, "tags": [tag.slug for tag in device.tags.all()]})
    return JsonResponse({"rows": rows})


def interfaces_per_device(request):
    """DJP-004: a query issued at the manager, inside the loop."""
    rows = []
    for device in Device.objects.all():
        first = Interface.objects.filter(device=device, enabled=True).first()
        rows.append({"device": device.name, "first": first.name if first else None})
    return JsonResponse({"rows": rows})


def device_count(request):
    """DJP-005: `len()` over rows nothing ever reads."""
    total = len(Device.objects.filter(site__region="eu"))
    return JsonResponse({"total": total})


def site_has_devices(request, pk):
    """DJP-006: counting rows to find out whether there are any."""
    if Device.objects.filter(site_id=pk).count() > 0:
        return JsonResponse({"populated": True})
    return JsonResponse({"populated": False})


def rename_region(request):
    """DJP-007: one UPDATE per row."""
    for site in Site.objects.filter(region="emea"):
        site.region = "eu"
        site.save()
    return JsonResponse({"ok": True})


def device_serials(request):
    """DJP-009: a column explicitly not fetched, then read."""
    rows = []
    for device in Device.objects.only("name"):
        rows.append({"name": device.name, "serial": device.serial})
    return JsonResponse({"rows": rows})


class AuditEventViewSet(viewsets.ModelViewSet):
    """DJD-003 and DJP-010 together, on the one table that grows.

    The list is paginated by the project default and the model declares no
    ordering, so page boundaries move as rows arrive. The caller may also sort
    by `detail`, an unindexed `TextField`, making every page a full sort.

    The class-level `queryset` is what resolves the model. Written with only
    `get_queryset`, both rules went silent -- neither a `get_queryset` body nor
    the serializer's `Meta.model` is read for it -- and they were right to:
    a rule that cannot name the model cannot name the fix. `get_queryset` still
    scopes the rows, which is what keeps DJA-004 correctly quiet here.
    """

    queryset = AuditEvent.objects.all()
    serializer_class = AuditEventSerializer
    filter_backends = [OrderingFilter]
    ordering_fields = ["created", "detail"]

    def get_queryset(self):
        return AuditEvent.objects.filter(actor=self.request.user)


class DeviceViewSet(viewsets.ModelViewSet):
    """DJP-003: the serializer walks a relation this queryset never fetched."""

    queryset = Device.objects.all()
    serializer_class = DeviceSerializer

    def get_queryset(self):
        return Device.objects.filter(owner=self.request.user)


class FetchedDeviceViewSet(viewsets.ModelViewSet):
    """Control for DJP-003: the same getter, over a queryset that fetches."""

    queryset = Device.objects.select_related("site")
    serializer_class = FetchedDeviceSerializer

    def get_queryset(self):
        return Device.objects.filter(owner=self.request.user).select_related("site")


class RecentEventViewSet(viewsets.ModelViewSet):
    """Control for DJP-010: the caller may only sort by an indexed column.

    Its first draft was a viewset over `Site`, and it was worthless. DJP-010
    only speaks about models carrying a self-stamping timestamp, which is its
    evidence that a table accumulates rows, and `Site` has none -- so the
    control passed by being unreachable, which is indistinguishable from the
    rule being broken. It lists the same growing table as the reported viewset
    and differs only in which columns the caller may sort by.

    It pages like every other list endpoint, and so DJD-003 reports it too --
    correctly, because it is a second unordered paginated list of the same
    table. Its second draft turned pagination off to keep the finding count
    tidy, which traded DJD-003 for DJA-013: an endpoint returning every row is
    not a fix, and a fixture that hides one rule behind another is lying about
    both. Being a control for DJP-010 does not make it correct in every other
    respect, and the manifest says so by expecting both findings here.
    """

    queryset = AuditEvent.objects.all()
    serializer_class = AuditEventSerializer
    filter_backends = [OrderingFilter]
    ordering_fields = ["created"]
