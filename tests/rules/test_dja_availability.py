"""DJA -- availability rules, and the configuration that only looks correct.

The benchmarks are silent for all three of these, and two of the silences are
the interesting part: NetBox names a pagination class and sets no PAGE_SIZE,
which is the exact shape DJA-013 reports, and is right not to be reported
because NetBoxPagination supplies its own limit from runtime configuration.
Getting that wrong would have put a false positive on the largest project in
the set, so the case is pinned here as well as measured there.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Finding, Severity
from djaudit.registry import all_rules
from tests.api.test_permissions import (
    DRF_DECORATORS,
    DRF_PERMISSION_SOURCE,
    DRF_ROUTERS,
    DRF_VIEWS,
    DRF_VIEWSETS,
    SETTINGS_MARKERS,
)

DRF_PAGINATION = """
class BasePagination:
    pass

class PageNumberPagination(BasePagination):
    page_size = None

class LimitOffsetPagination(BasePagination):
    default_limit = None
"""

MODELS = """
from django.db import models
from django.conf import settings

class Note(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.TextField()
"""

VIEWSET = """
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
    permission_classes = [IsAuthenticated]
"""

ROUTED = """
from rest_framework import routers
from shop.api import NoteViewSet

router = routers.DefaultRouter()
router.register('notes', NoteViewSet)
"""

STRICT = SETTINGS_MARKERS + (
    "REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES':"
    " ['rest_framework.permissions.IsAuthenticated']}\n"
)


def settings_with(**entries: str) -> str:
    body = ", ".join(f"{key!r}: {value}" for key, value in entries.items())
    return SETTINGS_MARKERS + (
        "REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES':"
        " ['rest_framework.permissions.IsAuthenticated'], " + body + "}\n"
    )


def project(
    make_project,
    api_source: str = VIEWSET,
    settings: str = STRICT,
    urls: str = ROUTED,
    **extra: str,
) -> ProjectContext:
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
            "rest_framework/pagination.py": DRF_PAGINATION,
            "shop/__init__.py": "",
            "shop/models.py": MODELS,
            "shop/api.py": api_source,
            "shop/urls.py": urls,
            "shop/settings.py": settings,
            **extra,
        }
    )
    return built


def run(make_project, rule_id: str, **kwargs) -> list[Finding]:
    ctx = project(make_project, **kwargs)
    rule = next(r for r in all_rules() if r.meta.id == rule_id)()
    return list(rule.check(ctx))


class TestUnboundedListEndpoint:
    """DJA-013 -- the list that hands back the table."""

    def test_reports_a_missing_default(self, make_project) -> None:
        found = run(make_project, "DJA-013")
        assert len(found) == 1
        assert found[0].location.file == "shop/settings.py"
        assert "1 list endpoint(s)" in found[0].message

    def test_reports_a_pagination_class_with_no_page_size(self, make_project) -> None:
        """The sharp case: configured pagination that paginates nothing.

        `PageNumberPagination.page_size` *is* `api_settings.PAGE_SIZE`, and
        `paginate_queryset` returns None when it is falsy. The settings file
        reads as solved.
        """
        found = run(
            make_project,
            "DJA-013",
            settings=settings_with(
                DEFAULT_PAGINATION_CLASS="'rest_framework.pagination.PageNumberPagination'"
            ),
        )
        assert len(found) == 1
        assert "sets no PAGE_SIZE" in found[0].message

    def test_silent_when_both_are_set(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-013",
            settings=settings_with(
                DEFAULT_PAGINATION_CLASS="'rest_framework.pagination.PageNumberPagination'",
                PAGE_SIZE="50",
            ),
        )
        assert found == []

    def test_silent_when_the_class_supplies_its_own_size(self, make_project) -> None:
        """NetBox's shape: a subclass reading its limit from runtime config."""
        found = run(
            make_project,
            "DJA-013",
            settings=settings_with(DEFAULT_PAGINATION_CLASS="'shop.paging.SitePagination'"),
            **{
                "shop/paging.py": (
                    "from rest_framework.pagination import LimitOffsetPagination\n\n"
                    "class SitePagination(LimitOffsetPagination):\n"
                    "    def __init__(self):\n"
                    "        self.default_limit = 100\n"
                )
            },
        )
        assert found == []

    def test_silent_when_the_class_overrides_the_method(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-013",
            settings=settings_with(DEFAULT_PAGINATION_CLASS="'shop.paging.SitePagination'"),
            **{
                "shop/paging.py": (
                    "from rest_framework.pagination import LimitOffsetPagination\n\n"
                    "class SitePagination(LimitOffsetPagination):\n"
                    "    def get_limit(self, request):\n"
                    "        return 100\n"
                )
            },
        )
        assert found == []

    def test_a_subclass_inheriting_drfs_line_does_not_vouch(self, make_project) -> None:
        """`page_size = api_settings.PAGE_SIZE` is a redirect, not an answer."""
        found = run(
            make_project,
            "DJA-013",
            settings=settings_with(DEFAULT_PAGINATION_CLASS="'shop.paging.SitePagination'"),
            **{
                "shop/paging.py": (
                    "from rest_framework.settings import api_settings\n"
                    "from rest_framework.pagination import PageNumberPagination\n\n"
                    "class SitePagination(PageNumberPagination):\n"
                    "    page_size = api_settings.PAGE_SIZE\n"
                )
            },
        )
        assert len(found) == 1
        assert "sets no PAGE_SIZE" in found[0].message

    def test_reports_a_view_that_switched_it_off(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-013",
            api_source=VIEWSET + "    pagination_class = None\n",
            settings=settings_with(
                DEFAULT_PAGINATION_CLASS="'rest_framework.pagination.PageNumberPagination'",
                PAGE_SIZE="50",
            ),
        )
        assert len(found) == 1
        assert found[0].location.file == "shop/api.py"
        assert "pagination_class = None" in found[0].message

    def test_a_view_that_opted_out_is_not_counted_against_settings(self, make_project) -> None:
        """Two findings for one endpoint would blame settings for a view's choice."""
        found = run(
            make_project,
            "DJA-013",
            api_source=VIEWSET + "    pagination_class = None\n",
        )
        settings_finding = [f for f in found if f.location.file == "shop/settings.py"]
        assert len(settings_finding) == 1
        assert "No routed list endpoint" in settings_finding[0].message
        assert settings_finding[0].severity is Severity.LOW

    def test_silent_on_a_project_without_drf(self, make_project) -> None:
        """healthchecks has no DRF, and DRF's defaults are not facts about it."""
        ctx: ProjectContext = make_project(
            {
                "manage.py": "",
                "shop/__init__.py": "",
                "shop/models.py": MODELS,
                "shop/urls.py": "urlpatterns = []\n",
                "shop/settings.py": STRICT,
            }
        )
        rule = next(r for r in all_rules() if r.meta.id == "DJA-013")()
        assert list(rule.check(ctx)) == []

    def test_severity_drops_when_nothing_lists(self, make_project) -> None:
        found = run(make_project, "DJA-013", urls="urlpatterns = []\n")
        assert len(found) == 1
        assert found[0].severity is Severity.LOW

    def test_names_the_endpoints_that_rely_on_it(self, make_project) -> None:
        found = run(make_project, "DJA-013")
        blob = " ".join(e.content for e in found[0].evidence)
        assert "shop.api.NoteViewSet lists Note" in blob
