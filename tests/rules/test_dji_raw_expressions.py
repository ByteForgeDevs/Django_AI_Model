"""DJI-004 -- request data interpolated into a ``RawSQL`` or ``Func``.

The corpus holds 20 of these calls across 3,091 files and exactly two of them
compose their SQL, so the fixtures carry most of the weight. They are written
around the two measurements that shaped the rule: that ``template=`` is a
decoy keyword belonging mostly to Django's email machinery, and that a
same-file import is enough to say which ``Func`` a name means.

``Func``'s SQL keywords are keyword-only, so there is deliberately no
positional test for them -- and ``RawSQL``'s SQL *is* positional, so there is
one for it.
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
    rule = next(r for r in all_rules() if r.meta.id == "DJI-004")
    return list(rule().check(ctx))


class TestRawSql:
    def test_the_statement_written_positionally(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models.expressions import RawSQL
            from library.models import Book

            def search(request):
                return Book.objects.annotate(
                    hit=RawSQL(f"title = '{request.GET['q']}'", [])
                )
            """,
        )
        assert len(found) == 1
        assert found[0].rule_id == "DJI-004"
        assert found[0].family is Family.DJI
        assert found[0].severity is Severity.CRITICAL
        assert found[0].properties["expression"] == "RawSQL"
        assert found[0].properties["slot"] == "sql"

    def test_the_statement_written_by_name(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models.expressions import RawSQL
            from library.models import Book

            def search(request):
                return Book.objects.annotate(
                    hit=RawSQL(sql=f"title = '{request.GET['q']}'", params=[])
                )
            """,
        )
        assert len(found) == 1
        assert found[0].properties["slot"] == "sql"

    def test_imported_from_django_db_models(self, make_project):
        """Both module paths bind the same class."""
        found = findings(
            make_project,
            """
            from django.db.models import RawSQL
            from library.models import Book

            def search(request):
                return Book.objects.annotate(hit=RawSQL(f"x = '{request.GET['q']}'", []))
            """,
        )
        assert len(found) == 1

    def test_params_is_the_fix(self, make_project):
        assert (
            findings(
                make_project,
                """
                from django.db.models.expressions import RawSQL
                from library.models import Book

                def search(request):
                    return Book.objects.annotate(
                        hit=RawSQL("title = %s", [request.GET["q"]])
                    )
                """,
            )
            == []
        )

    def test_a_statement_written_whole(self, make_project):
        assert (
            findings(
                make_project,
                """
                from django.db.models.expressions import RawSQL
                from library.models import Book

                def search(request):
                    return Book.objects.annotate(hit=RawSQL("title = 'x'", []))
                """,
            )
            == []
        )

    def test_a_call_with_no_arguments(self, make_project):
        assert (
            findings(
                make_project,
                """
                from django.db.models.expressions import RawSQL
                from library.models import Book

                def search(request):
                    return Book.objects.annotate(hit=RawSQL())
                """,
            )
            == []
        )


class TestFunc:
    def test_the_template(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models import Func
            from library.models import Book

            def search(request):
                return Book.objects.annotate(
                    hit=Func("title", template=f"upper(%(expressions)s) = '{request.GET['q']}'")
                )
            """,
        )
        assert len(found) == 1
        assert found[0].properties["expression"] == "Func"
        assert found[0].properties["slot"] == "template"

    def test_the_function_name(self, make_project):
        """`function=` is the commonest Func keyword by far: 11 of 11 in the corpus."""
        found = findings(
            make_project,
            """
            from django.db.models import Func
            from library.models import Book

            def search(request):
                return Book.objects.annotate(hit=Func("title", function=f"{request.GET['f']}"))
            """,
        )
        assert len(found) == 1
        assert found[0].properties["slot"] == "function"

    def test_the_argument_joiner(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models import Func
            from library.models import Book

            def search(request):
                return Book.objects.annotate(
                    hit=Func("a", "b", function="concat", arg_joiner=f"{request.GET['j']}")
                )
            """,
        )
        assert len(found) == 1
        assert found[0].properties["slot"] == "arg_joiner"

    def test_two_slots_are_two_findings(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models import Func
            from library.models import Book

            def search(request):
                return Book.objects.annotate(
                    hit=Func(
                        "title",
                        function=f"{request.GET['f']}",
                        arg_joiner=f"{request.GET['j']}",
                    )
                )
            """,
        )
        assert len(found) == 2
        assert {f.properties["slot"] for f in found} == {"function", "arg_joiner"}

    def test_the_expressions_are_not_read(self, make_project):
        """Positional arguments are compiled and parameterised, never templates."""
        assert (
            findings(
                make_project,
                """
                from django.db.models import Func, Value
                from library.models import Book

                def search(request):
                    return Book.objects.annotate(
                        hit=Func(Value(request.GET["q"]), function="upper")
                    )
                """,
            )
            == []
        )

    def test_output_field_is_not_sql(self, make_project):
        """Composed deliberately, so a mutant that widens the slot list fails.

        An uncomposed value here would leave nothing for a widened rule to
        report, and the test would pass whether or not `output_field` was
        treated as SQL -- which is exactly how it first passed.
        """
        assert (
            findings(
                make_project,
                """
                from django.db.models import Func
                from library.models import Book

                def search(request):
                    return Book.objects.annotate(
                        hit=Func("title", function="upper", output_field=f"{request.GET['t']}")
                    )
                """,
            )
            == []
        )


