"""Reading fields, and being honest about the ones we cannot read.

Most of these tests are about the third state. A keyword that is present but
computed is not the same as an absent keyword, and a graph that conflates them
lets a rule claim the field has no default when what it has is
``default=timezone.now``.
"""

from __future__ import annotations

from djaudit.astutils import UNKNOWN
from djaudit.graph.builder import build_model_graph
from djaudit.graph.nodes import ModelGraph, ModelNode


def model(graph: ModelGraph, ref: str) -> ModelNode:
    found = graph.get(ref)
    assert found is not None, f"{ref} not in graph: {sorted(graph.models)}"
    return found


def one_model(make_project, body: str) -> ModelNode:
    """Build a project around a single model body and return the model."""
    ctx = make_project(
        {
            "shop/__init__.py": "",
            "shop/models.py": (
                "from django.db import models\n\n\nclass Order(models.Model):\n" + body
            ),
        }
    )
    return model(build_model_graph(ctx), "shop.Order")


class TestWhatCountsAsAField:
    def test_reads_django_fields(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    name = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True)
    total = models.DecimalField(max_digits=8, decimal_places=2)
""",
        )
        assert list(order.fields) == ["name", "created", "total"]
        assert order.fields["created"].auto_now_add

    def test_declaration_order_is_kept(self, make_project) -> None:
        # Serializer rules report on "the first sensitive field exposed", and
        # a reordered dict would move the finding to a different line.
        order = one_model(
            make_project,
            """
    zulu = models.CharField(max_length=1)
    alpha = models.CharField(max_length=1)
""",
        )
        assert list(order.fields) == ["zulu", "alpha"]

    def test_accepts_a_custom_field(self, make_project) -> None:
        # NetBox has six of these. Requiring a Django class would drop every
        # custom column in the project.
        order = one_model(
            make_project,
            """
    colour = ColorField()
""",
        )
        assert order.fields["colour"].kind == "ColorField"
        assert not order.fields["colour"].is_django

    def test_rejects_things_that_are_not_columns(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    objects = models.Manager()
    name = models.CharField(max_length=2)
""",
        )
        assert list(order.fields) == ["name"]

    def test_finds_a_generic_foreign_key(self, make_project) -> None:
        # Neither name ends in Field, so without listing them explicitly a
        # generic relation would be invisible.
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.contrib.contenttypes.fields import GenericForeignKey
                from django.db import models

                class Note(models.Model):
                    target = GenericForeignKey("content_type", "object_id")
                """,
            }
        )
        assert model(build_model_graph(ctx), "shop.Note").fields["target"].kind == (
            "GenericForeignKey"
        )

    def test_finds_a_conditionally_declared_field(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    name = models.CharField(max_length=2)
    if True:
        extra = models.TextField()
""",
        )
        assert "extra" in order.fields


class TestParameters:
    def test_reads_the_flags(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    slug = models.SlugField(unique=True, db_index=True)
    note = models.TextField(null=True, blank=True)
    code = models.CharField(max_length=8, primary_key=True, editable=False)
""",
        )
        assert order.fields["slug"].unique and order.fields["slug"].db_index
        assert order.fields["note"].null and order.fields["note"].blank
        assert order.fields["code"].primary_key
        assert not order.fields["code"].editable

    def test_an_absent_flag_reports_djangos_default(self, make_project) -> None:
        order = one_model(make_project, "    name = models.CharField(max_length=2)\n")
        name = order.fields["name"]
        assert not name.null and not name.blank and not name.unique
        assert name.editable  # Django's default is True, not False
        assert name.unreadable == ()

    def test_max_length_and_default(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    name = models.CharField(max_length=200, default="x")
    count = models.IntegerField(default=0)
""",
        )
        assert order.fields["name"].max_length == 200
        assert order.fields["name"].default == "x"
        assert order.fields["count"].has_default
        assert order.fields["count"].default == 0

    def test_no_default_is_not_a_default_of_none(self, make_project) -> None:
        # Django's sentinel is NOT_PROVIDED, and "no default" and
        # "default=None" mean different things to a migration.
        order = one_model(
            make_project,
            """
    a = models.IntegerField()
    b = models.IntegerField(default=None, null=True)
""",
        )
        assert not order.fields["a"].has_default
        assert order.fields["b"].has_default
        assert order.fields["b"].default is None


class TestWhatWeCannotRead:
    def test_a_callable_default_is_marked_unreadable(self, make_project) -> None:
        # This is the normal case, not a failure: default=timezone.now is
        # exactly right and cannot be evaluated.
        order = one_model(
            make_project,
            """
    created = models.DateTimeField(default=timezone.now)
""",
        )
        created = order.fields["created"]
        assert created.has_default
        assert created.default is UNKNOWN
        assert "default" in created.unreadable
        assert not created.knows("default")

    def test_a_computed_flag_keeps_djangos_default_and_says_so(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    name = models.CharField(max_length=2, null=USE_NULL)
""",
        )
        name = order.fields["name"]
        assert not name.null
        assert not name.knows("null")

    def test_enum_choices_are_unreadable_rather_than_absent(self, make_project) -> None:
        # Passing a TextChoices class is idiomatic and modern. Treating it as
        # "no choices" would misreport every project that uses one.
        order = one_model(
            make_project,
            """
    status = models.CharField(max_length=2, choices=Status.choices)
""",
        )
        status = order.fields["status"]
        assert status.has_choices
        assert not status.knows("choices")

    def test_literal_choices_are_read(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    status = models.CharField(max_length=2, choices=[("a", "A"), ("b", "B")])
""",
        )
        status = order.fields["status"]
        assert status.knows("choices")
        assert status.choices == (("a", "A"), ("b", "B"))

    def test_an_unreadable_max_length_is_not_guessed(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    name = models.CharField(max_length=NAME_LENGTH)
""",
        )
        assert order.fields["name"].max_length is None
        assert not order.fields["name"].knows("max_length")


class TestRelationsAreFlagged:
    def test_relation_fields_are_marked(self, make_project) -> None:
        order = one_model(
            make_project,
            """
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE)
    profile = models.OneToOneField("Profile", on_delete=models.CASCADE)
    tags = models.ManyToManyField("Tag")
    name = models.CharField(max_length=2)
""",
        )
        assert [n for n, f in order.fields.items() if f.is_relation] == [
            "owner",
            "profile",
            "tags",
        ]

    def test_raw_arguments_are_kept_for_the_next_pass(self, make_project) -> None:
        # Relation targets are positional, and re-walking the tree to find them
        # would mean parsing every model body twice.
        order = one_model(
            make_project,
            """
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE)
""",
        )
        owner = order.fields["owner"]
        assert len(owner.args) == 1
        assert "on_delete" in owner.kwargs


class TestInheritanceIsNotFlattenedYet:
    def test_a_field_belongs_to_the_class_that_declared_it(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Timestamped(models.Model):
                    created = models.DateTimeField(auto_now_add=True)

                    class Meta:
                        abstract = True

                class Order(Timestamped):
                    name = models.CharField(max_length=2)
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert list(model(graph, "shop.Timestamped").fields) == ["created"]
        assert list(model(graph, "shop.Order").fields) == ["name"]


class TestRealProjects:
    def test_the_fixture_project(self, vulnerable_project) -> None:
        from djaudit.discovery import build_context

        graph = build_context(vulnerable_project).model_graph
        book = model(graph, "app.Book")
        assert book.fields["title"].max_length == 200
        assert book.fields["author"].is_relation
