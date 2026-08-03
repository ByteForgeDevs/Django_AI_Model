"""Inheritance across module boundaries.

Reading one file at a time is enough for a tutorial project and useless on a
real one. NetBox declares 185 models and almost none of them names
`models.Model`: they inherit through a `PrimaryModel`/`NetBoxModel` chain
layered over a dozen feature mixins, re-exported through star imports, spread
over three packages. Seen one file at a time that is 63 models with most of
their columns missing.

The distinctions that decide correctness are all here: abstract bases copy
columns down, concrete ones do not, proxies share a table, and a plain mixin
contributes nothing at all despite sitting in the same base list.
"""

from __future__ import annotations

from djaudit.graph.builder import build_model_graph
from djaudit.graph.inheritance import absolute, package_dotted
from djaudit.graph.nodes import ModelGraph, ModelNode


def model(graph: ModelGraph, ref: str) -> ModelNode:
    found = graph.get(ref)
    assert found is not None, f"{ref} not in graph: {sorted(graph.models)}"
    return found


class TestDottedNames:
    def test_a_module_is_named_from_its_package_root(self, tmp_path) -> None:
        # NetBox's code sits at <root>/netbox/netbox/models/, which every
        # import in the project calls netbox.models. Anchoring on the project
        # root would call it netbox.netbox.models and resolve nothing.
        pkg = tmp_path / "netbox" / "netbox" / "models"
        pkg.mkdir(parents=True)
        (pkg.parent / "__init__.py").touch()
        (pkg / "__init__.py").touch()
        assert package_dotted(pkg / "__init__.py") == "netbox.models"

    def test_a_plain_module_keeps_its_own_name(self, tmp_path) -> None:
        pkg = tmp_path / "shop"
        pkg.mkdir()
        (pkg / "__init__.py").touch()
        (pkg / "models.py").touch()
        assert package_dotted(pkg / "models.py") == "shop.models"


class TestRelativeImports:
    def test_one_dot_from_a_module_means_its_package(self) -> None:
        # NetBox writes this in dcim/models/power.py and means
        # dcim.models.device_components.
        assert (
            absolute(".device_components.Cabled", "dcim.models.power")
            == "dcim.models.device_components.Cabled"
        )

    def test_one_dot_from_a_package_means_itself(self) -> None:
        # The same line inside dcim/models/__init__.py means the same module,
        # while starting one component shorter -- the package *is* dcim.models.
        assert (
            absolute(".device_components", "dcim.models", is_package=True)
            == "dcim.models.device_components"
        )

    def test_two_dots_climb_a_level(self) -> None:
        assert absolute("..utils.Thing", "dcim.models.power") == "dcim.utils.Thing"

    def test_an_absolute_name_is_untouched(self) -> None:
        assert absolute("netbox.models.PrimaryModel", "dcim.models") == (
            "netbox.models.PrimaryModel"
        )


