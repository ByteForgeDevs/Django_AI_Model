"""DJP-009 -- a field read on a queryset that deferred it.

Every guard in this rule exists because a measurement contradicted an
assumption, so the tests are organised around those measurements rather than
around the code. `scripts/prefetch_cache_probe.py` establishes the three that
matter: reading an excluded field costs a query per row, assigning one costs
nothing, and the primary key is loaded whatever `only()` said.

The declining tests carry most of the weight here. The rule's first version
produced eleven findings across the benchmark corpora and every one of them
was wrong -- ten to a rebound loop variable and one to an assignment -- so
`TestTheRowsItDeclines` is a direct transcription of that failure.
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
INSTALLED_APPS = ["library"]
ROOT_URLCONF = "library.urls"
"""

MODELS = """
from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)


class Book(models.Model):
    title = models.CharField(max_length=200)
    isbn = models.CharField(max_length=20)
    blurb = models.TextField()
    author = models.ForeignKey(Author, on_delete=models.CASCADE)


class Reprint(Book):
    year = models.IntegerField()


class Coded(models.Model):
    code = models.CharField(max_length=10, primary_key=True)
    label = models.CharField(max_length=50)
    extra = models.CharField(max_length=50)
"""


def findings(make_project, files: dict[str, str]) -> list[Finding]:
    tree = {
        "manage.py": "import os\n",
        "library/__init__.py": "",
        "library/settings.py": SETTINGS,
        "library/urls.py": "urlpatterns = []\n",
        "library/models.py": MODELS,
    }
    tree.update({name: textwrap.dedent(code).lstrip() for name, code in files.items()})
    ctx: ProjectContext = make_project(tree)
    rule = next(r for r in all_rules() if r.meta.id == "DJP-009")
    return list(rule().check(ctx))


def in_loop(make_project, body: str) -> list[Finding]:
    """A loop over a restricted queryset, in a plain module."""
    return findings(
        make_project,
        {
            "library/work.py": "from library.models import Book, Coded, Reprint\n\n\ndef run():\n"
            + textwrap.indent(textwrap.dedent(body).strip() + "\n", "    ")
        },
    )


