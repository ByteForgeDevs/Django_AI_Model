"""DJP-005 -- `len(queryset)` where only the number of rows is wanted.

The rule's whole difficulty is that `len(qs)` is usually right. Code that counts
rows and then reads them should call it: one query beats `.count()` plus an
iteration. So these tests spend most of their effort on what the rule must stay
quiet about, and the reported cases are the two where the rows provably cannot
be read again.
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


def findings(make_project, code: str, rule_id: str = "DJP-005") -> list[Finding]:
    ctx: ProjectContext = make_project(
        {
            "manage.py": "import os\n",
            "library/__init__.py": "",
            "library/settings.py": SETTINGS,
            "library/urls.py": "urlpatterns = []\n",
            "library/models.py": MODELS,
            "library/service.py": textwrap.dedent(code).lstrip(),
        }
    )
    rule = next(r for r in all_rules() if r.meta.id == rule_id)
    return list(rule().check(ctx))


class TestTheReportedShapes:
    def test_an_inline_queryset(self, make_project):
        """Nothing holds the rows, so nothing can read them."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                return len(Book.objects.filter(live=True))
            """,
        )
        assert len(found) == 1
        assert found[0].properties["model"] == "library.Book"
        assert found[0].properties["suggested"] == ".count()"
        assert found[0].properties["binding"] == ""

    def test_a_name_read_only_by_the_len(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                books = Book.objects.all()
                return len(books)
            """,
        )
        assert len(found) == 1
        assert found[0].properties["binding"] == "books"

    def test_severity_and_confidence(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                return len(Book.objects.all())
            """,
        )
        assert found[0].severity is Severity.MEDIUM
        assert found[0].confidence is Confidence.FIRM

    def test_the_message_names_the_call_and_the_fix(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                return len(Book.objects.all())
            """,
        )
        assert "len(Book.objects.all())" in found[0].message
        assert "`.count()`" in found[0].message


class TestWhenTheRowsAreUsed:
    """The idiom this rule must not report."""

    def test_a_name_iterated_after_counting(self, make_project):
        """`len()` populates the cache the loop then reads. Two uses, one query
        -- swapping in `.count()` would make this *slower*."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def report():
                books = Book.objects.all()
                print(len(books))
                for book in books:
                    print(book.title)
            """,
        )
        assert found == []

    def test_a_name_returned_as_well(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def report():
                books = Book.objects.all()
                return len(books), books
            """,
        )
        assert found == []

    def test_a_name_indexed_after_counting(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def report():
                books = Book.objects.all()
                if len(books):
                    return books[0]
                return None
            """,
        )
        assert found == []


class TestWhatIsExcluded:
    def test_a_queryset_whose_model_we_cannot_name_is_left_alone(self, make_project):
        """`self.get_queryset()` tracks with origin `self` and model `None`.

        This is what the `FRESH` allowlist really excludes. A finding here
        could not name the model to count instead, and the override may not
        return a queryset at all.
        """
        found = findings(
            make_project,
            """
            from rest_framework import viewsets

            class BookViewSet(viewsets.ModelViewSet):
                def total(self):
                    return len(self.get_queryset())
            """,
        )
        assert found == []

    def test_a_default_manager_is_reported(self, make_project):
        """The contrast to the case above: also not `objects`, but its model
        *is* known, so `FRESH` admits it."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                return len(Book._default_manager.all())
            """,
        )
        assert len(found) == 1
        assert found[0].properties["model"] == "library.Book"

    def test_a_related_accessor_is_left_alone(self, make_project):
        """Measured in `scripts/prefetch_cache_probe.py`: under
        `prefetch_related`, `len(x.books.all())` costs the same 2 queries as
        `.count()`, so there is no improvement to advise.

        The queryset tracker does not resolve a related accessor to a model
        today, so this is currently silent for that reason as well. The
        exclusion is kept explicit so the rule does not start reporting them
        the day the tracker learns to.
        """
        found = findings(
            make_project,
            """
            from library.models import Author

            def total():
                for author in Author.objects.all():
                    print(len(author.books.all()))
            """,
        )
        assert found == []

    def test_a_sliced_queryset_is_left_alone(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                return len(Book.objects.all()[:10])
            """,
        )
        assert found == []

    def test_a_terminal_chain_is_not_a_queryset(self, make_project):
        """`values_list(...).first()` is a row, and `len()` of it is a length
        of a tuple, not a table scan."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                return len(Book.objects.values_list("id").first())
            """,
        )
        assert found == []

    def test_len_with_the_wrong_number_of_arguments(self, make_project):
        """`len()` is a `TypeError` at runtime but parses fine, and reading
        `args[0]` without checking would crash the whole audit on it.

        The module has to contain a tracked queryset as well, or `counts()`
        returns before the walk and this passes without exercising anything.
        """
        found = findings(
            make_project,
            """
            from library.models import Book

            def broken():
                return len(), len(Book.objects.count(), 2)
            """,
        )
        assert found == []

    def test_len_of_something_that_is_not_a_queryset(self, make_project):
        found = findings(
            make_project,
            """
            def total(rows):
                return len(rows) + len("abc") + len([1, 2, 3])
            """,
        )
        assert found == []

    def test_another_one_argument_call_over_a_discarded_queryset(self, make_project):
        """Only `len` is a count. `list(qs)` and `bool(qs)` have the same shape
        -- one positional argument holding a discarded fresh queryset -- and
        every guard downstream of the name check passes for them, so the name
        check is the only thing standing between this rule and telling someone
        that `list(books)` should be `.count()`.

        The `len` call is here as the contrast: without it the module would be
        silent for want of anything to report, and the test would pass whether
        or not the name were checked.
        """
        found = findings(
            make_project,
            """
            from library.models import Book

            def shapes():
                return list(Book.objects.all()), bool(Book.objects.all())

            def counted():
                return len(Book.objects.all())
            """,
        )
        assert [f.location.line for f in found] == [7]

    def test_a_project_with_no_len_at_all(self, make_project):
        """The empty-input control: silence here must mean nothing was found,
        not that the walk never ran."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                return Book.objects.count()
            """,
        )
        assert found == []


class TestExistsRefinement:
    """`len(qs) > 0` wants `.exists()`, which stops at the first row."""

    def test_compared_greater_than_zero(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def any_books():
                return len(Book.objects.all()) > 0
            """,
        )
        assert len(found) == 1
        assert found[0].properties["suggested"] == ".exists()"
        assert "`.exists()`" in found[0].message

    def test_compared_equal_to_zero(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def empty():
                return len(Book.objects.all()) == 0
            """,
        )
        assert found[0].properties["suggested"] == ".exists()"

    def test_negated(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def empty():
                return not len(Book.objects.all())
            """,
        )
        assert found[0].properties["suggested"] == ".exists()"

    def test_compared_against_a_real_bound_still_wants_count(self, make_project):
        """`> 10` is a genuine count, not an emptiness test."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def many():
                return len(Book.objects.all()) > 10
            """,
        )
        assert found[0].properties["suggested"] == ".count()"

    def test_assigned_rather_than_compared_wants_count(self, make_project):
        found = findings(
            make_project,
            """
            from library.models import Book

            def total():
                n = len(Book.objects.all())
                return n
            """,
        )
        assert found[0].properties["suggested"] == ".count()"
