"""DJI-002 -- request data interpolated into a ``.raw()`` query.

Two things carry this rule, and they get most of the tests.

The first is that ``.raw()`` looks like ORM usage. It sits in a chain beside
``.filter()``, it returns model instances, and none of that touches the string.
So the reporting tests are about the string, and the declining tests are about
everything the ORM does correctly a line away.

The second is the receiver. ``.raw`` is a method on other objects -- a
``requests`` response has one -- and the rule answers by asking the queryset
tracker rather than by matching names. That is only worth anything if it
accepts the shapes people write, so all four are here: a manager, a manager
through a local, a chain, and a keyword argument.
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
    rule = next(r for r in all_rules() if r.meta.id == "DJI-002")
    return list(rule().check(ctx))


class TestWhatItReports:
    def test_an_fstring_on_a_manager(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw(
                    f"SELECT * FROM book WHERE title = '{request.GET['q']}'"
                )
            """,
        )
        assert len(found) == 1
        assert found[0].rule_id == "DJI-002"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.CRITICAL
        assert found[0].properties["composition"] == "f-string"
        assert found[0].properties["tainted"] == "request.GET['q']"

    def test_percent_formatting(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw("SELECT * FROM book WHERE t = '%s'" % request.POST["q"])
            """,
        )
        assert len(found) == 1
        assert found[0].properties["composition"] == "%-formatting"

    def test_str_format(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                sql = "SELECT * FROM book WHERE t = '{}'".format(request.GET["q"])
                return Book.objects.raw(sql)
            """,
        )
        assert len(found) == 1
        assert found[0].properties["composition"] == ".format()"

    def test_concatenation(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw("SELECT * FROM book WHERE t = '" + request.GET["q"] + "'")
            """,
        )
        assert len(found) == 1
        assert found[0].properties["composition"] == "concatenation"

    def test_the_statement_passed_by_keyword(self, make_project):
        """Django names the parameter ``raw_query``, so it can be a keyword."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw(
                    raw_query=f"SELECT * FROM book WHERE t = '{request.GET['q']}'"
                )
            """,
        )
        assert len(found) == 1

    def test_a_queryset_held_in_a_local(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                books = Book.objects
                return books.raw(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found) == 1

    def test_a_chain_ending_in_raw(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(pk__gt=1).raw(
                    f"SELECT * FROM book WHERE t = '{request.GET['q']}'"
                )
            """,
        )
        assert len(found) == 1

    def test_taint_arriving_through_a_local(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                term = request.GET["q"]
                return Book.objects.raw(f"SELECT * FROM book WHERE t = '{term}'")
            """,
        )
        assert len(found) == 1
        assert found[0].properties["tainted"] == "term"


class TestWhatItDeclines:
    def test_a_statement_written_whole(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.raw("SELECT * FROM book")
                """,
            )
            == []
        )

    def test_the_value_passed_in_params(self, make_project):
        """The fix, which must not read as the defect."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.raw(
                        "SELECT * FROM book WHERE t = %s", [request.GET["q"]]
                    )
                """,
            )
            == []
        )

    def test_a_table_name_from_model_metadata(self, make_project):
        """An identifier cannot be a parameter, so interpolating one is correct."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.raw(f"SELECT * FROM {Book._meta.db_table}")
                """,
            )
            == []
        )

    def test_a_value_this_analysis_cannot_place(self, make_project):
        """Unknown is an answer the rule keeps to itself."""
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def helper(term):
                    return Book.objects.raw(f"SELECT * FROM book WHERE t = '{term}'")
                """,
            )
            == []
        )

    def test_the_orm_doing_it_properly_alongside(self, make_project):
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


class TestFindingTheQueryset:
    def test_a_response_from_the_requests_library(self, make_project):
        """``Response.raw`` is the false positive a name match would produce."""
        assert (
            findings(
                make_project,
                """
                import requests

                def fetch(request):
                    response = requests.get("https://example.com")
                    return response.raw(f"SELECT {request.GET['q']}")
                """,
            )
            == []
        )

    def test_a_cursor(self, make_project):
        assert (
            findings(
                make_project,
                """
                from django.db import connection

                def search(request):
                    with connection.cursor() as cursor:
                        return cursor.raw(f"SELECT {request.GET['q']}")
                """,
            )
            == []
        )

    def test_an_object_that_arrived_as_a_parameter(self, make_project):
        assert (
            findings(
                make_project,
                """
                def search(request, thing):
                    return thing.raw(f"SELECT {request.GET['q']}")
                """,
            )
            == []
        )

    def test_the_contrast(self, make_project):
        """The same statement on a real queryset, so the silence above is the
        receiver test and not the taint judgement."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw(f"SELECT {request.GET['q']}")
            """,
        )
        assert len(found) == 1


class TestHowItSpeaks:
    def test_the_message_quotes_the_receiver(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert "Book.objects.raw()" in found[0].message
        assert "params" in found[0].message

    def test_a_long_receiver_is_cut(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.filter(pk__gt=1).exclude(title="").order_by("pk").raw(
                    f"SELECT * FROM book WHERE t = '{request.GET['q']}'"
                )
            """,
        )
        assert "..." in found[0].message
        assert len(found[0].message.split(" builds the SQL passed to ")[1].split(".raw()")[0]) <= 40

    def test_direct_interpolation_is_certain(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
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
                return Book.objects.raw(f"SELECT * FROM book WHERE t = '{term}'")
            """,
        )
        assert found[0].confidence is Confidence.FIRM

    def test_the_evidence_is_the_composed_statement(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found[0].evidence) == 1
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "SELECT * FROM book" in found[0].evidence[0].content
        assert found[0].evidence[0].source.endswith("views.py:4")


class TestWithNothingToReasonFrom:
    def test_a_project_with_no_raw_call(self, make_project):
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

    def test_a_raw_call_with_no_argument(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.raw()
                """,
            )
            == []
        )


