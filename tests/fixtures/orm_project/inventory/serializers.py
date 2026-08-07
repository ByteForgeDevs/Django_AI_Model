"""Serializers for the ORM recall fixture.

`DJP-003` is the rule these exist for, and it is the subtlest in the family:
there is no loop anywhere in this file. The repetition lives inside DRF, which
calls a `SerializerMethodField` getter once per row, so a relation walked in
the getter costs one query per row of every list the serializer appears in.

The two serializers below are byte-for-byte the same question asked of two
different querysets, which is exactly why the rule cannot be written against
this file alone -- it has to find the view that uses the serializer.
"""

from rest_framework import serializers

from inventory.models import AuditEvent, Device, Site


class DeviceSerializer(serializers.ModelSerializer):
    """Its view's queryset does not fetch `site`, so `region` costs a query."""

    region = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = ["id", "name", "serial", "region"]

    def get_region(self, obj):
        return obj.site.region


class FetchedDeviceSerializer(serializers.ModelSerializer):
    """Control: identical getter, over a view that selects `site`."""

    region = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = ["id", "name", "serial", "region"]

    def get_region(self, obj):
        return obj.site.region


class AuditEventSerializer(serializers.ModelSerializer):
    """Names the model `AuditEventViewSet` lists.

    Its first draft reused `DeviceSerializer`, and both `DJD-003` and `DJP-010`
    went silent -- correctly, because a viewset with no `queryset` attribute is
    resolved through its serializer's `Meta.model`, and that said `Device`,
    which is ordered and has no `detail` column. The rules were right and the
    fixture was wrong.
    """

    class Meta:
        model = AuditEvent
        fields = ["id", "action", "detail", "created"]


class SiteSerializer(serializers.ModelSerializer):
    """Names the model `SiteViewSet` lists, so its control is about `Site`."""

    class Meta:
        model = Site
        fields = ["id", "name", "region"]
