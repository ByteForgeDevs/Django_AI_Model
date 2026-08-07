"""DJI-006 -- the client chooses the ordering field.

The measurement that shaped this rule: both places in the benchmark corpus that
let a request choose an ordering allowlist it first. netbox compares against an
``ORDERING_CHOICES`` dict and reassigns a default, healthchecks tests
``request.GET.get("sort") in VALID_SORT_VALUES``. A rule without the membership
guard would call both of them defects, so the guard has its own test class and
those two shapes are reproduced literally.

The other thing worth keeping in view is what this rule must *not* claim.
``order_by`` resolves its argument to a field and raises ``FieldError``
otherwise, so nothing here is SQL injection, and there is a test asserting the
message says as much.
"""

from __future__ import annotations

import ast
import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity
from djaudit.registry import all_rules

SETTINGS = """
SECRET_KEY = "x"
DEBUG = False
ALLOWED_HOSTS = ["example.com"]
INSTALLED_APPS = ["library"]
ROOT_URLCONF = "library.urls"
"""

MODELS = """
from django.db import models

class Account(models.Model):
    email = models.CharField(max_length=200)
    password_hash = models.CharField(max_length=200)

class Book(models.Model):
    title = models.CharField(max_length=200)
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
"""


def findings(make_project, code: str) -> list[Finding]:
    ctx: ProjectContext = make_project(
        {
            "manage.py": "import os\n",
            "library/__init__.py": "",
            "library/settings.py": SETTINGS,
            "library/urls.py": "urlpatterns = []\n",
            "library/models.py": MODELS,
            "library/views.py": textwrap.dedent(code).lstrip(),
        }
    )
    rule = next(r for r in all_rules() if r.meta.id == "DJI-006")
    return list(rule().check(ctx))


class TestWhatItFinds:
    def test_a_request_parameter_read_directly(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                return Book.objects.order_by(request.GET["sort"])
            """,
        )
        assert len(found) == 1
        assert found[0].rule_id == "DJI-006"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.MEDIUM
        assert found[0].confidence is Confidence.CERTAIN

    def test_a_request_parameter_through_a_name(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                sort = request.GET.get("sort", "title")
                return Book.objects.order_by(sort)
            """,
        )
        assert len(found) == 1
        assert found[0].confidence is Confidence.FIRM

    def test_a_chained_order_by(self, make_project):
        """The tracker keys a chain at its outermost call only."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                return Book.objects.order_by(request.GET["sort"]).filter(title="x")
            """,
        )
        assert len(found) == 1

    def test_url_captures_without_the_word_request(self, make_project):
        """``self.kwargs`` is the other source word the prefilter admits on."""
        found = findings(
            make_project,
            """
            from django.views.generic import View
            from library.models import Book

            class Listing(View):
                def get(self, *args, **kwargs):
                    return Book.objects.order_by(self.kwargs["sort"])
            """,
        )
        assert len(found) == 1

    def test_a_starred_expansion(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                return Book.objects.order_by(*request.GET.getlist("sort"))
            """,
        )
        assert len(found) == 1
        assert found[0].properties["starred"] == "true"
        assert found[0].properties["argument"] == "request.GET.getlist('sort')"

    def test_two_arguments_are_two_findings(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                return Book.objects.order_by(request.GET["a"], request.GET["b"])
            """,
        )
        assert len(found) == 2