class TestCrossModuleRecognition:
    def test_a_model_is_found_through_a_base_in_another_app(self, make_project) -> None:
        graph = build_model_graph(
            make_project(
                {
                    "common/__init__.py": "",
                    "common/models.py": """
                        from django.db import models

                        class TimestampedModel(models.Model):
                            created = models.DateTimeField(auto_now_add=True)

                            class Meta:
                                abstract = True
                    """,
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from common.models import TimestampedModel

                        class Order(TimestampedModel):
                            pass
                    """,
                }
            )
        )
        order = model(graph, "shop.Order")
        assert order.mro == ("common.TimestampedModel",)
        assert "created" in order.inherited

    def test_it_survives_a_star_import_re_export(self, make_project) -> None:
        """NetBox's ``netbox/models/__init__.py`` is nearly all star imports.

        A star import binds no name we can see in the importing file, so the
        only way to know whether it supplies a base class is to look in the
        module it names. Without that, everything under ``PrimaryModel`` --
        most of NetBox -- is invisible.
        """
        graph = build_model_graph(
            make_project(
                {
                    "common/__init__.py": "",
                    "common/models/__init__.py": "from common.models.features import *",
                    "common/models/features.py": """
                        from django.db import models

                        class ChangeLoggingMixin(models.Model):
                            last_updated = models.DateTimeField(auto_now=True)

                            class Meta:
                                abstract = True
                    """,
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from common.models import ChangeLoggingMixin

                        class Order(ChangeLoggingMixin):
                            pass
                    """,
                }
            )
        )
        assert "last_updated" in model(graph, "shop.Order").inherited

    def test_a_relative_import_between_sibling_modules_resolves(self, make_project) -> None:
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models/__init__.py": "",
                    "shop/models/base.py": """
                        from django.db import models

                        class Cabled(models.Model):
                            cable = models.CharField(max_length=32)

                            class Meta:
                                abstract = True
                    """,
                    "shop/models/power.py": """
                        from .base import Cabled

                        class PowerFeed(Cabled):
                            pass
                    """,
                }
            )
        )
        assert "cable" in model(graph, "shop.PowerFeed").inherited

    def test_a_missing_base_is_reported_not_assumed(self, make_project) -> None:
        # django-mptt and django-taggit are not in the checkout, and inventing
        # what MPTTModel contributes would be worse than admitting we cannot
        # see it.
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models
                        from mptt.models import MPTTModel

                        class Category(MPTTModel, models.Model):
                            name = models.CharField(max_length=32)
                    """,
                }
            )
        )
        assert graph.unresolved_bases["shop.Category"] == ("MPTTModel",)


class TestAbstractBases:
    def test_columns_are_copied_to_every_heir(self, make_project) -> None:
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Base(models.Model):
                            created = models.DateTimeField(auto_now_add=True)

                            class Meta:
                                abstract = True

                        class Order(Base):
                            reference = models.CharField(max_length=32)

                        class Invoice(Base):
                            pass
                    """,
                }
            )
        )
        order = model(graph, "shop.Order")
        assert set(order.all_fields) == {"reference", "created"}
        assert set(order.fields) == {"reference"}
        assert "created" in model(graph, "shop.Invoice").inherited

    def test_a_declared_field_beats_an_inherited_one(self, make_project) -> None:
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Base(models.Model):
                            name = models.CharField(max_length=10)

                            class Meta:
                                abstract = True

                        class Order(Base):
                            name = models.TextField()
                    """,
                }
            )
        )
        assert model(graph, "shop.Order").all_fields["name"].kind == "TextField"

    def test_the_nearest_base_wins_between_two(self, make_project) -> None:
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class First(models.Model):
                            name = models.CharField(max_length=10)

                            class Meta:
                                abstract = True

                        class Second(models.Model):
                            name = models.TextField()

                            class Meta:
                                abstract = True

                        class Order(First, Second):
                            pass
                    """,
                }
            )
        )
        assert model(graph, "shop.Order").inherited["name"].kind == "CharField"


class TestInheritedRelations:
    def test_a_relation_is_copied_and_renamed_for_the_heir(self, make_project) -> None:
        """Why ``%(class)s`` exists.

        One declaration on the base, and two heirs that must not both claim
        ``author.items`` -- Django would refuse to start.
        """
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Author(models.Model):
                            pass

                        class Base(models.Model):
                            author = models.ForeignKey(
                                Author,
                                on_delete=models.CASCADE,
                                related_name="%(class)s_items",
                            )

                            class Meta:
                                abstract = True

                        class Book(Base):
                            pass

                        class Review(Base):
                            pass
                    """,
                }
            )
        )
        accessors = {e.source: e.accessor for e in graph.incoming["shop.Author"]}
        assert accessors == {"shop.Book": "book_items", "shop.Review": "review_items"}

    def test_an_inherited_relation_records_where_it_came_from(self, make_project) -> None:
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Author(models.Model):
                            pass

                        class Base(models.Model):
                            author = models.ForeignKey(Author, on_delete=models.CASCADE)

                            class Meta:
                                abstract = True

                        class Book(Base):
                            pass
                    """,
                }
            )
        )
        edge = model(graph, "shop.Book").relations[0]
        assert edge.inherited_from == "shop.Base"
        assert edge.source == "shop.Book"
        assert edge.accessor == "book_set"


class TestConcreteInheritance:
    def test_multi_table_inheritance_adds_a_parent_link(self, make_project) -> None:
        # Restaurant(Place) keeps its own table and reaches Place's columns
        # over an implicit one-to-one. A graph that omits the link cannot
        # explain the join every query on the child performs.
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Place(models.Model):
                            address = models.CharField(max_length=64)

                        class Restaurant(Place):
                            menu = models.TextField()
                    """,
                }
            )
        )
        restaurant = model(graph, "shop.Restaurant")
        assert restaurant.parents == ("shop.Place",)
        assert restaurant.db_table == "shop_restaurant"

        link = restaurant.all_fields["place_ptr"]
        assert link.kind == "OneToOneField"
        assert link.primary_key

        edge = next(e for e in restaurant.relations if e.field_name == "place_ptr")
        assert edge.target == "shop.Place"
        assert edge.implicit
        assert edge.accessor == "restaurant"

    def test_a_concrete_parents_columns_are_not_copied_down(self, make_project) -> None:
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Place(models.Model):
                            address = models.CharField(max_length=64)

                        class Restaurant(Place):
                            menu = models.TextField()
                    """,
                }
            )
        )
        # address lives in shop_place. Claiming it as a column of
        # shop_restaurant would have a migration rule alter the wrong table.
        assert "address" not in model(graph, "shop.Restaurant").all_fields


