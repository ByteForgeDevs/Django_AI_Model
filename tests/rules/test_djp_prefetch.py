"""DJP-002 -- many rows fetched one row at a time.

The rule differs from DJP-001 in two ways that matter more than they look.
The attribute is not the query: `author.book_set` builds a manager and costs
nothing, and only the `.all()` after it talks to the database. And the fix is
not interchangeable: `select_related` cannot reach a set of rows, *including*
its bare no-argument form, so a rule that reused DJP-001's coverage test would
go quiet on exactly the queryset that needed it most.
"""

from __future__ import annotations

import textwrap

from djaudit.context import ProjectContext
from djaudit.graph.nodes import ModelGraph
from djaudit.models import Confidence, Finding, Severity
from djaudit.registry import all_rules
from djaudit.rules.performance import many_hop

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

class Passport(models.Model):
    author = models.OneToOneField(Author, on_delete=models.CASCADE)
    number = models.CharField(max_length=20)

class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    editor = models.ForeignKey(Author, null=True, on_delete=models.SET_NULL,
                               related_name="edited")
    censor = models.ForeignKey(Author, null=True, on_delete=models.SET_NULL,
                               related_name="+")
    tags = models.ManyToManyField("Tag")

class Tag(models.Model):
    label = models.CharField(max_length=50)
"""


def findings(make_project, body: str) -> list[Finding]:
    ctx: ProjectContext = make_project(
        {
            "manage.py": "import os\n",
            "library/__init__.py": "",
            "library/settings.py": SETTINGS,
            "library/urls.py": "urlpatterns = []\n",
            "library/models.py": MODELS,
            "library/views.py": textwrap.dedent(body).lstrip(),
        }
    )
    rule = next(r for r in all_rules() if r.meta.id == "DJP-002")
    return list(rule().check(ctx))


def paths(found: list[Finding]) -> list[str]:
    return sorted(f.properties["path"] for f in found)


class TestTheCaseItExistsFor:
    def test_a_forward_many_to_many(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing():
                for b in Book.objects.all():
                    print(b.tags.all())
            """,
        )
        assert paths(found) == ["tags"]

    def test_a_reverse_foreign_key_by_default_accessor(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    print(a.book_set.all())
            """,
        )
        assert paths(found) == ["book_set"]

    def test_a_reverse_foreign_key_by_related_name(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    print(a.edited.count())
            """,
        )
        assert paths(found) == ["edited"]

    def test_a_reverse_many_to_many(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Tag

            def listing():
                for t in Tag.objects.all():
                    print(t.book_set.exists())
            """,
        )
        assert paths(found) == ["book_set"]

    def test_it_names_the_whole_path_through_a_forward_hop(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing():
                for b in Book.objects.select_related("author"):
                    print(b.author.book_set.all())
            """,
        )
        assert paths(found) == ["author__book_set"]

    def test_a_comprehension_counts(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                return [a.book_set.count() for a in Author.objects.all()]
            """,
        )
        assert paths(found) == ["book_set"]

    def test_a_nested_loop_is_more_severe(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author, Publisher

            def listing():
                for p in Publisher.objects.all():
                    for a in Author.objects.all():
                        print(a.book_set.all())
            """,
        )
        assert [f.severity for f in found] == [Severity.HIGH]


