"""DJI-001 -- request data interpolated into a cursor statement.

The rule reports *reach*, not shape, and these tests are weighted accordingly.
An interpolated statement is the normal way to write some correct SQL -- a
table name cannot be a query parameter -- so most of the work here is in what
the rule must stay quiet about, and the corpus is the reason: all five composed
``.execute()`` calls across healthchecks, NetBox and pretix are correct.

The cursor tests matter for the same reason. A rule that could not recognise a
cursor would be silent on all five too, and from the outside the two failures
are indistinguishable.
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


def findings(make_project, code: str, rule_id: str = "DJI-001") -> list[Finding]:
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
    rule = next(r for r in all_rules() if r.meta.id == rule_id)
    return list(rule().check(ctx))


class TestWhatItReports:
    def test_an_fstring_carrying_a_query_parameter(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found) == 1
        assert found[0].rule_id == "DJI-001"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.CRITICAL
        assert found[0].properties["composition"] == "f-string"
        assert found[0].properties["tainted"] == "request.GET['q']"

    def test_percent_formatting(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute("SELECT * FROM book WHERE t = '%s'" % request.POST["t"])
            """,
        )
        assert len(found) == 1
        assert found[0].properties["composition"] == "%-formatting"

    def test_concatenation(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute("SELECT * FROM book WHERE t = " + request.GET["q"])
            """,
        )
        assert len(found) == 1
        assert found[0].properties["composition"] == "concatenation"

    def test_str_format(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute("SELECT * FROM book WHERE t = '{}'".format(request.GET["q"]))
            """,
        )
        assert len(found) == 1
        assert found[0].properties["composition"] == ".format()"

    def test_taint_arriving_through_a_local(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                term = request.GET["q"]
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book WHERE t = '{term}'")
            """,
        )
        assert len(found) == 1
        assert found[0].properties["tainted"] == "term"

    def test_executemany(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.executemany(f"DELETE FROM book WHERE t = '{request.GET['q']}'", rows)
            """,
        )
        assert len(found) == 1
        assert "executemany()" in found[0].message

    def test_a_class_based_view_reading_self_request(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            class Search:
                def get(self):
                    with connection.cursor() as cursor:
                        cursor.execute(f"SELECT {self.request.query_params['o']} FROM book")
            """,
        )
        assert len(found) == 1

    def test_only_the_tainted_part_is_named(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection
            from library.models import Book

            def search(request):
                cursor = connection.cursor()
                cursor.execute(
                    f"SELECT * FROM {Book._meta.db_table} WHERE t = '{request.GET['q']}'"
                )
            """,
        )
        assert len(found) == 1
        assert found[0].properties["tainted"] == "request.GET['q']"

    def test_a_statement_passed_by_keyword(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute(sql=f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found) == 1


class TestWhatItDeclines:
    def test_a_parameterised_statement(self, make_project):
        """The value goes beside the statement, never into it."""
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute("SELECT * FROM book WHERE t = %s", [request.GET["q"]])
            """,
        )
        assert found == []

    def test_a_table_name_from_model_metadata(self, make_project):
        """The pretix shape. An identifier cannot be a query parameter."""
        found = findings(
            make_project,
            """
            from django.db import connection
            from library.models import Book

            def count(request):
                cursor = connection.cursor()
                cursor.execute(f"SELECT count(*) FROM {Book._meta.db_table}")
            """,
        )
        assert found == []

    def test_a_local_conditional_over_literals(self, make_project):
        """The NetBox shape."""
        found = findings(
            make_project,
            """
            from django.db import connection

            def mode(request, allow_write):
                value = "READ WRITE" if allow_write else "READ ONLY"
                with connection.cursor() as cursor:
                    cursor.execute(f"SET SESSION CHARACTERISTICS AS TRANSACTION {value};")
            """,
        )
        assert found == []

    def test_a_helper_parameter(self, make_project):
        """UNKNOWN is not TAINTED. The caller decides and we are not looking."""
        found = findings(
            make_project,
            """
            from django.db import connection

            def lookup(term):
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book WHERE t = '{term}'")
            """,
        )
        assert found == []

    def test_a_sanitised_value(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def page(request):
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book LIMIT {int(request.GET['n'])}")
            """,
        )
        assert found == []

    def test_a_middleware_attribute(self, make_project):
        """`request.event` is a model instance middleware attached."""
        found = findings(
            make_project,
            """
            from django.db import connection

            def audit(request):
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book WHERE e = {request.event.pk}")
            """,
        )
        assert found == []

    def test_a_constant_statement(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def all_rows(request):
                cursor = connection.cursor()
                cursor.execute("SELECT * FROM book")
            """,
        )
        assert found == []

    def test_an_fstring_with_no_replacement_field(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def all_rows(request):
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book")
            """,
        )
        assert found == []

    def test_execute_on_something_that_is_not_a_cursor(self, make_project):
        """`.execute` is a method on plenty of things. A workflow is not a
        database."""
        found = findings(
            make_project,
            """
            def run(request):
                workflow.execute(f"step {request.GET['s']}")
            """,
        )
        assert found == []

    def test_a_taskrunner_named_like_nothing_in_particular(self, make_project):
        found = findings(
            make_project,
            """
            def run(request, engine):
                engine.execute(f"step {request.GET['s']}")
            """,
        )
        assert found == []

    def test_an_attribute_receiver_that_is_not_a_cursor(self, make_project):
        """`self.cursor` is a cursor; `self.engine` is not. Only the attribute
        name distinguishes them, so the rule must read it."""
        found = findings(
            make_project,
            """
            class Runner:
                def run(self, request):
                    self.engine.execute(f"step {request.GET['s']}")
            """,
        )
        assert found == []