class TestProxies:
    def test_a_proxy_shares_its_parents_table_and_columns(self, make_project) -> None:
        # NetBox's UserToken and ScriptModule are both this, and both looked
        # exactly like multi-table inheritance until Meta.proxy was read.
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Token(models.Model):
                            key = models.CharField(max_length=64)

                        class UserToken(Token):
                            class Meta:
                                proxy = True
                    """,
                }
            )
        )
        proxy = model(graph, "shop.UserToken")
        assert proxy.db_table == "shop_token"
        assert "key" in proxy.all_fields
        assert "token_ptr" not in proxy.all_fields

    def test_a_proxy_adds_no_reverse_relation(self, make_project) -> None:
        # The accessor belongs to the concrete model. Two classes claiming
        # author.token_set is a name Django never created twice.
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Author(models.Model):
                            pass

                        class Token(models.Model):
                            author = models.ForeignKey(Author, on_delete=models.CASCADE)

                        class UserToken(Token):
                            class Meta:
                                proxy = True
                    """,
                }
            )
        )
        assert [e.source for e in graph.incoming["shop.Author"]] == ["shop.Token"]


class TestPlainMixins:
    def test_a_non_model_mixin_contributes_nothing(self, make_project) -> None:
        """NetBox's ``TrackingModelMixin``, seven models deep in ``dcim``.

        A plain class in a model's base list is not a model. Django never
        contributes its attributes as fields, and treating it as a concrete
        ancestor invents both a table and the join to reach it.
        """
        graph = build_model_graph(
            make_project(
                {
                    "utilities/__init__.py": "",
                    "utilities/tracking.py": """
                        class TrackingModelMixin:
                            tracker_class = None
                    """,
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models
                        from utilities.tracking import TrackingModelMixin

                        class Order(TrackingModelMixin, models.Model):
                            reference = models.CharField(max_length=32)
                    """,
                }
            )
        )
        order = model(graph, "shop.Order")
        assert order.parents == ()
        assert order.mro == ()
        assert set(order.all_fields) == {"reference"}
        # Resolved, just not a model -- so not something we failed to read.
        assert "shop.Order" not in graph.unresolved_bases


class TestInheritedMeta:
    def test_ordering_comes_from_an_abstract_base(self, make_project) -> None:
        # Django rebuilds Meta from the base when the heir declares none.
        # Reading only the heir reports a model with no default ordering when
        # every query it makes is sorted.
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Base(models.Model):
                            class Meta:
                                abstract = True
                                ordering = ["-created"]

                        class Order(Base):
                            created = models.DateTimeField()
                    """,
                }
            )
        )
        assert model(graph, "shop.Order").ordering == ("-created",)

    def test_the_heir_overrides_it(self, make_project) -> None:
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Base(models.Model):
                            class Meta:
                                abstract = True
                                ordering = ["-created"]

                        class Order(Base):
                            created = models.DateTimeField()

                            class Meta:
                                ordering = ["created"]
                    """,
                }
            )
        )
        assert model(graph, "shop.Order").ordering == ("created",)

    def test_an_heir_is_not_abstract_by_inheritance(self, make_project) -> None:
        # The one Meta option Django deliberately does not inherit. If it did,
        # no project would have any tables.
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Base(models.Model):
                            class Meta:
                                abstract = True

                        class Order(Base):
                            pass
                    """,
                }
            )
        )
        order = model(graph, "shop.Order")
        assert not order.is_abstract
        assert order.db_table == "shop_order"


class TestCycles:
    def test_a_self_referential_base_does_not_hang(self, make_project) -> None:
        # Not valid Python, but a graph that recurses forever on it is a
        # crash on a file we were only meant to read.
        graph = build_model_graph(
            make_project(
                {
                    "shop/__init__.py": "",
                    "shop/models.py": """
                        from django.db import models

                        class Order(Order):
                            pass

                        class Real(models.Model):
                            pass
                    """,
                }
            )
        )
        assert graph.get("shop.Real") is not None
