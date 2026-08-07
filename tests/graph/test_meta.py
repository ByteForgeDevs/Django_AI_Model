"""``class Meta``, where a model says everything that is not a field.

Later rules ask this data direct questions -- is this filtered column indexed,
does this table have a default ordering that sorts every page, what is the
table actually called -- so the failure mode is not a missing feature but a
confident wrong answer. Each test below pins one place where the option name
and Django's behaviour disagree.
"""

from __future__ import annotations

from djaudit.graph.builder import build_model_graph
from djaudit.graph.nodes import ModelGraph, ModelNode


def model(graph: ModelGraph, ref: str) -> ModelNode:
    found = graph.get(ref)
    assert found is not None, f"{ref} not in graph: {sorted(graph.models)}"
    return found


def project(make_project, models_source: str) -> ModelGraph:
    return build_model_graph(
        make_project({"shop/__init__.py": "", "shop/models.py": models_source})
    )


def one(make_project, body: str) -> ModelNode:
    graph = project(
        make_project,
        f"""
        from django.db import models
        from django.db.models import Index, Q, UniqueConstraint

        class Order(models.Model):
            reference = models.CharField(max_length=32)
            created = models.DateTimeField()
{body}
        """,
    )
    return model(graph, "shop.Order")


class TestTableName:
    def test_it_is_derived_when_unset(self, make_project) -> None:
        # Absent is not the same as unknown: Django builds app_label_modelname,
        # and a rule matching a table name out of raw SQL needs that string.
        order = one(make_project, "")
        assert order.db_table == "shop_order"
        assert not order.db_table_explicit

    def test_an_explicit_name_wins(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                db_table = "legacy_orders"
            """,
        )
        assert order.db_table == "legacy_orders"
        assert order.db_table_explicit

    def test_an_abstract_model_has_no_table(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                abstract = True
            """,
        )
        assert order.db_table == ""

    def test_the_meta_app_label_changes_it(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                class Meta:
                    app_label = "billing"
            """,
        )
        assert model(graph, "billing.Order").db_table == "billing_order"


class TestAppLabel:
    def test_meta_app_label_rekeys_the_model(self, make_project) -> None:
        # Not cosmetic: the label is what "billing.Order" in a foreign key
        # resolves against, so getting it wrong breaks every relation into
        # this model.
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                class Meta:
                    app_label = "billing"
            """,
        )
        assert graph.get("billing.Order") is not None
        assert graph.get("shop.Order") is None

    def test_a_relation_resolves_through_the_override(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                class Meta:
                    app_label = "billing"

            class Line(models.Model):
                order = models.ForeignKey("billing.Order", on_delete=models.CASCADE)
            """,
        )
        # Only the model carrying the option moves. Line stays in shop, and
        # its string reference has to cross into the relabelled app.
        edge = model(graph, "shop.Line").relations[0]
        assert edge.target == "billing.Order"


class TestOrdering:
    def test_it_keeps_the_direction_prefix(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                ordering = ["-created", "reference"]
            """,
        )
        assert order.ordering == ("-created", "reference")
        assert not order.ordering_unreadable

    def test_a_bare_string_is_a_sequence_of_one(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                ordering = "created"
            """,
        )
        assert order.ordering == ("created",)

    def test_absent_ordering_is_not_unreadable(self, make_project) -> None:
        order = one(make_project, "")
        assert order.ordering == ()
        assert not order.ordering_unreadable

    def test_an_expression_element_leaves_the_rest_readable(self, make_project) -> None:
        """NetBox's ``dcim.Interface``, which is why this is read element-wise.

        ``ordering = ('device', CollateAsChar('_name'))`` evaluates to nothing
        as a whole, and the first column -- the one that decides whether the
        sort can use an index -- is a plain string sitting right there.
        """
        order = one(
            make_project,
            """
            class Meta:
                ordering = ("reference", models.F("created").desc())
            """,
        )
        assert order.ordering == ("reference",)
        assert order.ordering_unreadable

    def test_a_computed_ordering_is_flagged_not_dropped(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                ordering = DEFAULT_ORDER
            """,
        )
        assert order.ordering == ()
        assert order.ordering_unreadable


class TestUniqueTogether:
    def test_a_bare_tuple_is_one_constraint(self, make_project) -> None:
        # The classic misreading. ("a", "b") is a single constraint over two
        # columns; treating it as two would claim each column is unique alone.
        order = one(
            make_project,
            """
            class Meta:
                unique_together = ("reference", "created")
            """,
        )
        assert order.unique_together == (("reference", "created"),)

    def test_nested_tuples_are_separate_constraints(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                unique_together = (("reference",), ("created",))
            """,
        )
        assert order.unique_together == (("reference",), ("created",))

    def test_it_is_empty_when_unset(self, make_project) -> None:
        assert one(make_project, "").unique_together == ()


class TestIndexes:
    def test_fields_and_name_are_read(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                indexes = [Index(fields=["reference", "created"], name="ref_idx")]
            """,
        )
        index = order.indexes[0]
        assert index.fields == ("reference", "created")
        assert index.name == "ref_idx"
        assert not index.conditional

    def test_a_condition_makes_it_partial(self, make_project) -> None:
        """Healthchecks indexes ``alert_after`` only for rows not already down.

        A partial index serves queries carrying the same predicate and nothing
        else, so counting it as coverage for a plain filter would report a
        table scan resolved when it is not.
        """
        order = one(
            make_project,
            """
            class Meta:
                indexes = [
                    Index(
                        fields=["created"],
                        name="live_idx",
                        condition=Q(reference=""),
                    )
                ]
            """,
        )
        index = order.indexes[0]
        assert index.conditional
        assert index.covers_exactly == ()

    def test_an_expression_index_covers_no_plain_column(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                indexes = [Index(Lower("reference"), name="lower_idx")]
            """,
        )
        index = order.indexes[0]
        assert index.expressions
        assert index.covers_exactly == ()

    def test_a_descending_column_still_names_the_field(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                indexes = [Index(fields=["-created"], name="recent_idx")]
            """,
        )
        assert order.indexes[0].covers_exactly == ("created",)


