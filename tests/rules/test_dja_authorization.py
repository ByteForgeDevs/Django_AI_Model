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
