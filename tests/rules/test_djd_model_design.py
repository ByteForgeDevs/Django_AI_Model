"""DJD -- the shape of the data, and the rows that quietly go missing.

All three benchmarks are silent for DJD-001, and the silence is earned rather
than accidental: the word list matches nineteen retained-record models across
NetBox and pretix, four of those carry a foreign key to the user, and every one
of the four is `SET_NULL` or `PROTECT`. Two mature projects independently made
the choice this rule asks for. What is pinned here is the discrimination that
produces that agreement -- the policy check, and the name reading that has to
tell `Recorder` from `Order`.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Finding, Severity
from djaudit.registry import all_rules
from djaudit.rules.datamodel import camel_words, name_tokens
from tests.api.test_permissions import (
    DRF_DECORATORS,
    DRF_PERMISSION_SOURCE,
    DRF_ROUTERS,
    DRF_VIEWS,
    DRF_VIEWSETS,
)
from tests.rules.test_dja_availability import DRF_PAGINATION

SETTINGS = """
SECRET_KEY = "x"
DEBUG = False
ALLOWED_HOSTS = ["example.com"]
INSTALLED_APPS = ["shop"]
ROOT_URLCONF = "shop.urls"
"""

INVOICE = """
from django.conf import settings
from django.db import models

class Invoice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
"""


def project(make_project, models_source: str = INVOICE, **extra: str) -> ProjectContext:
    built: ProjectContext = make_project(
        {
            "manage.py": "",
            "shop/__init__.py": "",
            "shop/models.py": models_source,
            "shop/urls.py": "urlpatterns = []",
            "shop/settings.py": SETTINGS,
            **extra,
        }
    )
    return built


def run(make_project, rule_id: str, **kwargs) -> list[Finding]:
    ctx = project(make_project, **kwargs)
    rule = next(r for r in all_rules() if r.meta.id == rule_id)()
    return list(rule.check(ctx))


def model(name: str, policy: str = "models.CASCADE", extra: str = "") -> str:
    return f"""
from django.conf import settings
from django.db import models

