"""DJP-003 -- a relation walked once per serialized row.

The rule exists because nothing in a `SerializerMethodField` getter says it
runs more than once. DRF supplies the loop, the serializer supplies the
traversal, and the view supplies the queryset that could have avoided it --
three files, no `for` anywhere. These tests keep those three pieces wired
together, because the rule is only correct when it reads all of them.
"""

from __future__ import annotations

import textwrap

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Finding
from djaudit.registry import all_rules

SETTINGS = """
SECRET_KEY = "x"
DEBUG = False
ALLOWED_HOSTS = ["example.com"]
INSTALLED_APPS = ["library", "rest_framework"]
ROOT_URLCONF = "library.urls"
"""

MODELS = """
from django.db import models

class Publisher(models.Model):
    name = models.CharField(max_length=100)

class Author(models.Model):
    name = models.CharField(max_length=100)
    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)

class Tag(models.Model):
    label = models.CharField(max_length=50)

class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    tags = models.ManyToManyField(Tag)
"""

URLS = """
from rest_framework.routers import DefaultRouter
from library.views import BookViewSet

router = DefaultRouter()
router.register("books", BookViewSet)
urlpatterns = router.urls
"""


def findings(make_project, serializers: str, views: str) -> list[Finding]:
    ctx: ProjectContext = make_project(
        {
            "manage.py": "import os\n",
            "library/__init__.py": "",
            "library/settings.py": SETTINGS,
            "library/urls.py": URLS,
            "library/models.py": MODELS,
            "library/serializers.py": textwrap.dedent(serializers).lstrip(),
            "library/views.py": textwrap.dedent(views).lstrip(),
        }
    )
    rule = next(r for r in all_rules() if r.meta.id == "DJP-003")
    return list(rule().check(ctx))


def paths(found: list[Finding]) -> list[str]:
    return sorted(f.properties["path"] for f in found)


PLAIN_VIEW = """
from rest_framework import viewsets
from library.models import Book
from library.serializers import BookSerializer

class BookViewSet(viewsets.ModelViewSet):
    queryset = Book.objects.all()
    serializer_class = BookSerializer
"""

METHOD_SERIALIZER = """
from rest_framework import serializers
from library.models import Book

class BookSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = Book
        fields = ["id", "title", "author_name"]

    def get_author_name(self, obj):
        return obj.author.name
"""


class TestTheCaseItExistsFor:
    def test_a_forward_relation_the_view_did_not_select(self, make_project) -> None:
        found = findings(make_project, METHOD_SERIALIZER, PLAIN_VIEW)
        assert paths(found) == ["author"]
        assert found[0].properties["fetch"] == "select_related"

    def test_the_finding_names_the_view_that_has_to_change(self, make_project) -> None:
        """The fix is in a different file from the defect, so the evidence
        has to carry the reader there."""
        found = findings(make_project, METHOD_SERIALIZER, PLAIN_VIEW)
        content = found[0].evidence[0].content
        assert "library.views.BookViewSet" in content
        assert "select_related('author')" in content

    def test_a_multi_hop_forward_path(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                imprint = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "imprint"]

                def get_imprint(self, obj):
                    return obj.author.publisher.name
            """,
            PLAIN_VIEW,
        )
        assert paths(found) == ["author__publisher"]

    def test_a_many_to_many_needs_a_prefetch(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                labels = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "labels"]

                def get_labels(self, obj):
                    return [t.label for t in obj.tags.all()]
            """,
            PLAIN_VIEW,
        )
        assert paths(found) == ["tags"]
        assert found[0].properties["fetch"] == "prefetch_related"

    def test_a_get_queryset_override_is_read_too(self, make_project) -> None:
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                serializer_class = BookSerializer

                def get_queryset(self):
                    return Book.objects.filter(title="x")
            """,
        )
        assert paths(found) == ["author"]


class TestWhenTheViewAlreadyFetched:
    def test_select_related_on_the_class_attribute_silences_it(self, make_project) -> None:
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                queryset = Book.objects.select_related("author")
                serializer_class = BookSerializer
            """,
        )
        assert found == []

    def test_select_related_in_get_queryset_silences_it(self, make_project) -> None:
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                serializer_class = BookSerializer

                def get_queryset(self):
                    return Book.objects.select_related("author")
            """,
        )
        assert found == []

    def test_a_longer_path_covers_the_prefix(self, make_project) -> None:
        """`select_related('author__publisher')` loads `author` on the way."""
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                queryset = Book.objects.select_related("author__publisher")
                serializer_class = BookSerializer
            """,
        )
        assert found == []

    def test_prefetch_related_silences_the_many_to_many(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                labels = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "labels"]

                def get_labels(self, obj):
                    return [t.label for t in obj.tags.all()]
            """,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                queryset = Book.objects.prefetch_related("tags")
                serializer_class = BookSerializer
            """,
        )
        assert found == []

    def test_select_related_does_not_silence_a_many_to_many(self, make_project) -> None:
        """The same trap DJP-002 has: a join cannot reach many rows, and the
        bare form claims every forward path without touching this one."""
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                labels = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "labels"]

                def get_labels(self, obj):
                    return [t.label for t in obj.tags.all()]
            """,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                queryset = Book.objects.select_related()
                serializer_class = BookSerializer
            """,
        )
        assert paths(found) == ["tags"]