class TestWhatItMustNotSay:
    def test_prefetch_related_silences_it(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing():
                for b in Book.objects.prefetch_related("tags"):
                    print(b.tags.all())
            """,
        )
        assert found == []

    def test_a_longer_prefetch_covers_the_prefix(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing():
                for b in Book.objects.prefetch_related("author__book_set"):
                    print(b.author.book_set.all())
            """,
        )
        assert found == []

    def test_a_bare_select_related_does_not_reach_many_rows(self, make_project) -> None:
        """The distinction the rule exists to keep.

        `select_related()` with no arguments fetches every forward single-valued
        relation, and DJP-001 rightly treats it as covering anything it can
        walk. It cannot join a set of rows into one row, so if this rule reused
        that test it would fall silent on precisely the queryset most likely to
        be looping.
        """
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing():
                for b in Book.objects.select_related():
                    print(b.tags.all())
            """,
        )
        assert paths(found) == ["tags"]

    def test_naming_the_path_in_select_related_does_not_silence_it(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing():
                for b in Book.objects.select_related("tags"):
                    print(b.tags.all())
            """,
        )
        assert paths(found) == ["tags"]

    def test_a_manager_that_is_never_evaluated_is_not_a_query(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing():
                for b in Book.objects.all():
                    manager = b.tags
                    print(manager)
            """,
        )
        assert found == []

    def test_a_forward_foreign_key_belongs_to_djp_001(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book

            def listing():
                for b in Book.objects.all():
                    print(b.author.name)
            """,
        )
        assert found == []

    def test_a_reverse_one_to_one_is_a_single_row(self, make_project) -> None:
        """Written as a *call* on purpose.

        `a.passport.number` never reaches the one-to-one guard, because the
        chain is not called and the rule stops earlier. Removing the guard then
        breaks nothing and the test looks like it is protecting something it
        has never touched -- which is how it was written first.
        """
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    print(a.passport.get_number())
            """,
        )
        assert found == []

    def test_a_suppressed_reverse_accessor_does_not_exist(self, make_project) -> None:
        """`related_name="+"` tells Django to add no reverse attribute at all.

        Naming it in `prefetch_related` is an error rather than a fix, so a
        rule that reported it would be recommending a crash.
        """
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    print(a.book_set_censor.all())
            """,
        )
        assert found == []

    def test_an_unknown_model_is_not_guessed(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from elsewhere.models import Widget

            def listing():
                for w in Widget.objects.all():
                    print(w.part_set.all())
            """,
        )
        assert found == []

    def test_a_rebound_element_is_not_the_row(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    a = a.publisher
                    print(a.author_set.all())
            """,
        )
        assert found == []

    def test_an_ordinary_method_call_is_not_a_relation(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    print(a.get_absolute_url())
            """,
        )
        assert found == []


class TestWhichRelationsAreMultiValued:
    """`many_hop` asked directly, because `inspect` cannot ask it everything.

    `traversal` consumes every forward foreign key and one-to-one before
    `many_hop` is reached, so through the rule the forward single-valued case
    can never arrive -- and a defect that dropped the check produced no test
    failures at all. The helper's contract is wider than the one path the rule
    drives it down, so it is pinned here rather than left to look load-bearing.
    """

    def graph(self, make_project) -> ModelGraph:
        ctx: ProjectContext = make_project(
            {
                "manage.py": "import os\n",
                "library/__init__.py": "",
                "library/settings.py": SETTINGS,
                "library/urls.py": "urlpatterns = []\n",
                "library/models.py": MODELS,
            }
        )
        return ctx.model_graph

    def test_a_forward_foreign_key_is_not_multi_valued(self, make_project) -> None:
        assert many_hop(self.graph(make_project), "library.Book", "author") is None

    def test_a_forward_many_to_many_is(self, make_project) -> None:
        hop = many_hop(self.graph(make_project), "library.Book", "tags")
        assert hop is not None and not hop.reverse

    def test_a_reverse_foreign_key_is(self, make_project) -> None:
        hop = many_hop(self.graph(make_project), "library.Author", "book_set")
        assert hop is not None and hop.reverse

    def test_a_reverse_one_to_one_is_not(self, make_project) -> None:
        assert many_hop(self.graph(make_project), "library.Author", "passport") is None

    def test_a_suppressed_accessor_has_no_name_to_match(self, make_project) -> None:
        """`related_name="+"` means Django adds nothing to the target.

        The censor edge therefore has no accessor at all, and no attribute
        written in a loop can match it. Asserted on the graph rather than only
        through the rule, where a made-up name would be rejected anyway and the
        test would pass without the suppression mattering.
        """
        graph = self.graph(make_project)
        book = graph.get("library.Book")
        assert book is not None
        censor = next(e for e in book.relations if e.field_name == "censor")
        assert censor.accessor is None
        # And so it never enters the reverse index the rule reads, which is
        # where the suppression actually takes effect.
        incoming = graph.incoming["library.Author"]
        assert {e.field_name for e in incoming} == {"author", "editor"}


class TestHowOftenItSpeaks:
    def test_one_path_evaluated_twice_is_one_finding(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    print(a.book_set.count())
                    print(a.book_set.all())
            """,
        )
        assert paths(found) == ["book_set"]

    def test_two_different_paths_are_two_findings(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    print(a.book_set.all())
                    print(a.edited.all())
            """,
        )
        assert paths(found) == ["book_set", "edited"]


class TestConfidence:
    def test_an_unreadable_chain_lowers_confidence(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing(names):
                for a in Author.objects.filter(*names):
                    print(a.book_set.all())
            """,
        )
        assert [f.confidence for f in found] == [Confidence.TENTATIVE]

    def test_a_plain_chain_is_firm(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.filter(name="x"):
                    print(a.book_set.all())
            """,
        )
        assert [f.confidence for f in found] == [Confidence.FIRM]


class TestEvidence:
    def test_it_shows_which_side_of_the_relation_was_walked(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def listing():
                for a in Author.objects.all():
                    print(a.book_set.all())
            """,
        )
        content = found[0].evidence[0].content
        assert "prefetch_related: nothing" in content
        assert "library.Book.author (ForeignKey, reverse)" in content