class TestTheAllowlistGuard:
    def test_the_netbox_shape(self, make_project):
        """Compare against a container, reassign a default, then order.

        Reproduced from netbox dcim/views.py, which is one of only two places
        in the corpus that lets a request choose an ordering at all.
        """
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def listing(request):
                    ORDERING_CHOICES = {"title": "Title", "-title": "Title desc"}
                    sort = request.GET.get("sort", "title")
                    if sort not in ORDERING_CHOICES:
                        sort = "title"
                    return Book.objects.order_by(sort)
                """,
            )
            == []
        )

    def test_the_healthchecks_shape(self, make_project):
        """Test the request read itself for membership, then use it."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                VALID_SORT_VALUES = {"title", "-title"}

                def listing(request):
                    sort = "title"
                    if request.GET.get("sort") in VALID_SORT_VALUES:
                        sort = request.GET["sort"]
                    return Book.objects.order_by(sort)
                """,
            )
            == []
        )

    def test_a_conditional_expression_allowlist(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                ALLOWED = {"title"}

                def listing(request):
                    raw = request.GET.get("sort")
                    sort = raw if raw in ALLOWED else "title"
                    return Book.objects.order_by(sort)
                """,
            )
            == []
        )

    def test_a_comparison_that_is_not_membership_does_not_excuse_it(self, make_project):
        """``==`` is not an allowlist, and must not be mistaken for one."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                sort = request.GET.get("sort", "title")
                if sort == "":
                    sort = "title"
                return Book.objects.order_by(sort)
            """,
        )
        assert len(found) == 1

    def test_a_membership_test_on_a_different_value_does_not_excuse_it(self, make_project):
        """The guard matches identity, not the mere presence of an ``in``.

        Here the allowlist concerns the page number and the ordering field is
        left unchecked, which is exactly the bug the rule is for. Matching any
        membership test in the function would have missed it.
        """
        found = findings(
            make_project,
            """
            from library.models import Book

            ALLOWED_PAGES = {"1", "2"}

            def listing(request):
                page = request.GET.get("page")
                if page not in ALLOWED_PAGES:
                    page = "1"
                sort = request.GET.get("sort", "title")
                return Book.objects.order_by(sort)
            """,
        )
        assert len(found) == 1

    def test_a_membership_test_on_a_different_request_parameter(self, make_project):
        """Same point, written the way healthchecks writes its guard."""
        found = findings(
            make_project,
            """
            from library.models import Book

            ALLOWED_PAGES = {"1", "2"}

            def listing(request):
                if request.GET.get("page") in ALLOWED_PAGES:
                    pass
                return Book.objects.order_by(request.GET["sort"])
            """,
        )
        assert len(found) == 1

    def test_a_guard_in_a_nested_function_does_not_excuse_it(self, make_project):
        """The guard reads this scope's own nodes, not a closure's.

        A membership test inside a nested function runs when that function is
        called, which may be never and is certainly not here. Reading it would
        let unrelated inner code silence the ordering in the enclosing view.
        """
        found = findings(
            make_project,
            """
            from library.models import Book

            ALLOWED = {"title"}

            def listing(request):
                sort = request.GET.get("sort", "title")

                def check(value):
                    return sort in ALLOWED

                return Book.objects.order_by(sort)
            """,
        )
        assert len(found) == 1

    def test_the_request_method_idiom_does_not_excuse_it(self, make_project):
        """``if request.method in (...)`` is the commonest ``in`` in any view.

        Matching on the name ``request`` would have made this excuse every
        ordering in every view that checks its method, which is most of them.
        """
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                if request.method in ("GET", "HEAD"):
                    pass
                return Book.objects.order_by(request.GET["sort"])
            """,
        )
        assert len(found) == 1


class TestWhatItDeclines:
    def test_a_literal_ordering(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def listing(request):
                    return Book.objects.order_by("title")
                """,
            )
            == []
        )

    def test_an_orm_expression(self, make_project):
        """``order_by(Lower('name'))`` is most of what the corpus actually holds."""
        assert (
            findings(
                make_project,
                """
                from django.db.models.functions import Lower
                from library.models import Book

                def listing(request):
                    return Book.objects.order_by(Lower("title"))
                """,
            )
            == []
        )

    def test_ordering_something_that_is_not_a_queryset(self, make_project):
        """The safe queryset call keeps the file past the prefilter."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def listing(request, table):
                    Book.objects.order_by("title")
                    return table.order_by(request.GET["sort"])
                """,
            )
            == []
        )

    def test_a_field_chosen_from_a_literal_mapping(self, make_project):
        """pretix iterates a hardcoded dict into order_by; that is not taint."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def listing(request):
                    fields = {"name": "title", "size": "title"}
                    out = []
                    for key, field in fields.items():
                        out.append(Book.objects.order_by(field))
                    return out
                """,
            )
            == []
        )

    def test_a_method_that_is_not_order_by(self, make_project):
        """Only ordering is examined, and the method name is what says so.

        ``values_list`` also takes positional field names, and a tainted one
        there is a different defect with a different answer. Widening the
        method set would report it as an ordering oracle.
        """
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def listing(request):
                    return Book.objects.values_list(request.GET["field"])
                """,
            )
            == []
        )

    def test_earliest_and_latest_are_not_examined(self, make_project):
        """Documented as a limitation: neither returns an ordered page.

        Asserted against :func:`arguments` rather than through ``check``,
        because ``latest()`` is a terminal method the queryset tracker already
        declines -- so a rule-level test would pass whether or not the method
        filter did anything.
        """
        from djaudit.rules.ordering import arguments

        def parse(text: str) -> ast.expr:
            statement = ast.parse(text).body[0]
            assert isinstance(statement, ast.Expr)
            return statement.value

        for method in ("latest", "earliest", "distinct"):
            assert list(arguments(parse(f'qs.{method}(request.GET["s"])'))) == [], method

        assert len(list(arguments(parse('qs.order_by(request.GET["s"])')))) == 1


class TestHowItSpeaks:
    def test_it_does_not_call_this_sql_injection(self, make_project):
        """The rule's central factual claim, and the easiest one to get wrong."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                return Book.objects.order_by(request.GET["sort"])
            """,
        )
        assert len(found) == 1
        assert "not SQL injection" in found[0].message

    def test_it_quotes_the_argument_and_the_source(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                return Book.objects.order_by(request.GET["sort"])
            """,
        )
        assert "request.GET" in found[0].message
        assert found[0].properties["source"] == "request.GET"

    def test_it_carries_ast_evidence(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing(request):
                return Book.objects.order_by(request.GET["sort"])
            """,
        )
        assert found[0].evidence
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "order_by" in found[0].evidence[0].content


class TestWithNothingToReasonFrom:
    def test_an_empty_project(self, make_project):
        ctx: ProjectContext = make_project({"manage.py": "import os\n"})
        rule = next(r for r in all_rules() if r.meta.id == "DJI-006")
        assert list(rule().check(ctx)) == []

    def test_a_file_that_never_orders(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def listing(request):
                    return Book.objects.filter(title=request.GET["q"])
                """,
            )
            == []
        )