class TestOnlyEveryBranchCounts:
    def test_a_branch_that_forgot_is_still_reported(self, make_project) -> None:
        """A `get_queryset` guarantees only what all of its returns did."""
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                serializer_class = BookSerializer

                def get_queryset(self):
                    if self.request.user.is_staff:
                        return Book.objects.select_related("author")
                    return Book.objects.all()
            """,
        )
        assert paths(found) == ["author"]

    def test_every_branch_fetching_it_is_enough(self, make_project) -> None:
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                serializer_class = BookSerializer

                def get_queryset(self):
                    if self.request.user.is_staff:
                        return Book.objects.select_related("author")
                    return Book.objects.select_related("author").filter(title="x")
            """,
        )
        assert found == []


class TestWhatItRefusesToGuess:
    def test_a_serializer_no_view_uses_is_silent(self, make_project) -> None:
        """Without a queryset there is no coverage question to answer."""
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book

            class BookViewSet(viewsets.ModelViewSet):
                queryset = Book.objects.all()
            """,
        )
        assert found == []

    def test_a_column_is_not_a_relation(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                shout = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "shout"]

                def get_shout(self, obj):
                    return obj.title.upper()
            """,
            PLAIN_VIEW,
        )
        assert found == []

    def test_a_getter_with_no_row_parameter_is_not_per_row(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                whoami = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "whoami"]

                def get_whoami(self):
                    return "static"
            """,
            PLAIN_VIEW,
        )
        assert found == []

    def test_a_declared_field_that_is_not_a_method_field_is_ignored(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                author_name = serializers.CharField(source="author.name")

                class Meta:
                    model = Book
                    fields = ["id", "author_name"]

                def get_author_name(self, obj):
                    return obj.author.name
            """,
            PLAIN_VIEW,
        )
        assert found == []

    def test_a_rebound_row_name_says_nothing(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                other = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "other"]

                def get_other(self, obj):
                    obj = self.context["elsewhere"]
                    return obj.author.name
            """,
            PLAIN_VIEW,
        )
        assert found == []

    def test_naming_a_manager_is_not_evaluating_it(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                mgr = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "mgr"]

                def get_mgr(self, obj):
                    return obj.tags
            """,
            PLAIN_VIEW,
        )
        assert found == []

    def test_the_same_path_is_reported_once(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                blurb = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "blurb"]

                def get_blurb(self, obj):
                    return obj.author.name + " / " + obj.author.name
            """,
            PLAIN_VIEW,
        )
        assert paths(found) == ["author"]

    def test_two_different_paths_are_both_reported(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                blurb = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "blurb"]

                def get_blurb(self, obj):
                    return obj.author.name + " / " + obj.author.publisher.name
            """,
            PLAIN_VIEW,
        )
        assert paths(found) == ["author", "author__publisher"]

    def test_a_relation_read_with_nothing_after_it_is_still_a_query(self, make_project) -> None:
        found = findings(
            make_project,
            """
            from rest_framework import serializers
            from library.models import Book

            class BookSerializer(serializers.ModelSerializer):
                who = serializers.SerializerMethodField()

                class Meta:
                    model = Book
                    fields = ["id", "who"]

                def get_who(self, obj):
                    return str(obj.author)
            """,
            PLAIN_VIEW,
        )
        assert paths(found) == ["author"]

    def test_a_project_with_no_api_at_all_reports_nothing(self, make_project) -> None:
        ctx: ProjectContext = make_project(
            {
                "manage.py": "import os\n",
                "library/__init__.py": "",
                "library/settings.py": SETTINGS,
                "library/urls.py": "urlpatterns = []\n",
                "library/models.py": textwrap.dedent(MODELS).lstrip(),
            }
        )
        rule = next(r for r in all_rules() if r.meta.id == "DJP-003")
        assert list(rule().check(ctx)) == []


