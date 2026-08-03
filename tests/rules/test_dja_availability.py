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


DJANGO_FILTERS = """
class FilterSet:
    pass
"""

ACCOUNT = """
from django.db import models

class Account(models.Model):
    email = models.EmailField()
    password = models.CharField(max_length=128)
    status = models.CharField(max_length=20)
"""

FILTERED = """
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from shop.models import Account

class AccountViewSet(viewsets.ModelViewSet):
    queryset = Account.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
"""

ACCOUNT_ROUTED = """
from rest_framework import routers
from shop.api import AccountViewSet

router = routers.DefaultRouter()
router.register('accounts', AccountViewSet)
urlpatterns = router.urls
"""


def filtered(make_project, view_body: str, **extra: str) -> list[Finding]:
    return run(
        make_project,
        "DJA-014",
        api_source=FILTERED + view_body,
        urls=ACCOUNT_ROUTED,
        **{
            "shop/models.py": ACCOUNT,
            "django_filters/__init__.py": DJANGO_FILTERS,
            "django_filters/rest_framework.py": (
                "from django_filters import FilterSet\n\nclass DjangoFilterBackend:\n    pass\n"
            ),
            **extra,
        },
    )


class TestArbitraryFilterLookups:
    """DJA-014 -- the query parameter nobody reviewed."""

    def test_reports_all_fields_inline(self, make_project) -> None:
        found = filtered(make_project, "    filterset_fields = '__all__'\n")
        assert len(found) == 1
        assert found[0].severity is Severity.HIGH
        assert "password" in found[0].message

    def test_reports_the_legacy_attribute_name(self, make_project) -> None:
        """`filter_fields` is django-filter's pre-2.0 spelling and still works."""
        found = filtered(make_project, "    filter_fields = '__all__'\n")
        assert len(found) == 1

    def test_reports_a_filterset_class(self, make_project) -> None:
        found = filtered(
            make_project,
            "    filterset_class = AccountFilter\n",
            **{
                "shop/filters.py": (
                    "from django_filters import FilterSet\n"
                    "from shop.models import Account\n\n"
                    "class AccountFilter(FilterSet):\n"
                    "    class Meta:\n"
                    "        model = Account\n"
                    "        fields = '__all__'\n"
                ),
                "shop/api.py": FILTERED.replace(
                    "from shop.models import Account",
                    "from shop.models import Account\nfrom shop.filters import AccountFilter",
                )
                + "    filterset_class = AccountFilter\n",
            },
        )
        assert len(found) == 1
        assert found[0].location.file == "shop/filters.py"
        assert found[0].severity is Severity.HIGH

    def test_resolves_the_filterset_through_the_view_module_imports(self, make_project) -> None:
        """`filterset_class = filters.AccountFilter` is not a dotted path.

        Looking it up verbatim finds nothing, which reads exactly like a
        project with no over-broad filtersets. NetBox declares 129 of these.
        """
        found = filtered(
            make_project,
            "from shop import filters\n\n"
            "class Wide(viewsets.ModelViewSet):\n"
            "    queryset = Account.objects.all()\n"
            "    permission_classes = [IsAuthenticated]\n"
            "    filter_backends = [DjangoFilterBackend]\n"
            "    filterset_class = filters.AccountFilter\n",
            **{
                "shop/filters.py": (
                    "from django_filters import FilterSet\n"
                    "from shop.models import Account\n\n"
                    "class AccountFilter(FilterSet):\n"
                    "    class Meta:\n"
                    "        model = Account\n"
                    "        fields = '__all__'\n"
                ),
                "shop/urls.py": (
                    "from rest_framework import routers\n"
                    "from shop.api import AccountViewSet, Wide\n\n"
                    "router = routers.DefaultRouter()\n"
                    "router.register('accounts', AccountViewSet)\n"
                    "router.register('wide', Wide)\n"
                    "urlpatterns = router.urls\n"
                ),
            },
        )
        assert len(found) == 1
        assert found[0].location.file == "shop/filters.py"

    def test_silent_on_a_filterset_with_no_meta(self, make_project) -> None:
        """A FilterSet that only declares filters exposes exactly those."""
        found = filtered(
            make_project,
            "    filterset_class = AccountFilter\n",
            **{
                "shop/filters.py": (
                    "import django_filters\n"
                    "from django_filters import FilterSet\n\n"
                    "class AccountFilter(FilterSet):\n"
                    "    status = django_filters.CharFilter()\n"
                ),
                "shop/api.py": FILTERED.replace(
                    "from shop.models import Account",
                    "from shop.models import Account\nfrom shop.filters import AccountFilter",
                )
                + "    filterset_class = AccountFilter\n",
            },
        )
        assert found == []

    def test_exclude_is_not_accused_of_exposing_what_it_removed(self, make_project) -> None:
        found = filtered(
            make_project,
            "    filterset_class = AccountFilter\n",
            **{
                "shop/filters.py": (
                    "from django_filters import FilterSet\n"
                    "from shop.models import Account\n\n"
                    "class AccountFilter(FilterSet):\n"
                    "    class Meta:\n"
                    "        model = Account\n"
                    "        exclude = ['password']\n"
                ),
                "shop/api.py": FILTERED.replace(
                    "from shop.models import Account",
                    "from shop.models import Account\nfrom shop.filters import AccountFilter",
                )
                + "    filterset_class = AccountFilter\n",
            },
        )
        assert len(found) == 1
        assert "including password" not in found[0].message
        assert "holds back password" in found[0].message
        assert found[0].severity is Severity.MEDIUM

    def test_finds_a_filterset_nested_in_a_with_block(self, make_project) -> None:
        """pretix declares 23 of its filtersets inside `with scopes_disabled():`."""
        found = filtered(
            make_project,
            "    filterset_class = AccountFilter\n",
            **{
                "shop/filters.py": (
                    "from django_filters import FilterSet\n"
                    "from django_scopes import scopes_disabled\n"
                    "from shop.models import Account\n\n"
                    "with scopes_disabled():\n"
                    "    class AccountFilter(FilterSet):\n"
                    "        class Meta:\n"
                    "            model = Account\n"
                    "            fields = '__all__'\n"
                ),
                "shop/api.py": FILTERED.replace(
                    "from shop.models import Account",
                    "from shop.models import Account\nfrom shop.filters import AccountFilter",
                )
                + "    filterset_class = AccountFilter\n",
            },
        )
        assert len(found) == 1
        assert found[0].location.file == "shop/filters.py"

    def test_reports_a_substring_lookup_on_a_secret(self, make_project) -> None:
        found = filtered(
            make_project,
            "    filterset_fields = {'password': ['startswith'], 'status': ['exact']}\n",
        )
        assert len(found) == 1
        assert "startswith" in found[0].message
        assert found[0].severity is Severity.HIGH

    def test_silent_on_an_explicit_safe_field_list(self, make_project) -> None:
        found = filtered(make_project, "    filterset_fields = ['status', 'email']\n")
        assert found == []

    def test_silent_without_a_backend_to_read_it(self, make_project) -> None:
        """`filterset_fields` with no filter backend is a comment."""
        found = run(
            make_project,
            "DJA-014",
            api_source=FILTERED.replace("    filter_backends = [DjangoFilterBackend]\n", "")
            + "    filterset_fields = '__all__'\n",
            urls=ACCOUNT_ROUTED,
            **{
                "shop/models.py": ACCOUNT,
                "django_filters/__init__.py": DJANGO_FILTERS,
                "django_filters/rest_framework.py": "class DjangoFilterBackend:\n    pass\n",
            },
        )
        assert found == []

    def test_a_project_default_backend_counts(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-014",
            api_source=FILTERED.replace("    filter_backends = [DjangoFilterBackend]\n", "")
            + "    filterset_fields = '__all__'\n",
            urls=ACCOUNT_ROUTED,
            settings=settings_with(
                DEFAULT_FILTER_BACKENDS=("['django_filters.rest_framework.DjangoFilterBackend']")
            ),
            **{
                "shop/models.py": ACCOUNT,
                "django_filters/__init__.py": DJANGO_FILTERS,
                "django_filters/rest_framework.py": "class DjangoFilterBackend:\n    pass\n",
            },
        )
        assert len(found) == 1

    def test_reports_once_for_a_viewset_routed_to_many_methods(self, make_project) -> None:
        found = filtered(make_project, "    filterset_fields = '__all__'\n")
        assert len(found) == 1

    def test_filterset_class_wins_over_filterset_fields(self, make_project) -> None:
        """`get_filterset_class` returns the class before reading the fields."""
        found = filtered(
            make_project,
            "    filterset_fields = '__all__'\n",
            **{
                "shop/api.py": FILTERED.replace(
                    "from shop.models import Account",
                    "from shop.models import Account\nfrom shop.filters import AccountFilter",
                )
                + "    filterset_class = AccountFilter\n"
                + "    filterset_fields = '__all__'\n",
                "shop/filters.py": (
                    "from django_filters import FilterSet\n"
                    "from shop.models import Account\n\n"
                    "class AccountFilter(FilterSet):\n"
                    "    class Meta:\n"
                    "        model = Account\n"
                    "        fields = ['status']\n"
                ),
            },
        )
        assert found == []


