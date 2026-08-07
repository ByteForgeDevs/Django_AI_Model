"""DJP-010 -- a client-chosen sort column with no index, on a table that grows.

The rule exists in the narrow form it does because the wide forms were
measured and rejected: `Meta.ordering` on any unindexed column is 44 findings
across the corpora and `ordering_fields` on any unindexed column is 21, and in
both cases most are lookup tables where sorting the whole thing is free.

So most of what is pinned here is what the rule stays quiet about. The
accumulation test is the only thing standing between 7 findings and 21, and if
it ever silently starts returning a column for every model the rule becomes
noise without failing anything else.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Finding, Severity
from djaudit.registry import all_rules
from tests.api.test_permissions import (
    DRF_DECORATORS,
    DRF_PERMISSION_SOURCE,
    DRF_ROUTERS,
    DRF_VIEWS,
    DRF_VIEWSETS,
    SETTINGS_MARKERS,
)

DRF_FILTERS = """
class BaseFilterBackend:
    pass

class OrderingFilter(BaseFilterBackend):
    pass

class SearchFilter(BaseFilterBackend):
    pass
"""

MODELS = """
from django.db import models
from django.utils.timezone import now

SETTINGS_FLAG = True


class Checkin(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    scanned = models.DateTimeField(default=now)
    note = models.CharField(max_length=50)
    code = models.CharField(max_length=50, db_index=True)
    slug = models.SlugField()
    owner = models.ForeignKey("Category", on_delete=models.CASCADE)


class Category(models.Model):
    name = models.CharField(max_length=50)
    position = models.IntegerField()


class Touched(models.Model):
    changed = models.DateTimeField(auto_now=True)
    name = models.CharField(max_length=50)


class Indexed(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=50, db_index=True)


class Contradictory(models.Model):
    changed = models.DateTimeField(auto_now=True, default=now)
    name = models.CharField(max_length=50)


class Unreadable(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=50, db_index=SETTINGS_FLAG)


class FullyIndexed(models.Model):
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    name = models.CharField(max_length=50, db_index=True)


class Linked(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    plain = models.ForeignKey("Category", on_delete=models.CASCADE, db_index=False,
                              related_name="plain")
    crowd = models.ManyToManyField("Category", related_name="crowd")
"""

ORDERED = """
from rest_framework import viewsets
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import IsAuthenticated
from shop.models import Checkin

class CheckinViewSet(viewsets.ModelViewSet):
    queryset = Checkin.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [OrderingFilter]
    ordering_fields = ['note']
"""

ROUTED = """
from rest_framework import routers
from shop.api import CheckinViewSet

router = routers.DefaultRouter()
router.register('checkins', CheckinViewSet)
"""

STRICT = SETTINGS_MARKERS + (
    "REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES':"
    " ['rest_framework.permissions.IsAuthenticated']}\n"
)


def project(
    make_project,
    api_source: str = ORDERED,
    models: str = MODELS,
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
            "rest_framework/filters.py": DRF_FILTERS,
            "shop/__init__.py": "",
            "shop/models.py": models,
            "shop/api.py": api_source,
            "shop/urls.py": urls,
            "shop/settings.py": settings,
            **extra,
        }
    )
    return built


def run(make_project, **kwargs) -> list[Finding]:
    ctx = project(make_project, **kwargs)
    rule = next(r for r in all_rules() if r.meta.id == "DJP-010")()
    return list(rule.check(ctx))


def viewset(body: str, model: str = "Checkin") -> str:
    return f"""
from rest_framework import viewsets
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.permissions import IsAuthenticated
from shop.models import {model}

class CheckinViewSet(viewsets.ModelViewSet):
    queryset = {model}.objects.all()
    permission_classes = [IsAuthenticated]
{body}
"""


class TestTheSortItReports:
    def test_an_unindexed_column_on_a_growing_table(self, make_project) -> None:
        found = run(make_project)
        assert len(found) == 1
        assert "`note`" in found[0].message
        assert "Checkin" in found[0].message

    def test_it_points_at_the_declaration(self, make_project) -> None:
        found = run(make_project)
        assert found[0].location.file == "shop/api.py"
        assert found[0].location.line == 10

    def test_the_evidence_names_the_stamp_and_the_real_indexes(self, make_project) -> None:
        """A reviewer's first question is why this table is considered to grow."""
        content = run(make_project)[0].evidence[0].content
        assert "created" in content
        assert "code" in content and "slug" in content

    def test_several_unindexed_columns_are_one_finding(self, make_project) -> None:
        """One `ordering_fields` line is one edit, not one edit per name."""
        found = run(
            make_project,
            api_source=viewset(
                "    filter_backends = [OrderingFilter]\n    ordering_fields = ['note', 'scanned']"
            ),
        )
        assert len(found) == 1
        assert "`note`" in found[0].message and "`scanned`" in found[0].message

    def test_a_descending_name_is_the_same_column(self, make_project) -> None:
        found = run(
            make_project,
            api_source=viewset(
                "    filter_backends = [OrderingFilter]\n    ordering_fields = ['-note']"
            ),
        )
        assert len(found) == 1
        assert "`note`" in found[0].message

    def test_all_fields_reports_every_unindexed_column(self, make_project) -> None:
        found = run(
            make_project,
            api_source=viewset(
                "    filter_backends = [OrderingFilter]\n    ordering_fields = '__all__'"
            ),
        )
        assert len(found) == 1
        assert "__all__" in found[0].message
        assert "note" in found[0].message and "scanned" in found[0].message

    def test_all_fields_lists_a_bare_foreign_key_but_not_a_many_to_many(self, make_project) -> None:
        """DRF expands `__all__` to `_meta.fields`, which has columns and no M2M."""
        found = run(
            make_project,
            api_source=viewset(
                "    filter_backends = [OrderingFilter]\n    ordering_fields = '__all__'",
                model="Linked",
            ),
        )
        assert len(found) == 1
        assert "plain" in found[0].message
        assert "crowd" not in found[0].message

    def test_a_defaulted_timestamp_counts_as_growth(self, make_project) -> None:
        """`default=now` is `auto_now_add` written by hand, and pretix uses it."""
        found = run(
            make_project,
            models=MODELS.replace("created = models.DateTimeField(auto_now_add=True)\n    ", ""),
        )
        assert len(found) == 1

    def test_a_backend_installed_project_wide_counts(self, make_project) -> None:
        found = run(
            make_project,
            api_source=viewset("    ordering_fields = ['note']"),
            settings=SETTINGS_MARKERS
            + (
                "REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES':"
                " ['rest_framework.permissions.IsAuthenticated'],"
                " 'DEFAULT_FILTER_BACKENDS':"
                " ['rest_framework.filters.OrderingFilter']}\n"
            ),
        )
        assert len(found) == 1

    def test_a_tuple_is_read_like_a_list(self, make_project) -> None:
        """pretix writes every one of these as a tuple, trailing comma included."""
        found = run(
            make_project,
            api_source=viewset(
                "    filter_backends = [OrderingFilter]\n    ordering_fields = ('note', 'scanned',)"
            ),
        )
        assert len(found) == 1
        assert "`note`" in found[0].message

    def test_a_foreign_key_that_opted_out_of_its_index(self, make_project) -> None:
        """`order_by('plain')` sorts on `plain_id`, a column here like any other."""
        found = run(
            make_project,
            api_source=viewset(
                "    filter_backends = [OrderingFilter]\n    ordering_fields = ['plain']",
                model="Linked",
            ),
        )
        assert len(found) == 1
        assert "`plain`" in found[0].message

    def test_a_subclassed_backend_still_counts(self, make_project) -> None:
        """pretix routes everything through `RichOrderingFilter`, not DRF's."""
        found = run(
            make_project,
            api_source="""
from rest_framework import viewsets
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import IsAuthenticated
from shop.models import Checkin

class RichOrderingFilter(OrderingFilter):
    pass

class CheckinViewSet(viewsets.ModelViewSet):
    queryset = Checkin.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [RichOrderingFilter]
    ordering_fields = ['note']
""",
        )
        assert len(found) == 1


class TestTheSortItDeclines:
    """Twenty-one findings became seven here. Each of these is why."""

    def test_a_table_that_does_not_grow(self, make_project) -> None:
        """`Category` has no timestamp: it is configuration, and it is small."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n"
                    "    ordering_fields = ['name', 'position']",
                    model="Category",
                ),
                urls=ROUTED,
            )
            == []
        )

    def test_auto_now_is_modification_not_arrival(self, make_project) -> None:
        """A row that stamps when it *changed* says nothing about how many exist."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['name']",
                    model="Touched",
                ),
            )
            == []
        )

    def test_an_indexed_column(self, make_project) -> None:
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['code']"
                ),
            )
            == []
        )

    def test_a_slug_is_indexed_without_saying_so(self, make_project) -> None:
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['slug']"
                ),
            )
            == []
        )

    def test_a_foreign_key_is_indexed_without_saying_so(self, make_project) -> None:
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['owner']"
                ),
            )
            == []
        )

    def test_the_primary_key(self, make_project) -> None:
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['pk', 'id']"
                ),
            )
            == []
        )

    def test_a_relation_path(self, make_project) -> None:
        """`owner__name` is a column on another table, whose index we did not check."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['owner__name']"
                ),
            )
            == []
        )

    def test_a_name_that_is_not_a_field(self, make_project) -> None:
        """An annotation alias has no column and therefore no index to miss."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['total_paid']"
                ),
            )
            == []
        )

    def test_no_ordering_backend(self, make_project) -> None:
        """`ordering_fields` with nothing to read it is a comment."""
        assert run(make_project, api_source=viewset("    ordering_fields = ['note']")) == []

    def test_a_backend_that_is_not_an_ordering_one(self, make_project) -> None:
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [SearchFilter]\n    ordering_fields = ['note']"
                ),
            )
            == []
        )

    def test_no_ordering_fields_at_all(self, make_project) -> None:
        assert run(make_project, api_source=viewset("    filter_backends = [OrderingFilter]")) == []

    def test_a_list_it_cannot_read(self, make_project) -> None:
        """A computed list costs recall, never precision."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n"
                    "    ordering_fields = SORTABLE + ['note']"
                ),
            )
            == []
        )

    def test_a_growing_table_whose_columns_are_all_indexed(self, make_project) -> None:
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['name']",
                    model="Indexed",
                ),
            )
            == []
        )

    def test_auto_now_wins_over_a_default_written_beside_it(self, make_project) -> None:
        """Django ignores the default when `auto_now` is set, so must we."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['name']",
                    model="Contradictory",
                ),
            )
            == []
        )

    def test_a_flag_it_could_not_evaluate(self, make_project) -> None:
        """`db_index=SOME_SETTING` means the index is unknown, not absent."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['name']",
                    model="Unreadable",
                ),
            )
            == []
        )

    def test_a_many_to_many_is_not_a_column_here(self, make_project) -> None:
        """Sorting through a join is a different cost, and not this one."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['crowd']",
                    model="Linked",
                ),
            )
            == []
        )

    def test_all_fields_in_a_list_is_an_ordinary_name(self, make_project) -> None:
        """DRF compares `ordering_fields == '__all__'`, so in a list it is a column."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = ['__all__']"
                ),
            )
            == []
        )

    def test_all_fields_on_a_table_that_indexed_everything(self, make_project) -> None:
        """The wildcard is only bad for the columns that lack an index."""
        assert (
            run(
                make_project,
                api_source=viewset(
                    "    filter_backends = [OrderingFilter]\n    ordering_fields = '__all__'",
                    model="FullyIndexed",
                ),
            )
            == []
        )

    def test_an_unrouted_view(self, make_project) -> None:
        """A base class nobody routes serves no requests."""
        assert run(make_project, urls="urlpatterns = []\n") == []


class TestHowItSpeaks:
    def test_confidence_and_severity(self, make_project) -> None:
        found = run(make_project)[0]
        assert found.confidence is Confidence.FIRM
        assert found.severity is Severity.MEDIUM

    def test_it_names_the_family(self, make_project) -> None:
        assert run(make_project)[0].rule_id == "DJP-010"


class TestWithNothingToReasonFrom:
    def test_no_models(self, make_project) -> None:
        assert run(make_project, models="") == []

    def test_no_api_at_all(self, make_project) -> None:
        assert run(make_project, api_source="", urls="urlpatterns = []\n") == []