class TestConstraints:
    def test_a_unique_constraint_is_recognised(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                constraints = [
                    UniqueConstraint(fields=["reference"], name="uniq_ref")
                ]
            """,
        )
        constraint = order.constraints[0]
        assert constraint.is_unique
        assert constraint.fields == ("reference",)
        assert constraint.name == "uniq_ref"

    def test_a_check_constraint_is_not_unique(self, make_project) -> None:
        # A CheckConstraint creates no index. Conflating the two credits a
        # model with coverage it has not got.
        order = one(
            make_project,
            """
            class Meta:
                constraints = [
                    models.CheckConstraint(condition=Q(reference=""), name="ck")
                ]
            """,
        )
        constraint = order.constraints[0]
        assert constraint.kind == "CheckConstraint"
        assert not constraint.is_unique
        assert constraint.conditional

    def test_a_conditional_unique_constraint_is_flagged(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                constraints = [
                    UniqueConstraint(
                        fields=["reference"],
                        name="uniq_live",
                        condition=Q(created__isnull=False),
                    )
                ]
            """,
        )
        assert order.constraints[0].conditional

    def test_a_third_party_constraint_is_kept_by_its_name(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                constraints = [
                    ExclusionConstraint(name="no_overlap", expressions=[])
                ]
            """,
        )
        assert order.constraints[0].kind == "ExclusionConstraint"

    def test_a_non_call_element_is_skipped(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                constraints = [SHARED_CONSTRAINT]
            """,
        )
        assert order.constraints == ()


class TestIndexedFields:
    def test_it_unions_every_source(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models
            from django.db.models import Index, UniqueConstraint

            class Order(models.Model):
                reference = models.CharField(max_length=32, db_index=True)
                email = models.EmailField(unique=True)
                created = models.DateTimeField()
                status = models.CharField(max_length=8)
                note = models.TextField()

                class Meta:
                    indexes = [Index(fields=["created"], name="created_idx")]
                    constraints = [
                        UniqueConstraint(fields=["status"], name="uniq_status")
                    ]
            """,
        )
        assert model(graph, "shop.Order").indexed_fields == {
            "reference",
            "email",
            "created",
            "status",
        }

    def test_only_the_leading_column_of_a_composite_counts(self, make_project) -> None:
        # A btree on (a, b) serves a filter on a and does nothing for one on b
        # alone. Crediting the second column silences a real table scan.
        order = one(
            make_project,
            """
            class Meta:
                indexes = [Index(fields=["reference", "created"], name="ref_idx")]
            """,
        )
        assert order.indexed_fields == {"reference"}

    def test_a_partial_index_grants_no_coverage(self, make_project) -> None:
        order = one(
            make_project,
            """
            class Meta:
                indexes = [
                    Index(fields=["created"], name="i", condition=Q(reference=""))
                ]
            """,
        )
        assert order.indexed_fields == set()


class TestMetaInheritance:
    def test_an_inherited_meta_is_recorded(self, make_project) -> None:
        # Options we cannot see in this class body. Recording the base means a
        # later pass can resolve it instead of a rule silently assuming there
        # were none.
        graph = project(
            make_project,
            """
            from django.db import models

            class Base(models.Model):
                class Meta:
                    abstract = True
                    ordering = ["id"]

            class Order(Base):
                class Meta(Base.Meta):
                    db_table = "orders"
            """,
        )
        assert model(graph, "shop.Order").meta_bases == ("Meta",)

    def test_a_model_without_meta_has_none(self, make_project) -> None:
        assert one(make_project, "").meta_bases == ()


class TestIndexesDjangoCreatesWithoutBeingAsked:
    """Three defaults that differ from the blanket one, all measured.

    Building the tables for these fields emits `CREATE INDEX` for a plain
    `ForeignKey`, a `OneToOneField` and a `SlugField`, and nothing for a
    `ForeignKey(db_index=False)`. Reporting them as unindexed would make
    `indexed_fields` accuse every foreign-key filter of a table scan.
    """

    def test_a_foreign_key_is_indexed_without_saying_so(self, make_project):
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                name = models.CharField(max_length=50)

            class Book(models.Model):
                author = models.ForeignKey(Author, on_delete=models.CASCADE)
            """,
        )
        book = model(graph, "shop.Book")
        assert book.all_fields["author"].db_index is True
        assert "author" in book.indexed_fields

    def test_an_opted_out_foreign_key_is_not_indexed(self, make_project):
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                name = models.CharField(max_length=50)

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, db_index=False
                )
            """,
        )
        book = model(graph, "shop.Book")
        assert book.all_fields["author"].db_index is False
        assert "author" not in book.indexed_fields

    def test_a_one_to_one_is_unique_without_saying_so(self, make_project):
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                name = models.CharField(max_length=50)

            class Profile(models.Model):
                author = models.OneToOneField(Author, on_delete=models.CASCADE)
            """,
        )
        author = model(graph, "shop.Profile").all_fields["author"]
        assert author.unique is True
        assert author.db_index is True

    def test_a_slug_is_indexed_and_a_char_is_not(self, make_project):
        graph = project(
            make_project,
            """
            from django.db import models

            class Book(models.Model):
                slug = models.SlugField()
                title = models.CharField(max_length=50)
            """,
        )
        book = model(graph, "shop.Book")
        assert book.indexed_fields == {"slug"}
