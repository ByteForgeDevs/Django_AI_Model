"""DJP-004 -- a query issued once per iteration.

The rule's difficulty is not finding queries in loops; it is deciding which of
them are one round trip per row and which are one round trip total. A chain
built in a loop and never evaluated costs nothing. A queryset built above the
loop and merely read inside it costs one. A name bound to a query result and
then read three times is still one. These tests hold those apart, because the
first measurement of this rule returned 539 findings and most of them were the
same query counted again under a different name.
"""

from __future__ import annotations

import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Finding, Severity
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

class Author(models.Model):
    name = models.CharField(max_length=100)

class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
"""

URLS = """
urlpatterns = []
"""


def findings(make_project, code: str, rule_id: str = "DJP-004") -> list[Finding]:
    ctx: ProjectContext = make_project(
        {
            "manage.py": "import os\n",
            "library/__init__.py": "",
            "library/settings.py": SETTINGS,
            "library/urls.py": URLS,
            "library/models.py": MODELS,
            "library/service.py": textwrap.dedent(code).lstrip(),
        }
    )
    rule = next(r for r in all_rules() if r.meta.id == rule_id)
    return list(rule().check(ctx))


class TestWhatCountsAsAQuery:
    def test_a_manager_call_evaluated_in_a_loop_is_reported(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Author, Book

            def run():
                for book in Book.objects.all():
                    author = Author.objects.get(pk=book.author_id)
                    print(author)
            """,
        )
        assert len(found) == 1
        assert found[0].properties["step"] == "get"
        assert found[0].properties["model"] == "library.Author"

    def test_a_lazy_chain_is_not_reported(self, make_project):
        """`filter()` returns a queryset. Nothing has gone to the database yet."""
        found = findings(
            make_project,
            """
            from library.models import Author, Book

            def run():
                out = []
                for book in Book.objects.all():
                    out.append(Author.objects.filter(pk=book.author_id))
                return out
            """,
        )
        assert found == []

    def test_a_queryset_built_before_the_loop_is_not_reported(self, make_project):
        """The round trip happens once, above the loop. Reading it inside is free."""
        found = findings(
            make_project,
            """
            from library.models import Author, Book

            def run():
                total = Author.objects.count()
                for book in Book.objects.all():
                    print(book, total)
            """,
        )
        assert found == []

    def test_the_same_query_read_three_times_is_one_finding(self, make_project):
        """Dataflow classifies every later read of the name; only the call ran."""
        found = findings(
            make_project,
            """
            from library.models import Author, Book

            def run():
                for book in Book.objects.all():
                    author = Author.objects.get(pk=book.author_id)
                    print(author)
                    print(author)
                    return author
            """,
        )
        assert len(found) == 1

    def test_two_distinct_queries_are_two_findings(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Author, Book

            def run():
                for pk in [1, 2, 3]:
                    Author.objects.get(pk=pk)
                    Book.objects.filter(author_id=pk).count()
            """,
        )
        assert len(found) == 2
        assert sorted(f.properties["step"] for f in found) == ["count", "get"]


class TestWhatIsLeftToOtherRules:
    def test_a_write_is_left_to_djp_007(self, make_project):
        """`bulk_create` skips signals and may leave primary keys unset, so the
        fix is not a drop-in and gets its own rule."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def run(rows):
                for row in rows:
                    Book.objects.create(title=row)
            """,
        )
        assert found == []

    def test_a_chunked_bulk_write_is_not_reported(self, make_project):
        """Batching in a loop is the recommended pattern, not the defect."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def run(chunks):
                for chunk in chunks:
                    Book.objects.bulk_create(chunk)
            """,
        )
        assert found == []

    def test_a_related_read_is_left_to_djp_002(self, make_project):
        """The family divides by origin: a walk off a row in hand is DJP-002's,
        which can name the `prefetch_related` that fixes it. Asserting DJP-004
        is silent is not enough -- that would also pass if nothing saw the code
        at all -- so the same project is checked against DJP-002."""
        code = """
            from library.models import Author

            def run():
                for author in Author.objects.all():
                    author.books.count()
        """
        assert findings(make_project, code) == []
        assert len(findings(make_project, code, rule_id="DJP-002")) == 1


class TestWhichLoopOwnsIt:
    def test_a_nested_loop_is_attributed_to_the_inner_one(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Author, Book

            def run():
                for book in Book.objects.all():
                    for pk in [1, 2]:
                        Author.objects.get(pk=pk)
            """,
        )
        assert len(found) == 1
        assert found[0].properties["depth"] == "2"
        assert found[0].severity is Severity.CRITICAL, "a query per row per row is worse"

    def test_a_comprehension_counts_as_a_loop(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Author

            def run(ids):
                return [Author.objects.get(pk=pk) for pk in ids]
            """,
        )
        assert len(found) == 1

    def test_a_query_outside_any_loop_is_not_reported(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Author

            def run():
                return Author.objects.get(pk=1)
            """,
        )
        assert found == []


class TestHowSureItIs:
    def test_a_single_loop_stays_high(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Author

            def run(ids):
                for pk in ids:
                    Author.objects.get(pk=pk)
            """,
        )
        assert [f.severity for f in found] == [Severity.HIGH]

    def test_a_resolved_model_is_firm(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Author

            def run(ids):
                for pk in ids:
                    Author.objects.get(pk=pk)
            """,
        )
        assert [f.confidence for f in found] == [Confidence.FIRM]

    def test_an_unresolvable_root_is_not_reported(self, make_project):
        """Without a model the finding could not say what to fetch instead."""
        found = findings(
            make_project,
            """
            def run(self, ids):
                for pk in ids:
                    self.get_queryset().get(pk=pk)
            """,
        )
        assert found == []


class TestOnAProjectWithNothingToFind:
    def test_a_project_with_no_loops_yields_nothing(self, make_project):
        """Risk 13: a detector that finds nothing everywhere looks identical to
        a detector that is switched off, so the zero is asserted deliberately."""
        found = findings(
            make_project,
            """
            from library.models import Author

            def run():
                return Author.objects.all()
            """,
        )
        assert found == []
