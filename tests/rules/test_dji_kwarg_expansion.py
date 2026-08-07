"""DJI-005 -- request data expanded into queryset keyword arguments.

The measurement that shaped this rule is the one worth keeping in view while
reading the tests: all twelve calls in the corpus that expand a request source
into a method with one of these names are
``self.get(self.request, *self.args, **self.kwargs)`` -- a class-based view
re-rendering its form, not a queryset at all. So there is a test for that exact
shape, and the receiver test that excludes it is the rule's most important
guard rather than an afterthought.
"""

from __future__ import annotations

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

class Book(models.Model):
    title = models.CharField(max_length=200)
    secret = models.CharField(max_length=200)
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
    rule = next(r for r in all_rules() if r.meta.id == "DJI-005")
    return list(rule().check(ctx))


class TestLookupMethods:
    def test_filter(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.GET)
            """,
        )
        assert len(found) == 1
        assert found[0].rule_id == "DJI-005"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.HIGH
        assert found[0].properties["method"] == "filter"
        assert found[0].properties["kind"] == "lookup injection"

    def test_exclude(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.exclude(**request.GET)
            """,
        )
        assert len(found) == 1
        assert found[0].properties["method"] == "exclude"

    def test_get(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def show(request):
                return Book.objects.get(**request.GET)
            """,
        )
        assert len(found) == 1

    def test_get_or_create(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def show(request):
                return Book.objects.get_or_create(**request.POST)
            """,
        )
        assert len(found) == 1

    def test_query_params_is_drfs_alias(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.query_params)
            """,
        )
        assert len(found) == 1

    def test_expansion_at_the_end_of_a_chain(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.all().order_by("title").filter(**request.GET)
            """,
        )
        assert len(found) == 1

    def test_two_expansions_are_two_findings(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.GET).exclude(**request.POST)
            """,
        )
        assert len(found) == 2

    def test_a_chained_lookup_is_still_found(self, make_project):
        """The tracker keys a chain only at its outermost call.

        Reading that key alone would miss every lookup written before the last
        one, which on the benchmark corpus is nine of sixty-three real calls.
        """
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.GET).exclude(archived=True)
            """,
        )
        assert len(found) == 1
        assert found[0].location.line == 4

    def test_url_captures_without_the_word_request(self, make_project):
        """``self.kwargs`` is client text that arrives without a request name.

        Deliberately written with no ``request`` anywhere, because the file
        prefilter admits on ``request`` *or* ``self.kwargs``: drop the second
        word and this true positive disappears silently.
        """
        found = findings(
            make_project,
            """
            from django.views.generic import View
            from library.models import Book

            class BookView(View):
                def get(self, *args, **kwargs):
                    return Book.objects.filter(**self.kwargs)
            """,
        )
        assert len(found) == 1
        assert "self.kwargs" in found[0].message

    def test_a_lookup_before_a_terminal_method_is_still_found(self, make_project):
        """``.count()`` ends the chain, so it holds the tracker's only key."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.GET).count()
            """,
        )
        assert len(found) == 1

    def test_a_lookup_before_a_slice_is_still_found(self, make_project):
        """A subscript heads the chain, and is not a call at all."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.GET)[:10]
            """,
        )
        assert len(found) == 1


class TestWriteMethods:
    def test_create_is_mass_assignment(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def add(request):
                return Book.objects.create(**request.POST)
            """,
        )
        assert len(found) == 1
        assert found[0].properties["kind"] == "mass assignment"
        assert "which fields are written" in found[0].message

    def test_update_is_mass_assignment(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def edit(request):
                return Book.objects.filter(pk=1).update(**request.POST)
            """,
        )
        assert len(found) == 1
        assert found[0].properties["kind"] == "mass assignment"