class TestWhatItDeclines:
    def test_a_template_keyword_on_something_that_is_not_django(self, make_project):
        """The decoy: 42 of the corpus's 44 `template=` uses are email machinery.

        The `Func` import keeps the file past the prefilter, so the callable
        test is the only thing standing between `send_mail` and a finding.
        """
        assert (
            findings(
                make_project,
                """
                from django.db.models import Func
                from library.models import Book

                def notify(request):
                    Book.objects.annotate(hit=Func("title", function="upper"))
                    return send_mail(template=f"welcome-{request.GET['lang']}.html")
                """,
            )
            == []
        )

    def test_a_local_class_of_the_same_name(self, make_project):
        """Nothing imported it from Django, so nothing says it is Django's."""
        assert (
            findings(
                make_project,
                """
                class Func:
                    def __init__(self, *args, **kwargs): ...

                def search(request):
                    return Func("title", template=f"x = '{request.GET['q']}'")
                """,
            )
            == []
        )

    def test_an_import_from_somewhere_else(self, make_project):
        assert (
            findings(
                make_project,
                """
                from library.helpers import RawSQL

                def search(request):
                    return RawSQL(f"title = '{request.GET['q']}'", [])
                """,
            )
            == []
        )

    def test_an_alias_is_not_followed(self, make_project):
        """An `as` alias binds a name `expression_calls` will never match.

        Honouring it here would be worse than ignoring it: `RawSQL as Func`
        would bind `Func`, and the rule would then report a RawSQL call while
        calling it a Func. Declining is the honest answer, and no file in the
        3,091 of the benchmark corpora aliases either name.
        """
        assert (
            findings(
                make_project,
                """
                from django.db.models import RawSQL as Func

                def search(request):
                    return Func(f"title = '{request.GET['q']}'", [])
                """,
            )
            == []
        )

    def test_a_value_this_analysis_cannot_place(self, make_project):
        assert (
            findings(
                make_project,
                """
                from django.db.models.expressions import RawSQL

                def helper(term):
                    return RawSQL(f"title = '{term}'", [])
                """,
            )
            == []
        )


class TestHowItSpeaks:
    def test_the_message_names_the_raw_sql_door(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models.expressions import RawSQL
            from library.models import Book

            def search(request):
                return Book.objects.annotate(hit=RawSQL(f"x = '{request.GET['q']}'", []))
            """,
        )
        assert "the SQL passed to RawSQL()" in found[0].message
        assert "parameter" in found[0].message

    def test_the_message_names_the_func_slot(self, make_project):
        """The contrast, so a hardcoded slot phrase cannot pass."""
        found = findings(
            make_project,
            """
            from django.db.models import Func
            from library.models import Book

            def search(request):
                return Book.objects.annotate(hit=Func("t", function=f"{request.GET['f']}"))
            """,
        )
        assert "the function name in Func()" in found[0].message

    def test_direct_interpolation_is_certain(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models.expressions import RawSQL
            from library.models import Book

            def search(request):
                return Book.objects.annotate(hit=RawSQL(f"x = '{request.GET['q']}'", []))
            """,
        )
        assert found[0].confidence is Confidence.CERTAIN

    def test_taint_through_a_name_is_firm(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models.expressions import RawSQL
            from library.models import Book

            def search(request):
                term = request.GET["q"]
                return Book.objects.annotate(hit=RawSQL(f"x = '{term}'", []))
            """,
        )
        assert found[0].confidence is Confidence.FIRM

    def test_a_statement_built_into_a_local_is_named(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models.expressions import RawSQL
            from library.models import Book

            def search(request):
                sql = f"title = '{request.GET['q']}'"
                return Book.objects.annotate(hit=RawSQL(sql, []))
            """,
        )
        assert len(found) == 1
        assert "sql" in found[0].message

    def test_the_evidence_is_the_composed_expression(self, make_project):
        found = findings(
            make_project,
            """
            from django.db.models.expressions import RawSQL
            from library.models import Book

            def search(request):
                return Book.objects.annotate(hit=RawSQL(f"x = '{request.GET['q']}'", []))
            """,
        )
        assert len(found[0].evidence) == 1
        assert found[0].evidence[0].kind is EvidenceKind.AST
        assert "x = " in found[0].evidence[0].content


class TestWithNothingToReasonFrom:
    def test_a_project_with_neither_callable(self, make_project):
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

    def test_an_import_that_is_never_called(self, make_project):
        assert (
            findings(
                make_project,
                """
                from django.db.models import Func

                def search(request):
                    return request.GET["q"]
                """,
            )
            == []
        )
