"""DJP-001 -- a join that was available and was not taken.

The rule's whole difficulty is telling three identical-looking attribute reads
apart: one that crosses a relation and costs a query, one that reads a column
already in the row, and one that reads the foreign key's own integer column and
costs nothing at all. `b.author`, `b.title` and `b.author_id` differ only by
what the model graph says about them.
"""

from __future__ import annotations

import textwrap

from djaudit.context import ProjectContext
from djaudit.graph.nodes import ModelGraph
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

class Publisher(models.Model):
    name = models.CharField(max_length=100)

class Author(models.Model):
    name = models.CharField(max_length=100)
    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)

class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    editor = models.ForeignKey(Author, null=True, on_delete=models.SET_NULL,
                               related_name="edited")
    tags = models.ManyToManyField("Tag")

class Tag(models.Model):
    label = models.CharField(max_length=50)
"""


def findings(make_project, body: str, **extra: str) -> list[Finding]:
    ctx: ProjectContext = make_project(
        {
            "manage.py": "import os\n",
            "library/__init__.py": "",
            "library/settings.py": SETTINGS,
            "library/urls.py": "urlpatterns = []\n",
            "library/models.py": MODELS,
            "library/views.py": textwrap.dedent(body).lstrip(),
            **extra,
        }
    )
    rule = next(r for r in all_rules() if r.meta.id == "DJP-001")
    return list(rule().check(ctx))


def paths(found: list[Finding]) -> list[str]:
    return sorted(f.properties["path"] for f in found)


class TestTheCaseItExistsFor:
    def test_a_forward_relation_read_in_a_loop(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.author.name)
            """,
        )
        assert paths(found) == ["author"]
        assert found[0].properties["model"] == "library.Book"

    def test_it_names_the_whole_path(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.author.publisher.name)
            """,
        )
        assert paths(found) == ["author__publisher"]

    def test_a_comprehension_counts(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                return [b.author.name for b in Book.objects.all()]
            """,
        )
        assert paths(found) == ["author"]

    def test_a_comprehension_filter_counts(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                return [b for b in Book.objects.all() if b.author.name]
            """,
        )
        assert paths(found) == ["author"]

    def test_a_nested_loop_is_more_severe(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view(rows):
                for r in rows:
                    for b in Book.objects.all():
                        print(b.author.name)
            """,
        )
        assert [f.severity for f in found] == [Severity.HIGH]

    def test_two_different_paths_are_two_findings(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.author.name, b.editor.name)
            """,
        )
        assert paths(found) == ["author", "editor"]


class TestWhatItMustNotSay:
    def test_select_related_silences_it(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.select_related("author"):
                    print(b.author.name)
            """,
        )

    def test_a_longer_select_related_covers_the_prefix(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.select_related("author__publisher"):
                    print(b.author.name)
            """,
        )

    def test_bare_select_related_covers_everything_forward(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.select_related():
                    print(b.author.publisher.name)
            """,
        )

    def test_a_plain_column_is_not_a_relation(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.title)
            """,
        )

    def test_the_foreign_key_id_column_costs_nothing(self, make_project) -> None:
        """`b.author_id` is an integer already on the row. Reporting it would be
        the rule telling a developer to fix the thing they did to avoid it."""
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.author_id)
            """,
        )

    def test_a_many_to_many_is_not_select_related(self, make_project) -> None:
        """`select_related` cannot join a M2M. Suggesting it would be wrong
        advice, and Django raises when you try."""
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.tags.all())
            """,
        )

    def test_an_unknown_model_is_not_guessed(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            def view(things):
                for t in things:
                    print(t.author.name)
            """,
        )

    def test_a_rebound_element_is_not_the_row(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    b = something_else()
                    print(b.author.name)
            """,
        )

    def test_an_attribute_on_another_name_is_ignored(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view(other):
                for b in Book.objects.all():
                    print(other.author.name)
            """,
        )

    def test_values_rows_have_no_attributes(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.values("title"):
                    print(b.author)
            """,
        )

    def test_a_single_instance_is_not_a_loop_of_rows(self, make_project) -> None:
        assert not findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.get(pk=1).related:
                    print(b.author.name)
            """,
        )


class TestHowOftenItSpeaks:
    def test_one_path_read_twice_is_one_finding(self, make_project) -> None:
        """Django caches the fetched relation on the instance, so the second
        read costs nothing. Two findings would double the apparent cost."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.author.name)
                    print(b.author.publisher_id)
            """,
        )
        assert paths(found) == ["author"]

    def test_the_longest_chain_wins(self, make_project) -> None:
        """`b.author.publisher.name` contains `b.author`. Charging both would
        report one query twice."""
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.author.publisher.name)
            """,
        )
        assert len(found) == 1


class TestConfidence:
    def test_an_unreadable_chain_lowers_confidence(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view(names):
                for b in Book.objects.select_related(*names):
                    print(b.author.name)
            """,
        )
        assert [f.confidence for f in found] == [Confidence.TENTATIVE]

    def test_a_plain_chain_is_firm(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.all():
                    print(b.author.name)
            """,
        )
        assert [f.confidence for f in found] == [Confidence.FIRM]


class TestEvidence:
    def test_it_shows_what_was_selected(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def view():
                for b in Book.objects.select_related("editor"):
                    print(b.author.name)
            """,
        )
        content = found[0].evidence[0].content
        assert "select_related: editor" in content
        assert "library.Book.author (ForeignKey)" in content


class TestWithNothingToReasonFrom:
    """The empty-input control.

    Every guard above proves the rule stays quiet about a *particular* shape.
    None of them prove it stays quiet when it knows nothing at all, and a rule
    that still speaks with its evidence removed was never reading the evidence.
    Run against the three benchmark projects with the graph emptied, DJP-001
    goes from 32 findings to none.
    """

    def test_an_empty_model_graph_says_nothing(self, make_project) -> None:
        ctx: ProjectContext = make_project(
            {
                "manage.py": "import os\n",
                "library/__init__.py": "",
                "library/settings.py": SETTINGS,
                "library/urls.py": "urlpatterns = []\n",
                "library/models.py": MODELS,
                "library/views.py": textwrap.dedent(
                    """
                    from library.models import Book

                    def listing():
                        for b in Book.objects.all():
                            print(b.author.name)
                    """
                ).lstrip(),
            }
        )
        rule = next(r for r in all_rules() if r.meta.id == "DJP-001")
        assert len(list(rule().check(ctx))) == 1

        ctx._model_graph = ModelGraph()
        assert list(rule().check(ctx)) == []
