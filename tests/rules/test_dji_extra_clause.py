"""DJI-003 -- request data interpolated into a ``.extra()`` clause.

``.extra()`` appears in none of the 3,091 files of the three benchmark corpora,
so unlike its siblings this rule has no real-world code holding it honest.
These tests are the whole of its evidence, and they are written against
Django's documented signature -- read from ``inspect.signature`` rather than
from memory, which is how the ``having`` argument that has not existed for
years got removed from an earlier draft.

The split that matters is between the four SQL arguments and the two value
arguments, so every one of the six is here.
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
    rule = next(r for r in all_rules() if r.meta.id == "DJI-003")
    return list(rule().check(ctx))


class TestTheFourSqlArguments:
    def test_where(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(where=[f"title = '{request.GET['q']}'"])
            """,
        )
        assert len(found) == 1
        assert found[0].rule_id == "DJI-003"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.CRITICAL
        assert found[0].properties["clause"] == "where"

    def test_select(self, make_project):
        """``select`` is a mapping, and the SQL is in its values."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(
                    select={"c": f"SELECT COUNT(*) FROM x WHERE t = '{request.GET['q']}'"}
                )
            """,
        )
        assert len(found) == 1
        assert found[0].properties["clause"] == "select"

    def test_tables(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(tables=[f"{request.GET['t']}"])
            """,
        )
        assert len(found) == 1
        assert found[0].properties["clause"] == "tables"

    def test_order_by(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(order_by=[f"-{request.GET['sort']}"])
            """,
        )
        assert len(found) == 1
        assert found[0].properties["clause"] == "order_by"

    def test_a_positional_where(self, make_project):
        """``extra(select, where, ...)`` -- position two is the where list."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(None, [f"title = '{request.GET['q']}'"])
            """,
        )
        assert len(found) == 1
        assert found[0].properties["clause"] == "where"

    def test_two_clauses_are_two_findings(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(
                    where=[f"title = '{request.GET['q']}'"],
                    order_by=[f"-{request.GET['sort']}"],
                )
            """,
        )
        assert len(found) == 2
        assert {f.properties["clause"] for f in found} == {"where", "order_by"}

    def test_two_entries_in_one_clause_are_two_findings(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(
                    where=[f"a = '{request.GET['a']}'", f"b = '{request.GET['b']}'"]
                )
            """,
        )
        assert len(found) == 2


class TestTheTwoValueArguments:
    def test_params_is_the_fix(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.extra(
                        where=["title = %s"], params=[f"{request.GET['q']}"]
                    )
                """,
            )
            == []
        )

    def test_select_params_is_the_fix(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.extra(
                        select={"c": "x = %s"}, select_params=[f"{request.GET['q']}"]
                    )
                """,
            )
            == []
        )

    def test_params_in_its_positional_place(self, make_project):
        """Position three is ``params``, and must not be read as SQL."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.extra(
                        None, ["title = %s"], [f"{request.GET['q']}"]
                    )
                """,
            )
            == []
        )


class TestWhatItDeclines:
    def test_a_clause_written_whole(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.extra(where=["title IS NOT NULL"])
                """,
            )
            == []
        )

    def test_a_table_name_from_model_metadata(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.extra(tables=[f"{Book._meta.db_table}"])
                """,
            )
            == []
        )

    def test_a_value_this_analysis_cannot_place(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def helper(term):
                    return Book.objects.extra(where=[f"title = '{term}'"])
                """,
            )
            == []
        )

    def test_extra_on_something_that_is_not_a_queryset(self, make_project):
        """The safe queryset call keeps the file past the prefilter."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request, widget):
                    Book.objects.extra(where=["1 = 1"])
                    return widget.extra(where=[f"title = '{request.GET['q']}'"])
                """,
            )
            == []
        )

    def test_a_sql_keyword_on_a_method_that_is_not_extra(self, make_project):
        """``where=`` alone is not the trigger; the method name is.

        A mutation probe removed the ``attr != EXTRA`` test and no test
        noticed, because every fixture that reached the check was already an
        ``.extra()`` call. The safe ``.extra()`` here keeps the file past the
        file-level prefilter so the method-name check is the only thing left
        standing between this call and a finding.
        """
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    Book.objects.extra(where=["1 = 1"])
                    return Book.objects.filter(where=[f"title = '{request.GET['q']}'"])
                """,
            )
            == []
        )


class TestHowItSpeaks:
    def test_the_message_names_the_clause(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(where=[f"title = '{request.GET['q']}'"])
            """,
        )
        assert "where clause of .extra()" in found[0].message
        assert "params" in found[0].message

    def test_the_message_names_a_clause_that_is_not_where(self, make_project):
        """The contrast, so a hardcoded ``where`` in the message cannot pass."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(select={"hit": f"title = '{request.GET['q']}'"})
            """,
        )
        assert "select clause of .extra()" in found[0].message
        assert found[0].properties["clause"] == "select"

    def test_direct_interpolation_is_certain(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(where=[f"title = '{request.GET['q']}'"])
            """,
        )
        assert found[0].confidence is Confidence.CERTAIN

    def test_taint_through_a_name_is_firm(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                term = request.GET["q"]
                return Book.objects.extra(where=[f"title = '{term}'"])
            """,
        )
        assert found[0].confidence is Confidence.FIRM

    def test_the_evidence_is_the_composed_clause(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.extra(where=[f"title = '{request.GET['q']}'"])
            """,
        )
        assert len(found[0].evidence) == 1
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "title = " in found[0].evidence[0].content


class TestWithNothingToReasonFrom:
    def test_a_project_with_no_extra_call(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.filter(title=request.GET["q"])
                """,
            )
            == []
        )

    def test_an_extra_call_with_no_arguments(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.extra()
                """,
            )
            == []
        )

    def test_a_clause_list_held_in_a_local(self, make_project):
        """The list is a name, so the elements cannot be read one by one.

        ``compose`` resolves the name, finds a list rather than a composed
        string, and declines -- which is the honest answer, not a claim that
        the code is safe.
        """
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    clauses = [f"title = '{request.GET['q']}'"]
                    return Book.objects.extra(where=clauses)
                """,
            )
            == []
        )

    def test_more_positional_arguments_than_extra_accepts(self, make_project):
        """Seven positional arguments must not walk off the end of POSITIONS.

        Nobody writes this, which is why the bounds check went untested until a
        mutation probe removed it. A rule that raises is swallowed into
        `rule_errors` and reports nothing at all, so an IndexError here would
        silently disable the rule for the whole file.
        """
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.extra(1, 2, 3, 4, 5, 6, 7)
                """,
            )
            == []
        )