OPEN_LOGIN = """
from rest_framework.views import APIView

class LoginView(APIView):
    permission_classes = []

    def post(self, request):
        return None
"""

LOGIN_URLS = """
from django.urls import path
from shop.api import LoginView

urlpatterns = [path('login/', LoginView.as_view(), name='login')]
"""


def credentials(make_project, api_source: str = OPEN_LOGIN, **kwargs) -> list[Finding]:
    return run(make_project, "DJA-015", api_source=api_source, urls=LOGIN_URLS, **kwargs)


class TestUnthrottledCredentialEndpoint:
    """DJA-015 -- the login form that answers as fast as it is asked."""

    def test_reports_an_unthrottled_login(self, make_project) -> None:
        found = credentials(make_project)
        assert len(found) == 1
        assert found[0].severity is Severity.HIGH
        assert "DEFAULT_THROTTLE_CLASSES" in found[0].message

    def test_silent_when_a_throttle_is_configured(self, make_project) -> None:
        found = credentials(
            make_project,
            settings=settings_with(
                DEFAULT_THROTTLE_CLASSES="['rest_framework.throttling.AnonRateThrottle']",
                DEFAULT_THROTTLE_RATES="{'anon': '10/hour'}",
            ),
        )
        assert found == []

    def test_reports_a_view_that_switched_the_default_off(self, make_project) -> None:
        found = credentials(
            make_project,
            api_source=OPEN_LOGIN.replace(
                "    permission_classes = []",
                "    permission_classes = []\n    throttle_classes = []",
            ),
            settings=settings_with(
                DEFAULT_THROTTLE_CLASSES="['rest_framework.throttling.AnonRateThrottle']",
                DEFAULT_THROTTLE_RATES="{'anon': '10/hour'}",
            ),
        )
        assert len(found) == 1
        assert "throttle_classes = []" in found[0].message

    def test_reports_scoped_throttling_with_no_scope(self, make_project) -> None:
        """`ScopedRateThrottle.allow_request` returns True with no throttle_scope.

        The settings file names a throttle class and every view that forgot to
        set a scope is unthrottled, which is the PAGE_SIZE shape again.
        """
        found = credentials(
            make_project,
            settings=settings_with(
                DEFAULT_THROTTLE_CLASSES="['rest_framework.throttling.ScopedRateThrottle']",
                DEFAULT_THROTTLE_RATES="{'login': '10/hour'}",
            ),
        )
        assert len(found) == 1
        assert "allow_request returns True" in found[0].message

    def test_silent_when_the_scope_is_set(self, make_project) -> None:
        found = credentials(
            make_project,
            api_source=OPEN_LOGIN.replace(
                "    permission_classes = []",
                "    permission_classes = []\n    throttle_scope = 'login'",
            ),
            settings=settings_with(
                DEFAULT_THROTTLE_CLASSES="['rest_framework.throttling.ScopedRateThrottle']",
                DEFAULT_THROTTLE_RATES="{'login': '10/hour'}",
            ),
        )
        assert found == []

    def test_reports_a_throttle_with_no_matching_rate(self, make_project) -> None:
        found = credentials(
            make_project,
            settings=settings_with(
                DEFAULT_THROTTLE_CLASSES="['rest_framework.throttling.AnonRateThrottle']",
                DEFAULT_THROTTLE_RATES="{'burst': '10/hour'}",
            ),
        )
        assert len(found) == 1
        assert "DEFAULT_THROTTLE_RATES" in found[0].message

    def test_silent_behind_a_login(self, make_project) -> None:
        """An endpoint already requiring authentication is not where guessing happens."""
        found = credentials(
            make_project,
            api_source=OPEN_LOGIN.replace(
                "    permission_classes = []",
                "    permission_classes = [IsAuthenticated]",
            ).replace(
                "from rest_framework.views import APIView",
                "from rest_framework.views import APIView\n"
                "from rest_framework.permissions import IsAuthenticated",
            ),
        )
        assert found == []

    def test_silent_on_an_anonymous_read(self, make_project) -> None:
        """Checking a credential means sending one."""
        found = credentials(
            make_project,
            api_source=OPEN_LOGIN.replace("def post", "def get"),
        )
        assert found == []

    def test_an_anonymous_write_that_names_nothing_is_quieter(self, make_project) -> None:
        """The filter is the anonymous POST; the name only decides how loudly.

        Across 227 routed endpoints on NetBox and pretix there are exactly two
        anonymous writes and both hand out credentials, so requiring the name
        to say so would have missed one of them.
        """
        found = run(
            make_project,
            "DJA-015",
            api_source=OPEN_LOGIN.replace("LoginView", "SubmitView"),
            urls=LOGIN_URLS.replace("LoginView", "SubmitView").replace("login/", "submit/"),
        )
        assert len(found) == 1
        assert found[0].severity is Severity.MEDIUM
        assert found[0].confidence is Confidence.TENTATIVE

    def test_matches_on_the_url_pattern(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-015",
            api_source=OPEN_LOGIN.replace("LoginView", "SubmitView"),
            urls=LOGIN_URLS.replace("LoginView", "SubmitView").replace(
                "'login/'", "'password/reset/'"
            ),
        )
        assert len(found) == 1
        assert found[0].severity is Severity.HIGH

    def test_matches_drfs_own_base_class(self, make_project) -> None:
        found = run(
            make_project,
            "DJA-015",
            api_source=(
                "from rest_framework.authtoken.views import ObtainAuthToken\n\n"
                "class SubmitView(ObtainAuthToken):\n"
                "    permission_classes = []\n\n"
                "    def post(self, request):\n"
                "        return None\n"
            ),
            urls=LOGIN_URLS.replace("LoginView", "SubmitView").replace("login/", "go/"),
            **{
                "rest_framework/authtoken/__init__.py": "",
                "rest_framework/authtoken/views.py": (
                    "from rest_framework.views import APIView\n\n"
                    "class ObtainAuthToken(APIView):\n    pass\n"
                ),
            },
        )
        assert len(found) == 1
        assert found[0].severity is Severity.HIGH