class TestFindingTheCursor:
    def test_a_context_manager_binding(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                with connection.cursor() as handle:
                    handle.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found) == 1, "a `with` target holds the cursor it was bound to"

    def test_an_assignment_binding_with_an_unconventional_name(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                handle = connection.cursor()
                handle.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found) == 1

    def test_an_inline_factory_call(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                connection.cursor().execute(
                    f"SELECT * FROM book WHERE t = '{request.GET['q']}'"
                )
            """,
        )
        assert len(found) == 1

    def test_a_cursor_attribute(self, make_project):
        found = findings(
            make_project,
            """
            class Repo:
                def search(self, request):
                    self.cursor.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found) == 1

    def test_a_conventional_name_with_no_binding(self, make_project):
        """The fallback: a cursor handed in as a parameter has no binding to
        resolve, so the convention is the only evidence there is."""
        found = findings(
            make_project,
            """
            def search(request, cursor):
                cursor.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found) == 1

    def test_a_name_bound_to_something_that_is_not_a_cursor(self, make_project):
        """The contrast: resolution beats the convention when both apply."""
        found = findings(
            make_project,
            """
            def search(request):
                cursor = build_workflow()
                cursor.execute(f"step {request.GET['s']}")
            """,
        )
        assert found == [], "a resolved non-cursor is not rescued by its name"


class TestHowItSpeaks:
    def test_the_message_names_the_composition_and_the_value(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        message = found[0].message
        assert "an f-string" in message
        assert "request.GET['q']" in message
        assert "query parameter" in message

    def test_the_evidence_quotes_the_statement(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert len(found[0].evidence) == 1
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "SELECT * FROM book" in found[0].evidence[0].content
        assert "views.py:" in found[0].evidence[0].source

    def test_a_directly_written_source_is_certain(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert found[0].confidence is Confidence.CERTAIN

    def test_taint_inferred_through_a_name_is_only_firm(self, make_project):
        """The contrast. Nothing was inferred above; something was here."""
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                term = request.GET["q"]
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book WHERE t = '{term}'")
            """,
        )
        assert found[0].confidence is Confidence.FIRM

    def test_the_location_points_at_the_call(self, make_project):
        found = findings(
            make_project,
            """
            from django.db import connection

            def search(request):
                cursor = connection.cursor()
                cursor.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
            """,
        )
        assert found[0].location.file.endswith("views.py")
        assert found[0].location.line == 5


class TestWithNothingToReasonFrom:
    def test_a_project_with_no_sql_at_all(self, make_project):
        assert findings(make_project, "x = 1\n") == []

    def test_a_file_that_does_not_parse(self, make_project):
        """The prefilter reads source before parsing, so a syntax error must
        not become a crash."""
        assert findings(make_project, "def f(:\n    cursor.execute(x)\n") == []

    def test_execute_with_no_arguments(self, make_project):
        """A TypeError at runtime, but it parses, and indexing args[0] would
        crash the run."""
        assert (
            findings(
                make_project,
                """
                from django.db import connection

                def go(request):
                    cursor = connection.cursor()
                    cursor.execute()
                """,
            )
            == []
        )

    def test_a_bare_execute_call(self, make_project):
        """No receiver at all, so there is no cursor to identify."""
        assert (
            findings(
                make_project,
                """
                def go(request):
                    execute(f"SELECT {request.GET['q']}")
                """,
            )
            == []
        )

    def test_the_same_call_is_reported_once(self, make_project):
        """Nested scopes each walk their own statements; a call that belonged
        to both would be reported twice."""
        found = findings(
            make_project,
            """
            from django.db import connection

            def outer(request):
                def inner():
                    cursor = connection.cursor()
                    cursor.execute(f"SELECT * FROM book WHERE t = '{request.GET['q']}'")
                return inner
            """,
        )
        assert len(found) == 1
