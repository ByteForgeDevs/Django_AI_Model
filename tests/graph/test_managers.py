"""Managers, and what ``Model.objects`` actually is.

Every ORM question starts at a manager and on a real project it is rarely
Django's. 106 of NetBox's 142 concrete models reach the database through
``RestrictedQuerySet``, whose ``restrict()`` is how permission scoping happens
— so an authorization rule assuming ``objects`` is a plain ``Manager`` calls
all 106 of those viewsets unscoped. The opposite mistake is worse: a manager
overriding ``get_queryset`` returns less than the table, and a rule counting
what it returns is measuring the wrong set.
"""

from __future__ import annotations

from djaudit.graph.builder import build_model_graph
from djaudit.graph.nodes import ModelGraph, ModelNode


def model(graph: ModelGraph, ref: str) -> ModelNode:
    found = graph.get(ref)
    assert found is not None, f"{ref} not in graph: {sorted(graph.models)}"
    return found


def project(make_project, models_source: str, **extra: str) -> ModelGraph:
    files = {"shop/__init__.py": "", "shop/models.py": models_source, **extra}
    return build_model_graph(make_project(files))


class TestImplicitManager:
    def test_a_model_declaring_none_gets_objects(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                pass
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.name == "objects"
        assert manager.implicit
        assert manager.is_plain

    def test_declaring_one_suppresses_it(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class OrderManager(models.Manager):
                pass

            class Order(models.Model):
                things = OrderManager()
            """,
        )
        order = model(graph, "shop.Order")
        assert set(order.managers) == {"things"}

    def test_an_inherited_manager_suppresses_it_too(self, make_project) -> None:
        # ModelBase._prepare adds objects only when nothing was declared
        # anywhere in the MRO, which is why this waits for inheritance.
        graph = project(
            make_project,
            """
            from django.db import models

            class OrderManager(models.Manager):
                pass

            class Base(models.Model):
                things = OrderManager()

                class Meta:
                    abstract = True

            class Order(Base):
                pass
            """,
        )
        assert set(model(graph, "shop.Order").managers) == {"things"}

    def test_an_abstract_model_gets_nothing(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Base(models.Model):
                class Meta:
                    abstract = True
            """,
        )
        assert model(graph, "shop.Base").managers == {}


class TestRecognition:
    def test_as_manager_records_the_queryset(self, make_project) -> None:
        # NetBox's dominant form, and the queryset is the interesting half:
        # restrict() and any narrowing live there, not on the manager.
        graph = project(
            make_project,
            """
            from django.db import models

            class RestrictedQuerySet(models.QuerySet):
                def restrict(self, user):
                    return self

            class Order(models.Model):
                objects = RestrictedQuerySet.as_manager()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.queryset_class == "RestrictedQuerySet"
        assert not manager.implicit

    def test_from_queryset_records_it_as_well(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class OrderQuerySet(models.QuerySet):
                pass

            class Order(models.Model):
                objects = models.Manager.from_queryset(OrderQuerySet)()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.queryset_class == "OrderQuerySet"

    def test_a_manager_subclass_is_recognised_through_the_index(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Weird(models.Manager):
                pass

            class Order(models.Model):
                objects = Weird()
            """,
        )
        assert model(graph, "shop.Order").managers["objects"].manager_class == "Weird"

    def test_an_unseen_manager_is_recognised_by_its_name(self, make_project) -> None:
        # django-mptt's TreeManager is nine NetBox models' default and is not
        # in the checkout. The suffix convention is all that is left.
        graph = project(
            make_project,
            """
            from django.db import models
            from mptt.managers import TreeManager

            class Order(models.Model):
                objects = TreeManager()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.manager_class == "TreeManager"
        assert not manager.implicit

    def test_a_manager_composed_from_a_queryset_is_still_a_manager(self, make_project) -> None:
        """NetBox's ``IPAddressManager``, whose only base is a function call.

        ``class IPAddressManager(Manager.from_queryset(IPAddressQuerySet))``
        has no base we can read as a name, so the class looks like it descends
        from nothing at all.
        """
        graph = project(
            make_project,
            """
            from django.db import models

            class OrderQuerySet(models.QuerySet):
                pass

            class OrderManager(models.Manager.from_queryset(OrderQuerySet)):
                def get_queryset(self):
                    return super().get_queryset().order_by("id")

            class Order(models.Model):
                objects = OrderManager()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.manager_class == "OrderManager"
        assert manager.narrows

    def test_a_non_manager_assignment_is_not_one(self, make_project) -> None:
        # NetBox has `objects = {s.name: s for s in self.scripts.all()}` in a
        # model body. A name-based guess has no way to decline it.
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                registry = dict()
                label = str(1)
            """,
        )
        order = model(graph, "shop.Order")
        assert set(order.managers) == {"objects"}
        assert order.managers["objects"].implicit


class TestNarrowing:
    def test_an_overridden_get_queryset_is_flagged(self, make_project) -> None:
        # A soft-delete manager hiding rows means a rule counting its results
        # is measuring the wrong set.
        graph = project(
            make_project,
            """
            from django.db import models

            class LiveManager(models.Manager):
                def get_queryset(self):
                    return super().get_queryset().filter(deleted=False)

            class Order(models.Model):
                deleted = models.BooleanField(default=False)
                objects = LiveManager()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.narrows

    def test_it_is_inherited_from_the_managers_own_base(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class LiveManager(models.Manager):
                def get_queryset(self):
                    return super().get_queryset().filter(deleted=False)

            class OrderManager(LiveManager):
                pass

            class Order(models.Model):
                objects = OrderManager()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.narrows

    def test_a_plain_manager_does_not_narrow(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                objects = models.Manager()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.is_plain
        assert not manager.narrows


class TestWhichOneIsDefault:
    def test_the_first_declared_wins(self, make_project) -> None:
        # Django orders by (mro depth, creation counter), which for one class
        # is declaration order. Keeping it is why managers are not sorted.
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                published = models.Manager()
                everything = models.Manager()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.name == "published"

    def test_meta_default_manager_name_overrides_it(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                published = models.Manager()
                everything = models.Manager()

                class Meta:
                    default_manager_name = "everything"
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.name == "everything"

    def test_the_base_manager_is_tracked_separately(self, make_project) -> None:
        """Django follows a foreign key through ``_base_manager``, not the default.

        That is deliberate: a filtered default manager must not be able to make
        a related object vanish when something dereferences a key to it.
        """
        graph = project(
            make_project,
            """
            from django.db import models

            class LiveManager(models.Manager):
                def get_queryset(self):
                    return super().get_queryset().filter(deleted=False)

            class Order(models.Model):
                deleted = models.BooleanField(default=False)
                objects = LiveManager()
                everything = models.Manager()

                class Meta:
                    base_manager_name = "everything"
            """,
        )
        order = model(graph, "shop.Order")
        default = order.default_manager
        base = order.base_manager
        assert default is not None
        assert base is not None
        assert default.name == "objects"
        assert default.narrows
        assert base.name == "everything"
        assert not base.narrows

    def test_a_declared_manager_beats_an_inherited_one(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class BaseManager(models.Manager):
                pass

            class OwnManager(models.Manager):
                pass

            class Base(models.Model):
                objects = BaseManager()

                class Meta:
                    abstract = True

            class Order(Base):
                objects = OwnManager()
            """,
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.manager_class == "OwnManager"


class TestCrossModuleManagers:
    def test_a_manager_defined_in_another_module_resolves(self, make_project) -> None:
        # NetBox keeps IPAddressManager in ipam/managers.py and attaches it in
        # ipam/models/ip.py, which is the normal arrangement.
        graph = project(
            make_project,
            """
            from django.db import models
            from shop.managers import OrderManager

            class Order(models.Model):
                objects = OrderManager()
            """,
            **{
                "shop/managers.py": """
                    from django.db import models

                    class OrderManager(models.Manager):
                        def get_queryset(self):
                            return super().get_queryset().filter(open=True)
                """
            },
        )
        manager = model(graph, "shop.Order").default_manager
        assert manager is not None
        assert manager.dotted == "shop.managers.OrderManager"
        assert manager.narrows