class {name}(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete={policy}{extra})
"""


class TestNameReading:
    """A rule that judges by name has to read names properly."""

    def test_splits_camel_case(self) -> None:
        assert camel_words("OrderPosition") == ["order", "position"]

    def test_splits_underscores(self) -> None:
        assert camel_words("audit_log") == ["audit", "log"]

    def test_keeps_an_acronym_whole(self) -> None:
        assert "api" in camel_words("APIToken")

    def test_a_word_containing_another_is_not_that_word(self) -> None:
        # The reason this rule splits instead of using `in`: substring matching
        # reports every Recorder as an order and every Historic as a history.
        assert "order" not in name_tokens("Recorder")
        assert "history" not in name_tokens("HistoricPassword")

    def test_joins_adjacent_words(self) -> None:
        assert "changelog" in name_tokens("ChangeLog")
        assert "logentry" in name_tokens("LogEntry")

    def test_joins_pairs_inside_a_longer_name(self) -> None:
        assert "auditlog" in name_tokens("StaffSessionAuditLog")

    def test_does_not_join_across_a_gap(self) -> None:
        assert "orderrefund" not in name_tokens("OrderExportRefund")

    def test_a_single_word_produces_no_pair(self) -> None:
        assert name_tokens("Recorder") == frozenset({"recorder"})


class TestCascadingRetainedRecord:
    """DJD-001 -- deleting a user deletes the evidence."""

    def test_reports_an_invoice_that_cascades(self, make_project) -> None:
        found = run(make_project, "DJD-001")
        assert len(found) == 1
        assert found[0].severity is Severity.MEDIUM
        assert found[0].confidence is Confidence.FIRM
        assert "Invoice" in found[0].message

    def test_names_the_word_that_made_it_a_record(self, make_project) -> None:
        found = run(make_project, "DJD-001")
        assert any("invoice" in e.content for e in found[0].evidence)

    def test_points_at_the_field(self, make_project) -> None:
        found = run(make_project, "DJD-001")
        assert found[0].location.file == "shop/models.py"
        assert found[0].location.line == 5

    def test_silent_on_protect(self, make_project) -> None:
        assert run(make_project, "DJD-001", models_source=model("Invoice", "models.PROTECT")) == []

    def test_silent_on_set_null(self, make_project) -> None:
        # NetBox's ObjectChange and pretix's LogEntry are both this shape.
        found = run(
            make_project,
            "DJD-001",
            models_source=model("AuditLog", "models.SET_NULL", ", null=True"),
        )
        assert found == []

    def test_silent_on_a_bookmark(self, make_project) -> None:
        # Cascading a user's own preferences is correct, and is what every
        # CASCADE-to-user on both benchmarks actually is.
        assert run(make_project, "DJD-001", models_source=model("Bookmark")) == []

    def test_silent_on_a_notification_subscription(self, make_project) -> None:
        # NetBox's Subscription is a change-notification subscription, not a
        # billing one, which is why `subscription` is not in the word list.
        assert run(make_project, "DJD-001", models_source=model("Subscription")) == []

    def test_reports_a_compound_name(self, make_project) -> None:
        assert len(run(make_project, "DJD-001", models_source=model("ChangeLog"))) == 1

    def test_reports_a_one_to_one(self, make_project) -> None:
        source = INVOICE.replace("ForeignKey", "OneToOneField")
        assert len(run(make_project, "DJD-001", models_source=source)) == 1

    def test_silent_on_a_bare_cascade_reference(self, make_project) -> None:
        # `from django.db.models import CASCADE` is the same policy written
        # differently, and has to be read the same way.
        source = INVOICE.replace("models.CASCADE", "CASCADE")
        assert len(run(make_project, "DJD-001", models_source=source)) == 1

    def test_silent_when_the_target_is_not_the_user(self, make_project) -> None:
        source = INVOICE.replace("settings.AUTH_USER_MODEL", '"shop.Tenant"')
        assert run(make_project, "DJD-001", models_source=source) == []

    def test_silent_when_the_policy_is_unreadable(self, make_project) -> None:
        # A callable built at runtime is not evidence of a cascade.
        source = INVOICE.replace("models.CASCADE", "pick_policy()")
        assert run(make_project, "DJD-001", models_source=source) == []

    def test_reports_each_model_once(self, make_project) -> None:
        source = (
            INVOICE
            + """
class Payment(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
"""
        )
        assert len(run(make_project, "DJD-001", models_source=source)) == 2

    def test_reports_the_class_that_wrote_the_line(self, make_project) -> None:
        # An abstract base with the field is one finding, not one per subclass.
        source = """
from django.conf import settings
from django.db import models

class OwnedInvoice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        abstract = True

class ProformaInvoice(OwnedInvoice):
    pass

class FinalInvoice(OwnedInvoice):
    pass
"""
        found = run(make_project, "DJD-001", models_source=source)
        assert [f.location.line for f in found] == [5]


def field(decl: str, meta: str = "") -> str:
    return f"""
from django.db import models

class Note(models.Model):
    {decl}
{meta}
"""


class TestNullableStringField:
    """DJD-002 -- two spellings of empty in one column."""

    def test_reports_a_nullable_charfield(self, make_project) -> None:
        found = run(
            make_project,
            "DJD-002",
            models_source=field("title = models.CharField(max_length=50, null=True)"),
        )
        assert len(found) == 1
        assert found[0].severity is Severity.LOW
        assert "title" in found[0].message

    def test_silent_when_blank_is_declared(self, make_project) -> None:
        # 166 of the 191 nullable string fields on the benchmarks are this
        # shape; reporting them would bury everything else the tool says.
        found = run(
            make_project,
            "DJD-002",
            models_source=field("title = models.CharField(max_length=50, null=True, blank=True)"),
        )
        assert found == []

    def test_silent_when_not_nullable(self, make_project) -> None:
        found = run(
            make_project,
            "DJD-002",
            models_source=field('title = models.CharField(max_length=50, default="")'),
        )
        assert found == []

    def test_silent_on_a_non_string_field(self, make_project) -> None:
        found = run(
            make_project,
            "DJD-002",
            models_source=field("count = models.IntegerField(null=True)"),
        )
        assert found == []

    def test_silent_when_unique(self, make_project) -> None:
        found = run(
            make_project,
            "DJD-002",
            models_source=field("slug = models.SlugField(null=True, unique=True)"),
        )
        assert found == []

    def test_silent_when_a_composite_unique_spans_it(self, make_project) -> None:
        # pretix's Customer is unique on (organizer, email) and must hold many
        # email-less customers per organizer, which is what null buys. Reading
        # only field-level unique reported it.
        source = """
from django.db import models

class Note(models.Model):
    owner = models.CharField(max_length=50)
    email = models.EmailField(null=True)

    class Meta:
        unique_together = (("owner", "email"),)
"""
        assert run(make_project, "DJD-002", models_source=source) == []

    def test_silent_when_a_unique_constraint_spans_it(self, make_project) -> None:
        source = """
from django.db import models

class Note(models.Model):
    owner = models.CharField(max_length=50)
    email = models.EmailField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "email"], name="u"),
        ]