class TestTheReadsItReports:
    def test_a_field_only_left_out(self, make_project):
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.isbn)
            """,
        )
        assert len(found) == 1
        assert "b.isbn" in found[0].message
        assert "only('title')" in found[0].message

    def test_a_field_defer_named(self, make_project):
        found = in_loop(
            make_project,
            """
            for b in Book.objects.defer("blurb"):
                print(b.blurb)
            """,
        )
        assert len(found) == 1
        assert "b.blurb" in found[0].message

    def test_one_finding_per_attribute(self, make_project):
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.isbn, b.blurb)
            """,
        )
        assert len(found) == 2
        assert len({f.location.line for f in found}) == 1
        assert {"isbn", "blurb"} == {f.message.split("`")[1].split(".")[1] for f in found}

    def test_the_same_attribute_read_twice_is_one_finding(self, make_project):
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.isbn)
                print(b.isbn)
            """,
        )
        assert len(found) == 1

    def test_a_field_inherited_from_a_parent_model(self, make_project):
        found = in_loop(
            make_project,
            """
            for r in Reprint.objects.only("year"):
                print(r.isbn)
            """,
        )
        assert len(found) == 1

    def test_a_method_called_on_a_deferred_field(self, make_project):
        """`b.title.upper()` loads `title`; only the first hop matters."""
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("isbn"):
                print(b.title.upper())
            """,
        )
        assert len(found) == 1
        assert "b.title" in found[0].message

    def test_a_join_path_does_not_load_local_columns(self, make_project):
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("author__name"):
                print(b.title)
            """,
        )
        assert len(found) == 1
        assert "author__name" in found[0].message

    def test_a_write_through_a_deferred_field(self, make_project):
        """`b.title.x = 1` must load `title` before it can set anything."""
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("isbn"):
                b.title.cached = 1
            """,
        )
        assert len(found) == 1

    def test_a_write_to_a_different_object(self, make_project):
        """Another object's `isbn` says nothing about this row's."""
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                other.isbn = "x"
                print(b.isbn)
            """,
        )
        assert len(found) == 1

    def test_the_evidence_names_the_restriction_and_the_column(self, make_project):
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.isbn)
            """,
        )
        content = found[0].evidence[0].content
        assert "only('title')" in content
        assert "library.Book.isbn" in content

    def test_it_is_firm_by_default(self, make_project):
        found = in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.isbn)
            """,
        )
        assert found[0].confidence is Confidence.FIRM


class TestTheRowsItDeclines:
    """The eleven false positives the first version of this rule produced."""

    def test_a_rebound_loop_variable(self, make_project):
        """Ten of the eleven. Both loops were in one pretix module."""
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                b = Book.objects.get(pk=b.pk)
                print(b.isbn)
            """,
        )

    def test_an_assignment_to_an_excluded_field(self, make_project):
        """The eleventh. Measured at one query, not one per row."""
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                b.isbn = "new"
            """,
        )

    def test_an_assignment_before_a_read_of_the_same_field(self, make_project):
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                b.isbn = "new"
                print(b.isbn)
            """,
        )

    def test_a_field_the_restriction_kept(self, make_project):
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.title)
            """,
        )

    def test_a_field_defer_did_not_name(self, make_project):
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.defer("blurb"):
                print(b.isbn)
            """,
        )

    def test_the_primary_key(self, make_project):
        """Always loaded, whatever `only()` said. Measured at one query."""
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.pk, b.id)
            """,
        )

    def test_an_unrestricted_queryset(self, make_project):
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.all():
                print(b.isbn)
            """,
        )

    def test_a_bare_relation_read(self, make_project):
        """Deferred, but its cost is a join: DJP-001 reports this one."""
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.author)
            """,
        )

    def test_an_explicitly_declared_primary_key(self, make_project):
        """`only()` cannot defer the primary key. Measured at one query."""
        assert not in_loop(
            make_project,
            """
            for c in Coded.objects.only("label"):
                print(c.code)
            """,
        )

    def test_a_restriction_it_could_not_read(self, make_project):
        assert not findings(
            make_project,
            {
                "library/work.py": """
                from library.models import Book

                WANTED = ["title"]


                def run():
                    for b in Book.objects.only("title").defer(*WANTED):
                        print(b.isbn)
                """
            },
        )

    def test_a_relation_named_through_a_join(self, make_project):
        """`only('author__name')` fetches `author`; DJP-001 owns the rest."""
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("author__name"):
                print(b.author)
            """,
        )

    def test_a_relation_traversal(self, make_project):
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.author.name)
            """,
        )

    def test_an_attribute_that_is_not_a_field(self, make_project):
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title"):
                print(b.get_absolute_url())
            """,
        )

    def test_rows_that_are_not_instances(self, make_project):
        assert not in_loop(
            make_project,
            """
            for b in Book.objects.only("title").values("isbn"):
                print(b.isbn)
            """,
        )

    def test_a_file_with_no_restriction_at_all(self, make_project):
        """The text prefilter's control: silence must not depend on it."""
        assert not findings(
            make_project,
            {
                "library/work.py": """
                from library.models import Book


                def run():
                    for b in Book.objects.all():
                        print(b.isbn)
                """
            },
        )

    def test_an_empty_project(self, make_project):
        assert not findings(make_project, {})


VIEWS = """
from rest_framework import serializers, viewsets

from library.models import Book


class BookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = {fields}


class BookViewSet(viewsets.ModelViewSet):
    {queryset}
    serializer_class = BookSerializer
"""


def in_view(make_project, *, fields: str, queryset: str) -> list[Finding]:
    return findings(
        make_project,
        {"library/views.py": VIEWS.format(fields=fields, queryset=queryset)},
    )


