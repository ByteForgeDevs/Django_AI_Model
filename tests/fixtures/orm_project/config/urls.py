"""URLconf for the ORM recall fixture.

Nothing here is a defect, but the routes are load-bearing for rules that would
otherwise have nothing to say. `DJD-003` and `DJP-010` are both about what a
*list endpoint* does, and an unrouted viewset is not an endpoint -- so without
this file the two would pass the fixture by being silent, which is precisely
the failure the eval harness exists to prevent.

The control views are routed too. An unreachable control is not a control: it
passes by being invisible, which is indistinguishable from the rule being
broken.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from inventory import controls, views

router = DefaultRouter()
router.register("devices", views.DeviceViewSet, basename="device")
router.register("fetched-devices", views.FetchedDeviceViewSet, basename="fetched-device")
router.register("recent-events", views.RecentEventViewSet, basename="recent-event")
router.register("events", views.AuditEventViewSet, basename="event")

urlpatterns = [
    path("api/", include(router.urls)),
    # The defects.
    path("reports/device-sites/", views.device_sites),
    path("reports/device-interfaces/", views.device_interfaces),
    path("reports/device-tags/", views.device_tags),
    path("reports/interfaces-per-device/", views.interfaces_per_device),
    path("reports/device-count/", views.device_count),
    path("reports/sites/<int:pk>/populated/", views.site_has_devices),
    path("reports/rename-region/", views.rename_region),
    path("reports/device-serials/", views.device_serials),
    # The same queries, written correctly.
    path("controls/device-sites/", controls.device_sites_fetched),
    path("controls/device-interfaces/", controls.device_interfaces_fetched),
    path("controls/device-tags/", controls.device_tags_fetched),
    path("controls/interface-sites/", controls.interface_sites),
    path("controls/enabled-interfaces/", controls.enabled_interfaces),
    path("controls/device-names/", controls.device_names),
    path("controls/sites/<int:pk>/populated/", controls.site_has_devices_exists),
    path("controls/rename-region/", controls.rename_region_bulk),
    path("controls/export-events/", controls.export_events),
    path("controls/device-serials/", controls.device_serials_fetched),
]