"""
        assert run(make_project, "DJD-002", models_source=source) == []

    def test_reports_a_textfield(self, make_project) -> None:
        found = run(
            make_project, "DJD-002", models_source=field("body = models.TextField(null=True)")
        )
        assert len(found) == 1

    def test_groups_a_model_into_one_finding(self, make_project) -> None:
        # pretix's Invoice declares fifteen of these and they are one migration
        # to fix, not fifteen findings to read.
        source = """
from django.db import models

class Note(models.Model):
    a = models.CharField(max_length=5, null=True)
    b = models.CharField(max_length=5, null=True)
    c = models.TextField(null=True)
"""
        found = run(make_project, "DJD-002", models_source=source)
        assert len(found) == 1
        assert "3 nullable string columns" in found[0].message
        assert all(n in found[0].message for n in ("a", "b", "c"))

    def test_counts_one_column_in_the_singular(self, make_project) -> None:
        found = run(
            make_project, "DJD-002", models_source=field("body = models.TextField(null=True)")
        )
        assert "1 nullable string column with" in found[0].message

    def test_evidence_lists_every_line(self, make_project) -> None:
        source = """
from django.db import models

class Note(models.Model):
    a = models.CharField(max_length=5, null=True)
    b = models.TextField(null=True)
"""
        found = run(make_project, "DJD-002", models_source=source)
        content = found[0].evidence[0].content
        assert "line 4" in content and "line 5" in content

    def test_reports_a_file_field(self, make_project) -> None:
        found = run(
            make_project, "DJD-002", models_source=field("doc = models.FileField(null=True)")
        )
        assert len(found) == 1

    def test_reports_the_abstract_base_not_each_child(self, make_project) -> None:
        source = """
from django.db import models

class Owned(models.Model):
    label = models.CharField(max_length=5, null=True)

    class Meta:
        abstract = True

class Note(Owned):
    pass

class Memo(Owned):
    pass
"""
        found = run(make_project, "DJD-002", models_source=source)
        assert [f.location.line for f in found] == [4]


DRF_STUBS = {
    "rest_framework/__init__.py": "",
    "rest_framework/views.py": DRF_VIEWS,
    "rest_framework/generics.py": DRF_VIEWS,
    "rest_framework/viewsets.py": DRF_VIEWSETS,
    "rest_framework/permissions.py": DRF_PERMISSION_SOURCE,
    "rest_framework/decorators.py": DRF_DECORATORS,
    "rest_framework/routers.py": DRF_ROUTERS,
    "rest_framework/pagination.py": DRF_PAGINATION,
}

PAGINATED = """
SECRET_KEY = "x"
DEBUG = False
ALLOWED_HOSTS = ["example.com"]
INSTALLED_APPS = ["shop"]
ROOT_URLCONF = "shop.urls"
REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}
"""

ROUTED_NOTES = """
from rest_framework.routers import DefaultRouter
from shop import api

router = DefaultRouter()
router.register("notes", api.NoteViewSet)
urlpatterns = router.urls
"""

PLAIN_NOTE = """
from django.db import models

class Note(models.Model):
    body = models.TextField()