class TestTheViewsItReports:
    """The shape with no loop in it, and the one that costs the most."""

    def test_a_serializer_naming_a_field_the_queryset_deferred(self, make_project):
        found = in_view(
            make_project,
            fields='["id", "title", "isbn"]',
            queryset='queryset = Book.objects.only("title")',
        )
        assert len(found) == 1
        assert "BookViewSet" in found[0].message
        assert "BookSerializer" in found[0].message
        assert "isbn" in found[0].message

    def test_a_serializer_taking_every_field(self, make_project):
        found = in_view(
            make_project,
            fields='"__all__"',
            queryset='queryset = Book.objects.only("title")',
        )
        assert {"isbn", "blurb"} <= {
            f.message.split("serialises `")[1].split("`")[0] for f in found
        }

    def test_a_serializer_using_exclude(self, make_project):
        """`exclude` names what is dropped, so the rest is read."""
        found = findings(
            make_project,
            {
                "library/views.py": """
                from rest_framework import serializers, viewsets

                from library.models import Book


                class BookSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Book
                        exclude = ["blurb"]


                class BookViewSet(viewsets.ModelViewSet):
                    queryset = Book.objects.only("title")
                    serializer_class = BookSerializer
                """
            },
        )
        assert {"isbn"} <= {f.message.split("serialises `")[1].split("`")[0] for f in found}

    def test_get_queryset_rather_than_the_attribute(self, make_project):
        found = in_view(
            make_project,
            fields='["id", "title", "isbn"]',
            queryset=('def get_queryset(self):\n        return Book.objects.only("title")'),
        )
        assert len(found) == 1

    def test_a_declared_field_following_its_source(self, make_project):
        found = findings(
            make_project,
            {
                "library/views.py": """
                from rest_framework import serializers, viewsets

                from library.models import Book


                class BookSerializer(serializers.ModelSerializer):
                    code = serializers.CharField(source="isbn")

                    class Meta:
                        model = Book
                        fields = ["id", "title", "code"]


                class BookViewSet(viewsets.ModelViewSet):
                    queryset = Book.objects.only("title")
                    serializer_class = BookSerializer
                """
            },
        )
        assert len(found) == 1
        assert "isbn" in found[0].message


