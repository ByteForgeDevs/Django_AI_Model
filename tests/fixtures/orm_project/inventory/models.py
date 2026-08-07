"""Models for the ORM recall fixture.

The graph is deliberately small and deliberately complete: one forward chain
(`Interface` -> `Device` -> `Site`), one reverse relation, one many-to-many,
and one table that grows without bound. Between them they are every shape the
performance rules reason about, and no shape is here twice.

PLANTED DEFECT: `AuditEvent.actor` cascades from the user. Deleting a user
deletes the record of what that user did, which is `DJD-001` -- the one place
in this project where a *field* rather than a *query* is the defect.

PLANTED DEFECT: `AuditEvent` declares no `Meta.ordering`. That is not a finding
by itself -- plenty of models have no natural order -- but it becomes one the
moment a paginated endpoint lists it, which `config/urls.py` arranges. This is
the shape `DJD-003` has been waiting for since Phase 2: the rule shipped
reporting nothing in any fixture, and an unexamined zero is not a passing
control.

`Device.notes` is `null=True` on a text column with no `blank=True`, which is
`DJD-002`. Beside it, `decommissioned_by` is `null=True` *and* `blank=True` --
Django's documented way of saying the absence is meaningful -- so the rule has
to name one of the two and not the other, in the same model.
"""

from django.conf import settings
from django.db import models


class Site(models.Model):
    name = models.CharField(max_length=100, unique=True)
    region = models.CharField(max_length=50, db_index=True)

    class Meta:
        ordering = ["name"]


class Tag(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["slug"]


class Device(models.Model):
    name = models.CharField(max_length=100, db_index=True)
    serial = models.CharField(max_length=64, unique=True)
    # DJD-002: a nullable string column has two ways to be empty.
    notes = models.TextField(null=True)
    # Control: `null=True` with `blank=True` is Django's documented way to say
    # the absence is meaningful, so DJD-002 must list `notes` and not this.
    decommissioned_by = models.CharField(max_length=100, null=True, blank=True)
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="devices")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="devices"
    )
    tags = models.ManyToManyField(Tag, related_name="devices", blank=True)

    class Meta:
        ordering = ["name"]


class Interface(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="interfaces")
    name = models.CharField(max_length=64)
    mac = models.CharField(max_length=17, db_index=True)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]


class AuditEvent(models.Model):
    """Append-only history. Grows for the life of the deployment."""

    # DJD-001: deleting a user erases the evidence of what they did.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="audit_events"
    )
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="audit_events")
    action = models.CharField(max_length=32)
    # No index. DJP-010 cares that a caller can sort by this column.
    detail = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    # DJD-003: no ordering, and config/urls.py routes a paginated list of it.