"""


def api_project(
    make_project,
    view_source: str,
    models_source: str = PLAIN_NOTE,
    settings: str = PAGINATED,
) -> ProjectContext:
    built: ProjectContext = make_project(
        {
            "manage.py": "",
            **DRF_STUBS,
            "shop/__init__.py": "",
            "shop/models.py": models_source,
            "shop/api.py": view_source,
            "shop/urls.py": ROUTED_NOTES,
            "shop/settings.py": settings,
        }
    )
    return built


def paginated(make_project, view_source: str, **kwargs) -> list[Finding]:
    ctx = api_project(make_project, view_source, **kwargs)
    rule = next(r for r in all_rules() if r.meta.id == "DJD-003")()
    return list(rule.check(ctx))


BARE = """
from rest_framework import viewsets
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
"""


class TestUnorderedPaginatedModel:
    """DJD-003 -- page two contains what page one already showed."""

    def test_reports_an_unordered_paginated_list(self, make_project) -> None:
        found = paginated(make_project, BARE)
        assert len(found) == 1
        assert found[0].severity is Severity.MEDIUM
        assert "Note" in found[0].message

    def test_the_rule_can_be_constructed(self) -> None:
        # It could not. ApiRule.inspect is abstract and this rule overrode
        # check instead, so instantiation raised, the engine filed it under
        # rule_errors, and three benchmarks reported a clean zero that meant
        # the rule had never run.
        rule = next(r for r in all_rules() if r.meta.id == "DJD-003")()
        assert rule.meta.id == "DJD-003"

    def test_silent_when_the_model_declares_ordering(self, make_project) -> None:
        models_source = """
from django.db import models

class Note(models.Model):
    body = models.TextField()

    class Meta:
        ordering = ["pk"]
"""
        assert paginated(make_project, BARE, models_source=models_source) == []

    def test_silent_when_the_queryset_orders(self, make_project) -> None:
        source = """
from rest_framework import viewsets
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.order_by("pk")
"""
        assert paginated(make_project, source) == []

    def test_silent_when_get_queryset_orders(self, make_project) -> None:
        # Every pretix candidate is this shape: queryset = Model.objects.none()
        # as a placeholder, with the real ordering inside get_queryset.
        source = """
from rest_framework import viewsets
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.none()

    def get_queryset(self):
        return Note.objects.order_by("pk")
"""
        assert paginated(make_project, source) == []

    def test_silent_when_the_view_declares_ordering(self, make_project) -> None:
        source = """
from rest_framework import viewsets
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
    ordering = ["pk"]
"""
        assert paginated(make_project, source) == []

    def test_silent_when_an_ancestor_orders(self, make_project) -> None:
        source = """
from rest_framework import viewsets
from shop.models import Note

class OrderedBase(viewsets.ModelViewSet):
    def get_queryset(self):
        return super().get_queryset().order_by("pk")

class NoteViewSet(OrderedBase):
    queryset = Note.objects.all()
"""
        assert paginated(make_project, source) == []

    def test_silent_when_a_custom_manager_could_order(self, make_project) -> None:
        # NetBox's Region has no Meta.ordering and is paginated, and is stable
        # because objects = TreeManager() orders by (tree_id, lft).
        models_source = """
from django.db import models
from mptt.managers import TreeManager

class Note(models.Model):
    body = models.TextField()
    objects = TreeManager()
"""
        assert paginated(make_project, BARE, models_source=models_source) == []

    def test_silent_when_pagination_is_switched_off(self, make_project) -> None:
        source = """
from rest_framework import viewsets
from shop.models import Note

class NoteViewSet(viewsets.ModelViewSet):
    queryset = Note.objects.all()
    pagination_class = None
"""
        assert paginated(make_project, source) == []

    def test_silent_when_the_project_does_not_paginate(self, make_project) -> None:
        settings = PAGINATED.replace(
            '"DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",', ""
        ).replace('"PAGE_SIZE": 20,', "")
        assert paginated(make_project, BARE, settings=settings) == []

    def test_evidence_names_both_halves(self, make_project) -> None:
        found = paginated(make_project, BARE)
        blob = " ".join(e.content for e in found[0].evidence)
        assert "Meta.ordering" in blob and "order_by" in blob
