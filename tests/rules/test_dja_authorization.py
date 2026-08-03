"""DJA -- authorization rules, and the shapes they must not report.

The benchmarks are near-silent here by design: NetBox and pretix both set a
strict permission default and scope their querysets, so almost every one of
these rules correctly finds nothing there. That makes this file the only place
recall is demonstrated, so every rule below is shown firing on a project built
to trigger it *and* staying silent on the nearest correct thing -- which for
this family is usually one keyword away from the defect.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Finding, Severity
from djaudit.registry import all_rules
from djaudit.rules._api import production_settings
from tests.api.test_permissions import (
    DRF_DECORATORS,
    DRF_PERMISSION_SOURCE,
    DRF_ROUTERS,
    DRF_VIEWS,
    DRF_VIEWSETS,
    SETTINGS_MARKERS,
)

STRICT = SETTINGS_MARKERS + (
    "REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES':"
    " ['rest_framework.permissions.IsAuthenticated']}\n"
)

OPEN_DEFAULT = SETTINGS_MARKERS + (
    "REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny']}\n"
)

NO_DRF_SETTING = SETTINGS_MARKERS

MODELS = """
from django.db import models
from django.conf import settings

class Note(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.TextField()

class Tag(models.Model):
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=50)

class Region(models.Model):
    name = models.CharField(max_length=50)
"""

ROUTED = """
from rest_framework import routers
from shop.api import NoteViewSet

router = routers.DefaultRouter()
router.register('notes', NoteViewSet)
"""


def project(make_project, api_source: str, settings: str = STRICT, urls: str = ROUTED):
    built: ProjectContext = make_project(
        {
            "manage.py": "",
            "rest_framework/__init__.py": "",
            "rest_framework/views.py": DRF_VIEWS,
            "rest_framework/generics.py": DRF_VIEWS,
            "rest_framework/viewsets.py": DRF_VIEWSETS,
            "rest_framework/permissions.py": DRF_PERMISSION_SOURCE,
            "rest_framework/decorators.py": DRF_DECORATORS,
            "rest_framework/routers.py": DRF_ROUTERS,
            "rest_framework/serializers.py": "class ModelSerializer:\n    pass\n",
            "shop/__init__.py": "",
            "shop/models.py": MODELS,
            "shop/api.py": api_source,
            "shop/urls.py": urls,
            "shop/settings.py": settings,
        }
    )
    return built


def run(make_project, rule_id: str, api_source: str, **kwargs) -> list[Finding]:
    ctx = project(make_project, api_source, **kwargs)
    rule = next(r for r in all_rules() if r.meta.id == rule_id)()
    return list(rule.check(ctx))


VIEWSET = """
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Note.objects.filter(owner=self.request.user)
"""


class TestProductionSettings:
    def test_prefers_the_entrypoint_over_a_base(self, make_project) -> None:
        ctx = project(make_project, VIEWSET)
        view = production_settings(ctx)
        assert view is not None
        assert view.module.path.name == "settings.py"

    def test_none_when_nothing_resolves(self, make_project) -> None:
        ctx: ProjectContext = make_project({"shop/__init__.py": ""})
        assert production_settings(ctx) is None


class TestDefaultPermissions:
    """DJA-001 -- the one line that decides the whole project."""

    def test_reports_an_explicit_allow_any(self, make_project) -> None:
        found = run(make_project, "DJA-001", VIEWSET, settings=OPEN_DEFAULT)
        assert len(found) == 1
        assert "AllowAny" in found[0].message

    def test_reports_the_setting_being_absent(self, make_project) -> None:
        found = run(make_project, "DJA-001", VIEWSET, settings=NO_DRF_SETTING)
        assert len(found) == 1
        assert "DRF's own default applies" in found[0].message

    def test_silent_when_a_strict_default_is_set(self, make_project) -> None:
        assert not run(make_project, "DJA-001", VIEWSET, settings=STRICT)

    def test_reported_once_however_many_views_exist(self, make_project) -> None:
        source = VIEWSET + "\n".join(
            f"class Extra{i}(viewsets.ModelViewSet):\n    queryset = Note.objects.all()\n"
            for i in range(5)
        )
        found = run(make_project, "DJA-001", source, settings=OPEN_DEFAULT)
        assert len(found) == 1

    def test_severity_drops_when_no_view_relies_on_it(self, make_project) -> None:
        found = run(make_project, "DJA-001", VIEWSET, settings=OPEN_DEFAULT)
        assert found[0].severity is Severity.LOW

    def test_severity_rises_when_a_view_relies_on_it(self, make_project) -> None:
        source = VIEWSET.replace("    permission_classes = [IsAuthenticated]\n", "")
        found = run(make_project, "DJA-001", source, settings=OPEN_DEFAULT)
        assert found[0].severity is Severity.HIGH
        assert "1 of 1 routed views" in found[0].message

    def test_silent_on_a_project_with_no_views(self, make_project) -> None:
        ctx: ProjectContext = make_project(
            {"manage.py": "", "shop/__init__.py": "", "shop/settings.py": OPEN_DEFAULT}
        )
        rule = next(r for r in all_rules() if r.meta.id == "DJA-001")()
        assert not list(rule.check(ctx))

    def test_points_at_the_module_defining_the_setting(self, make_project) -> None:
        found = run(make_project, "DJA-001", VIEWSET, settings=OPEN_DEFAULT)
        assert found[0].location.file.endswith("settings.py")
        assert found[0].location.line > 1


class TestViewWithoutPermissions:
    """DJA-002 -- the view that never said anything."""

    NAKED = """