class TestHowSureItIs:
    def test_an_unreadable_queryset_downgrades_rather_than_silences(self, make_project) -> None:
        """The traversal is still per-row; only coverage is unknown."""
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.serializers import BookSerializer
            from library.helpers import visible

            class BookViewSet(viewsets.ModelViewSet):
                serializer_class = BookSerializer

                def get_queryset(self):
                    return visible(self.request)
            """,
        )
        assert paths(found) == ["author"]
        assert found[0].confidence is Confidence.TENTATIVE

    def test_a_readable_queryset_speaks_firmly(self, make_project) -> None:
        found = findings(make_project, METHOD_SERIALIZER, PLAIN_VIEW)
        assert found[0].confidence is Confidence.FIRM

    def test_a_generic_prefetch_is_read_like_any_other(self, make_project) -> None:
        """NetBox's `InterfaceViewSet` in miniature.

        `GenericPrefetch` takes the same leading lookup as `Prefetch`, so
        reading it is what turns this from a tentative finding into no finding:
        `author__publisher` covers `author`, and a prefetch through a forward
        foreign key does populate it -- measured at 3 queries against a 5-query
        baseline by `scripts/prefetch_cache_probe.py`.
        """
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from django.contrib.contenttypes.prefetch import GenericPrefetch
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                queryset = Book.objects.prefetch_related(
                    GenericPrefetch("author__publisher", [Book.objects.all()])
                )
                serializer_class = BookSerializer
            """,
        )
        assert paths(found) == []

    def test_a_fetch_call_we_cannot_read_downgrades_rather_than_silences(
        self, make_project
    ) -> None:
        """A splatted argument list names paths we cannot enumerate.

        Treating it as "did not fetch" would be a firm false positive, and
        treating it as "fetched everything" would hide real ones, so the rule
        speaks tentatively instead.
        """
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            PATHS = ["author"]

            class BookViewSet(viewsets.ModelViewSet):
                queryset = Book.objects.prefetch_related(*PATHS)
                serializer_class = BookSerializer
            """,
        )
        assert paths(found) == ["author"]
        assert found[0].confidence is Confidence.TENTATIVE

    def test_a_prefetch_through_a_forward_relation_covers_it(self, make_project) -> None:
        """Measured: `prefetch_related('vm__site')` costs 3 queries, not 5."""
        found = findings(
            make_project,
            METHOD_SERIALIZER,
            """
            from rest_framework import viewsets
            from library.models import Book
            from library.serializers import BookSerializer

            class BookViewSet(viewsets.ModelViewSet):
                queryset = Book.objects.prefetch_related("author__publisher")
                serializer_class = BookSerializer
            """,
        )
        assert found == []


class TestWhereTheGetterLives:
    """177 of NetBox's 187 reachable method fields implement the getter on a base."""

    BASE = """
    from rest_framework import serializers
    from library.models import Book

    class DisplaySerializer(serializers.ModelSerializer):
        display = serializers.SerializerMethodField()

        def get_display(self, obj):
            return obj.author.name

    class BookSerializer(DisplaySerializer):
        class Meta:
            model = Book
            fields = ["id", "display"]
    """

    def test_an_inherited_getter_is_followed(self, make_project) -> None:
        assert paths(findings(make_project, self.BASE, PLAIN_VIEW)) == ["author"]

    def test_the_finding_is_reported_where_the_traversal_is_written(self, make_project) -> None:
        """The base may live in another file; the fix belongs at the traversal."""
        ctx: ProjectContext = make_project(
            {
                "manage.py": "import os\n",
                "library/__init__.py": "",
                "library/settings.py": SETTINGS,
                "library/urls.py": URLS,
                "library/models.py": MODELS,
                "library/base.py": textwrap.dedent(
                    """
                    from rest_framework import serializers

                    class DisplaySerializer(serializers.ModelSerializer):
                        display = serializers.SerializerMethodField()

                        def get_display(self, obj):
                            return obj.author.name
                    """
                ).lstrip(),
                "library/serializers.py": textwrap.dedent(
                    """
                    from library.base import DisplaySerializer
                    from library.models import Book

                    class BookSerializer(DisplaySerializer):
                        class Meta:
                            model = Book
                            fields = ["id", "display"]
                    """
                ).lstrip(),
                "library/views.py": textwrap.dedent(PLAIN_VIEW).lstrip(),
            }
        )
        rule = next(r for r in all_rules() if r.meta.id == "DJP-003")
        found = list(rule().check(ctx))
        assert [str(f.location.file).rsplit("/", 1)[-1] for f in found] == ["base.py"]

    def test_a_serializer_reached_through_a_package_re_export_is_linked(self, make_project) -> None:
        """`resolve_name` stops at the re-export path; `lookup` follows the star.

        NetBox writes `from .circuits import *` in
        `circuits/api/serializers/__init__.py`. Keying suppliers on the name as
        written lost 136 of its 137 serializer-bearing views.
        """
        ctx: ProjectContext = make_project(
            {
                "manage.py": "import os\n",
                "library/__init__.py": "",
                "library/settings.py": SETTINGS,
                "library/urls.py": URLS,
                "library/models.py": MODELS,
                "library/serializers/__init__.py": "from library.serializers.books import *\n",
                "library/serializers/books.py": textwrap.dedent(METHOD_SERIALIZER).lstrip(),
                "library/views.py": textwrap.dedent(PLAIN_VIEW).lstrip(),
            }
        )
        rule = next(r for r in all_rules() if r.meta.id == "DJP-003")
        assert paths(list(rule().check(ctx))) == ["author"]