class TestAStatementBuiltIntoALocal:
    def test_an_fstring_assigned_then_passed(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                sql = f"SELECT * FROM book WHERE t = '{request.GET['q']}'"
                return Book.objects.raw(sql)
            """,
        )
        assert len(found) == 1
        assert "assigned to sql" in found[0].message

    def test_a_local_holding_a_literal(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    sql = "SELECT * FROM book"
                    return Book.objects.raw(sql)
                """,
            )
            == []
        )

    def test_the_receiver_is_still_checked(self, make_project):
        """Indirect composition must not bypass the queryset test."""
        assert (
            findings(
                make_project,
                """
                import requests

                def fetch(request):
                    sql = f"SELECT {request.GET['q']}"
                    return requests.get("https://example.com").raw(sql)
                """,
            )
            == []
        )


class TestTellingRawFromEverythingElse:
    def test_a_composed_value_passed_in_params(self, make_project):
        """The statement is the first argument, and only the first.

        A params list can hold anything -- including an f-string -- and still
        be safe, because the driver never parses it. A rule that read the last
        argument would report the fix.
        """
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    return Book.objects.raw(
                        "SELECT * FROM book WHERE t = %s", [f"{request.GET['q']}"]
                    )
                """,
            )
            == []
        )

    def test_a_different_queryset_method(self, make_project):
        """``.extra()`` is a raw-SQL surface too, but it is DJI-003's.

        The receiver test cannot make this distinction -- the tracker calls
        this chain a queryset, correctly -- so only the method name does.

        The safe ``.raw()`` below is load-bearing: without it the file has no
        ``.raw(`` in it, the prefilter rejects it before the method name is
        ever compared, and this test would pass whatever the comparison said.
        """
        assert (
            findings(
                make_project,
                """
                from library.models import Book

                def search(request):
                    Book.objects.raw("SELECT 1")
                    return Book.objects.extra(f"SELECT {request.GET['q']}")
                """,
            )
            == []
        )

    def test_the_statement_is_read_from_the_first_position(self, make_project):
        """With params written out, the statement is still argument one."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw(
                    f"SELECT * FROM book WHERE t = '{request.GET['q']}'", []
                )
            """,
        )
        assert len(found) == 1

    def test_every_tainted_part_is_named(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def search(request):
                return Book.objects.raw(
                    f"SELECT * FROM book WHERE t = '{request.GET['q']}' "
                    f"AND a = '{request.GET['a']}'"
                )
            """,
        )
        assert len(found) == 1
        assert found[0].properties["tainted"] == "request.GET['q'], request.GET['a']"


class TestThePrefilter:
    def test_it_admits_everything_the_rule_reports(self, make_project):
        """The prefilter is a cost gate, and only under-admission can hurt.

        Widening it cannot change the output -- ``candidate()`` still decides --
        so a mutation that widens it is invisible, and rightly. Narrowing it is
        the direction that silently loses findings, and nothing downstream
        would show that, so it is asserted here directly: with both stages
        disabled the rule must report exactly what it reports with them on.
        """
        code = """
            from library.models import Book

            def search(request):
                return Book.objects.raw(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
        """
        gated = findings(make_project, code)

        rule_class = next(r for r in all_rules() if r.meta.id == "DJI-002")

        class Ungated(rule_class):  # type: ignore[valid-type, misc]
            WORDS = ("",)

            def check(self, ctx):
                for path in ctx.python_files:
                    root = ctx.scopes(path)
                    if root is not None:
                        yield from self.inspect(ctx, path, root)

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
        ungated = list(Ungated().check(ctx))
        assert len(gated) == len(ungated) == 1
        assert gated[0].location.line == ungated[0].location.line