from rest_framework import viewsets
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
"""

    def test_reports_a_view_inheriting_an_open_default(self, make_project) -> None:
        found = run(make_project, "DJA-002", self.NAKED, settings=OPEN_DEFAULT)
        assert len(found) == 1
        assert "declares no permission_classes" in found[0].message

    def test_reports_a_view_inheriting_drfs_own_default(self, make_project) -> None:
        found = run(make_project, "DJA-002", self.NAKED, settings=NO_DRF_SETTING)
        assert len(found) == 1
        assert "DRF's own default" in found[0].message

    def test_silent_under_a_strict_default(self, make_project) -> None:
        assert not run(make_project, "DJA-002", self.NAKED, settings=STRICT)

    def test_silent_when_the_view_declares_its_own(self, make_project) -> None:
        assert not run(make_project, "DJA-002", VIEWSET, settings=OPEN_DEFAULT)

    def test_silent_when_an_ancestor_declares_it(self, make_project) -> None:
        source = """
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from shop.models import Note

class Guarded(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

class NoteViewSet(Guarded):
    queryset = Note.objects.all()
"""
        assert not run(make_project, "DJA-002", source, settings=OPEN_DEFAULT)

    def test_silent_on_an_unrouted_view(self, make_project) -> None:
        assert not run(
            make_project, "DJA-002", self.NAKED, settings=OPEN_DEFAULT, urls="urlpatterns = []\n"
        )

    def test_writable_endpoints_are_graded_higher(self, make_project) -> None:
        found = run(make_project, "DJA-002", self.NAKED, settings=OPEN_DEFAULT)
        assert found[0].severity is Severity.HIGH


class TestOpenWritableEndpoint:
    """DJA-003 -- the view that said 'anyone' out loud."""

    EMPTY = """
from rest_framework import viewsets
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
    permission_classes = []
"""

    def test_reports_an_empty_permission_list(self, make_project) -> None:
        found = run(make_project, "DJA-003", self.EMPTY)
        assert len(found) == 1
        assert found[0].severity is Severity.CRITICAL
        assert "POST" in found[0].message

    def test_reports_an_explicit_allow_any(self, make_project) -> None:
        source = self.EMPTY.replace(
            "permission_classes = []",
            "permission_classes = [AllowAny]",
        ).replace(
            "from shop.models import Note",
            "from shop.models import Note\nfrom rest_framework.permissions import AllowAny",
        )
        found = run(make_project, "DJA-003", source)
        assert len(found) == 1
        assert "AllowAny" in found[0].message

    def test_silent_when_the_opening_came_from_the_default(self, make_project) -> None:
        """DJA-002 owns that case; two findings on one line is one too many."""
        naked = TestViewWithoutPermissions.NAKED
        assert not run(make_project, "DJA-003", naked, settings=OPEN_DEFAULT)

    def test_silent_on_a_read_only_route(self, make_project) -> None:
        source = """
from rest_framework import viewsets
from shop.models import Note

class NoteViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Note.objects.all()
    permission_classes = []
"""
        assert not run(make_project, "DJA-003", source)

    def test_silent_when_a_permission_restricts(self, make_project) -> None:
        assert not run(make_project, "DJA-003", VIEWSET)


class TestUnscopedQueryset:
    """DJA-004 -- every caller sees everyone's rows."""

    UNSCOPED = """
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Note.objects.all()
"""

    def test_reports_a_list_endpoint_over_a_user_owned_model(self, make_project) -> None:
        found = run(make_project, "DJA-004", self.UNSCOPED)
        assert len(found) == 1
        assert "everyone's records" in found[0].message
        assert found[0].severity is Severity.HIGH

    def test_silent_when_the_queryset_is_scoped(self, make_project) -> None:
        assert not run(make_project, "DJA-004", VIEWSET)

    def test_silent_when_scoping_goes_through_a_local(self, make_project) -> None:
        source = self.UNSCOPED.replace(
            "        return Note.objects.all()",
            "        qs = Note.objects.all()\n        return qs.filter(owner=self.request.user)",
        )
        assert not run(make_project, "DJA-004", source)

    def test_silent_when_the_model_has_no_owner(self, make_project) -> None:
        source = self.UNSCOPED.replace("Note", "Region")
        urls = ROUTED.replace("NoteViewSet", "RegionViewSet")
        found = run(
            make_project,
            "DJA-004",
            source.replace("class RegionViewSet", "class RegionViewSet"),
            urls=urls,
        )
        assert not found

    def test_silent_when_the_user_link_is_not_ownership(self, make_project) -> None:
        """An ``approved_by`` foreign key records a signature, not ownership."""
        source = self.UNSCOPED.replace("Note", "Tag")
        urls = ROUTED.replace("NoteViewSet", "TagViewSet")
        assert not run(make_project, "DJA-004", source, urls=urls)

    def test_severity_rises_when_the_endpoint_is_also_open(self, make_project) -> None:
        source = self.UNSCOPED.replace(
            "permission_classes = [IsAuthenticated]", "permission_classes = []"
        )
        found = run(make_project, "DJA-004", source)
        assert found[0].severity is Severity.CRITICAL

    def test_silent_on_a_detail_only_view(self, make_project) -> None:
        source = """
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from shop.models import Note

class NoteDetail(GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Note.objects.all()
"""
        urls = """
from django.urls import path
from shop.api import NoteDetail

urlpatterns = [path('notes/<int:pk>/', NoteDetail.as_view())]
"""
        assert not run(make_project, "DJA-004", source, urls=urls)


class TestObjectPermissions:
    """DJA-005 -- the hook that was written and never ran."""

    OWNED = """
from rest_framework import viewsets
from rest_framework.permissions import BasePermission
from shop.models import Note

class IsOwner(BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.owner_id == request.user.id

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
    permission_classes = [IsOwner]

    def get_object(self):
        return Note.objects.get(pk=self.kwargs['pk'])
"""

    def test_reports_an_override_that_skips_the_check(self, make_project) -> None:
        found = run(make_project, "DJA-005", self.OWNED)
        assert len(found) == 1
        assert "check_object_permissions" in found[0].message

    def test_silent_when_the_check_is_called(self, make_project) -> None:
        source = self.OWNED.replace(
            "        return Note.objects.get(pk=self.kwargs['pk'])",
            "        obj = Note.objects.get(pk=self.kwargs['pk'])\n"
            "        self.check_object_permissions(self.request, obj)\n"
            "        return obj",
        )
        assert not run(make_project, "DJA-005", source)

    def test_silent_when_the_fetch_is_scoped_to_the_request(self, make_project) -> None:
        """NetBox's DashboardView: the filter is the check."""
        source = self.OWNED.replace(
            "        return Note.objects.get(pk=self.kwargs['pk'])",
            "        return Note.objects.filter(owner=self.request.user).first()",
        )
        assert not run(make_project, "DJA-005", source)

    def test_silent_when_no_permission_defines_the_hook(self, make_project) -> None:
        source = self.OWNED.replace(
            "    def has_object_permission(self, request, view, obj):\n"
            "        return obj.owner_id == request.user.id",
            "    def has_permission(self, request, view):\n        return request.user.is_staff",
        )
        assert not run(make_project, "DJA-005", source)

    def test_silent_when_get_object_is_not_overridden(self, make_project) -> None:
        source = self.OWNED.replace(
            "    def get_object(self):\n        return Note.objects.get(pk=self.kwargs['pk'])\n",
            "",
        )
        assert not run(make_project, "DJA-005", source)