class TestWithNothingToReasonFrom:
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
                    from library.models import Author

                    def listing():
                        for a in Author.objects.all():
                            print(a.book_set.all())
                    """
                ).lstrip(),
            }
        )
        rule = next(r for r in all_rules() if r.meta.id == "DJP-002")
        assert len(list(rule().check(ctx))) == 1

        ctx._model_graph = ModelGraph()
        assert list(rule().check(ctx)) == []


class TestWhatAPrefetchCannotFix:
    """Quiet wherever `prefetch_related` is not the fix.

    Every case here really does run one query per row, so none of these loops
    is innocent. But prefetching removes none of those queries: a write has no
    read to serve from cache and invalidates the cache it would have filled,
    and a cloning read starts from an empty cache and queries again. Measured
    in `scripts/prefetch_cache_probe.py`, the cloning forms go from 4 queries
    to 5 once prefetched -- strictly worse. Reporting either kind here would
    attach a remediation that does not work.
    """

    def test_adding_to_a_many_to_many(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book, Tag

            def tag_all(t: Tag):
                for b in Book.objects.all():
                    b.tags.add(t)
            """,
        )
        assert found == []

    def test_removing_from_a_many_to_many(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Book, Tag

            def untag_all(t: Tag):
                for b in Book.objects.all():
                    b.tags.remove(t)
            """,
        )
        assert found == []

    def test_creating_through_a_reverse_manager(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def seed():
                for a in Author.objects.all():
                    a.book_set.create(title="untitled")
            """,
        )
        assert found == []

    def test_a_queryset_update_never_brings_a_row_into_python(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def retitle():
                for a in Author.objects.all():
                    a.book_set.all().update(title="x")
            """,
        )
        assert found == []

    def test_a_queryset_delete_is_the_same(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def purge():
                for a in Author.objects.all():
                    a.book_set.all().delete()
            """,
        )
        assert found == []

    def test_a_read_in_the_same_loop_as_a_write_is_still_reported(self, make_project) -> None:
        """The exclusion is per call, not per loop.

        Suppressing the whole loop body would be the easy version of this
        guard and would lose a genuine finding standing next to the write.
        """
        found = findings(
            make_project,
            """
            from library.models import Book, Tag

            def retag(t: Tag):
                for b in Book.objects.all():
                    b.tags.add(t)
                    print(b.tags.all())
            """,
        )
        assert paths(found) == ["tags"]

    def test_a_read_that_merely_precedes_a_write_is_still_a_read(self, make_project) -> None:
        """`.all()` is only discounted when the write consumes *it*.

        Here the write is applied to a different receiver, so the `.all()`
        genuinely materialises rows and the prefetch would genuinely help.
        """
        found = findings(
            make_project,
            """
            from library.models import Author

            def audit():
                for a in Author.objects.all():
                    for b in a.book_set.all():
                        b.tags.clear()
            """,
        )
        assert paths(found) == ["book_set"]


class TestReadsAPrefetchCannotServe:
    """Cloning the queryset throws the prefetch cache away.

    `scripts/prefetch_cache_probe.py` measures every expression below against
    a real database: each one costs 4 queries plain and 5 prefetched, because
    the clone starts with an empty `_result_cache` and re-queries while the
    prefetch is paid for regardless. Suggesting `prefetch_related` here would
    make the code slower, so these belong to the query-in-a-loop rule.
    """

    def test_values_list_reclones_and_requeries(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def ids():
                out = []
                for a in Author.objects.all():
                    out.extend(a.book_set.values_list("id", flat=True))
            """,
        )
        assert found == []

    def test_all_then_values_list_is_no_better(self, make_project) -> None:
        """The cache is filled and then discarded, which is the worst case."""
        found = findings(
            make_project,
            """
            from library.models import Author

            def ids():
                out = []
                for a in Author.objects.all():
                    out.extend(a.book_set.all().values_list("id", flat=True))
            """,
        )
        assert found == []

    def test_a_filtered_read_needs_a_prefetch_object_not_a_name(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def recent():
                for a in Author.objects.all():
                    print(a.book_set.filter(title="x"))
            """,
        )
        assert found == []

    def test_first_clones_to_order_and_loses_the_cache(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def newest():
                for a in Author.objects.all():
                    print(a.book_set.first())
            """,
        )
        assert found == []

    def test_iterator_bypasses_the_cache_by_design(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from library.models import Author

            def stream():
                for a in Author.objects.all():
                    for b in a.book_set.iterator():
                        print(b)
            """,
        )
        assert found == []

    def test_count_is_served_from_the_cache(self, make_project) -> None:
        """`.count()` reads `len(_result_cache)` when it is populated."""
        found = findings(
            make_project,
            """
            from library.models import Author

            def tally():
                for a in Author.objects.all():
                    print(a.book_set.count())
            """,
        )
        assert paths(found) == ["book_set"]