class TestWhatItDeclines:
    def test_the_class_based_view_idiom(self, make_project):
        """The whole reason the receiver is tested.

        All twelve corpus calls that reach a request source through one of
        these method names are this shape. `self.get` is the view's own
        handler; it shares a name with `QuerySet.get` and nothing else.
        """
        assert (
            findings(
                make_project,
                """
                from django.views.generic import View

                class BookView(View):
                    def post(self, request, *args, **kwargs):
                        return self.get(self.request, *self.args, **self.kwargs)
                """,
            )
            == []
        )

    def test_a_plain_dictionary_expansion(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.filter(**{"title": "x"})
                """,
            )
            == []
        )

    def test_an_allowlisted_mapping(self, make_project):
        """The remediation, which must not be reported as the defect."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                ALLOWED = {"title"}

                def search(request):
                    terms = {k: v for k, v in request.GET.items() if k in ALLOWED}
                    return Book.objects.filter(**terms)
                """,
            )
            == []
        )

    def test_keywords_written_out(self, make_project):
        """A named keyword is a field the developer chose, not one the client did.

        The safe expansion below is load-bearing: without a ``**`` somewhere in
        the file the prefilter rejects it, and this test would pass without ever
        reaching the guard it exists to hold.
        """
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    Book.objects.filter(**{"archived": False})
                    return Book.objects.filter(title=request.GET["q"])
                """,
            )
            == []
        )

    def test_a_method_django_parses_no_lookups_from(self, make_project):
        """``annotate`` takes expressions, so a request string there is a TypeError.

        ``order_by(*field_names)`` and ``values_list(*fields, flat, named)`` take
        no ``**kwargs`` at all, per their signatures. None of the three can be
        steered into a lookup, so none of them belong in the method set.
        """
        for method in ("annotate", "aggregate", "alias", "values", "order_by", "values_list"):
            assert (
                findings(
                    make_project,
                    f"""
                    from library.models import Book

                    def search(request):
                        return Book.objects.{method}(**request.GET)
                    """,
                )
                == []
            ), method

    def test_a_call_that_is_not_a_method(self, make_project):
        """``dict(**request.GET)`` has no receiver to be a queryset."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    terms = dict(**request.GET)
                    return Book.objects.filter(title=terms.get("title"))
                """,
            )
            == []
        )

    def test_expansion_into_something_that_is_not_a_queryset(self, make_project):
        """The safe queryset call keeps the file past the prefilter."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request, widget):
                    Book.objects.filter(title="x")
                    return widget.filter(**request.GET)
                """,
            )
            == []
        )

    def test_a_method_that_takes_no_lookup_keywords(self, make_project):
        """`order_by(*field_names)` has no **kwargs, per its signature."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.order_by(*request.GET.getlist("o"))
                """,
            )
            == []
        )

    def test_a_mapping_this_analysis_cannot_place(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(terms):
                    return Book.objects.filter(**terms)
                """,
            )
            == []
        )


class TestHowItSpeaks:
    def test_the_message_names_the_method_and_the_mapping(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.GET)
            """,
        )
        assert "filter() is called" in found[0].message
        assert "request.GET" in found[0].message
        assert "which field is queried" in found[0].message

    def test_a_direct_expansion_is_certain(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.GET)
            """,
        )
        assert found[0].confidence is Confidence.CERTAIN

    def test_a_mapping_reached_through_a_name_is_firm(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                terms = request.GET
                return Book.objects.filter(**terms)
            """,
        )
        assert len(found) == 1
        assert found[0].confidence is Confidence.FIRM
        assert found[0].properties["mapping"] == "terms"

    def test_the_evidence_is_the_call(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(**request.GET)
            """,
        )
        assert len(found[0].evidence) == 1
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "filter(**request.GET)" in found[0].evidence[0].content


class TestWithNothingToReasonFrom:
    def test_a_project_with_no_expansion(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.all()
                """,
            )
            == []
        )

    def test_an_expansion_with_no_request_in_sight(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                DEFAULTS = {"title": "x"}

                def seed():
                    return Book.objects.create(**DEFAULTS)
                """,
            )
            == []
        )