class TestTheViewsItDeclines:
    def test_a_serializer_that_omits_the_deferred_column(self, make_project):
        """NetBox's `DataFileViewSet`, which is why the rule found nothing."""
        assert not in_view(
            make_project,
            fields='["id", "title"]',
            queryset='queryset = Book.objects.defer("blurb")',
        )

    def test_an_unrestricted_queryset(self, make_project):
        assert not in_view(
            make_project,
            fields='["id", "title", "isbn"]',
            queryset="queryset = Book.objects.all()",
        )

    def test_branches_that_do_not_agree(self, make_project):
        """One branch loads everything, so the column is not always deferred.

        The restricted branch is written *first* deliberately. With it second,
        the unrestricted branch is the one `agreed` inspects first and the
        disagreement is caught by whichever guard happens to run earliest,
        which leaves the equality test itself unexercised.
        """
        assert not in_view(
            make_project,
            fields='["id", "title", "isbn"]',
            queryset=(
                "def get_queryset(self):\n"
                "        if self.request.user.is_staff:\n"
                '            return Book.objects.only("title")\n'
                "        return Book.objects.all()"
            ),
        )

    def test_branches_restricting_different_columns(self, make_project):
        """Both restricted, neither agreeing.

        The serializer names a column *neither* branch loads, so a rule that
        picked either branch instead of insisting they agree would report --
        which matters because `queryset_expressions` yields in AST walk order
        rather than source order, and the test must not depend on which came
        first.
        """
        assert not in_view(
            make_project,
            fields='["id", "title", "isbn", "blurb"]',
            queryset=(
                "def get_queryset(self):\n"
                "        if self.request.user.is_staff:\n"
                '            return Book.objects.only("title")\n'
                '        return Book.objects.only("title", "isbn")'
            ),
        )

    def test_a_branch_whose_restriction_it_could_not_read(self, make_project):
        assert not findings(
            make_project,
            {
                "library/views.py": """
                from rest_framework import serializers, viewsets

                from library.models import Book

                WANTED = ["title"]


                class BookSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Book
                        fields = ["id", "title", "isbn"]


                class BookViewSet(viewsets.ModelViewSet):
                    def get_queryset(self):
                        return Book.objects.only(*WANTED)

                    serializer_class = BookSerializer
                """
            },
        )

    def test_a_serializer_whose_meta_it_could_not_read(self, make_project):
        assert not findings(
            make_project,
            {
                "library/views.py": """
                from rest_framework import serializers, viewsets

                from library.models import Book

                EXTRA = ["isbn"]


                class BookSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Book
                        fields = ["id", "title"] + EXTRA


                class BookViewSet(viewsets.ModelViewSet):
                    queryset = Book.objects.only("title")
                    serializer_class = BookSerializer
                """
            },
        )

    def test_a_serializer_that_inherits_its_meta(self, make_project):
        """`mode` is unset, so the field list is unknown rather than empty."""
        assert not findings(
            make_project,
            {
                "library/views.py": """
                from rest_framework import serializers, viewsets

                from library.models import Book


                class BaseSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Book
                        fields = ["id", "title", "isbn"]


                class BookSerializer(BaseSerializer):
                    pass


                class BookViewSet(viewsets.ModelViewSet):
                    queryset = Book.objects.only("title")
                    serializer_class = BookSerializer
                """
            },
        )

    def test_branches_that_agree(self, make_project):
        found = in_view(
            make_project,
            fields='["id", "title", "isbn"]',
            queryset=(
                "def get_queryset(self):\n"
                "        if self.request.user.is_staff:\n"
                '            return Book.objects.only("title")\n'
                '        return Book.objects.only("title")'
            ),
        )
        assert len(found) == 1

    def test_rows_that_are_not_instances(self, make_project):
        assert not in_view(
            make_project,
            fields='["id", "title", "isbn"]',
            queryset='queryset = Book.objects.only("title").values("isbn")',
        )

    def test_a_queryset_it_cannot_track(self, make_project):
        """A sibling view supplies the `.only(` the text prefilter looks for."""
        assert not findings(
            make_project,
            {
                "library/helpers.py": """
                from library.models import Book


                def restricted_books():
                    return Book.objects.only("title")
                """,
                "library/views.py": """
                from rest_framework import serializers, viewsets

                from library.models import Author, Book
                from library.helpers import restricted_books


                class BookSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Book
                        fields = ["id", "title", "isbn"]


                class AuthorSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Author
                        fields = ["id", "name"]


                class BookViewSet(viewsets.ModelViewSet):
                    queryset = restricted_books()
                    serializer_class = BookSerializer


                class AuthorViewSet(viewsets.ModelViewSet):
                    queryset = Author.objects.only("name")
                    serializer_class = AuthorSerializer
                """,
            },
        )

    def test_a_serializer_naming_neither_fields_nor_exclude(self, make_project):
        """DRF rejects this at runtime; the rule must not guess at it."""
        assert not findings(
            make_project,
            {
                "library/views.py": """
                from rest_framework import serializers, viewsets

                from library.models import Book


                class BookSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Book


                class BookViewSet(viewsets.ModelViewSet):
                    queryset = Book.objects.only("title")
                    serializer_class = BookSerializer
                """
            },
        )

    def test_an_exclude_list_it_could_only_partly_read(self, make_project):
        """A half-read `exclude` would invent columns the serializer drops."""
        assert not findings(
            make_project,
            {
                "library/views.py": """
                from rest_framework import serializers, viewsets

                from library.models import Book

                HIDDEN = "isbn"


                class BookSerializer(serializers.ModelSerializer):
                    class Meta:
                        model = Book
                        exclude = ["blurb", HIDDEN]


                class BookViewSet(viewsets.ModelViewSet):
                    queryset = Book.objects.only("title")
                    serializer_class = BookSerializer
                """
            },
        )

    def test_a_view_with_no_serializer(self, make_project):
        assert not findings(
            make_project,
            {
                "library/views.py": """
                from rest_framework import viewsets

                from library.models import Book


                class BookViewSet(viewsets.ModelViewSet):
                    queryset = Book.objects.only("title")
                """
            },
        )
