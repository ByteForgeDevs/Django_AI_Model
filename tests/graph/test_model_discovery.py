"""Finding the models, and getting their identity right.

Everything in Phase 2 hangs off knowing which classes are models and what
Django calls them, so these tests are mostly about the two ways that goes
wrong: mistaking something for a model, and failing to recognise one because
of how the import was written.
"""

from __future__ import annotations

from djaudit.graph.builder import app_dir_for, build_model_graph, is_model_module
from djaudit.graph.nodes import ModelGraph, ModelNode


def model(graph: ModelGraph, ref: str, *, app_label: str | None = None) -> ModelNode:
    """Look up a model that the test asserts must exist."""
    found = graph.get(ref, app_label=app_label)
    assert found is not None, f"{ref} not in graph: {sorted(graph.models)}"
    return found


class TestRecognition:
    def test_finds_a_plain_model(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Order(models.Model):
                    pass
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert len(graph) == 1
        assert graph.get("shop.Order") is not None

    def test_follows_the_import_alias(self, make_project) -> None:
        # Matching the string "models.Model" would miss all three of these.
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models as db
                from django.db.models import Model
                import django.db.models as m

                class A(db.Model):
                    pass

                class B(Model):
                    pass

                class C(m.Model):
                    pass
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert sorted(m.name for m in graph) == ["A", "B", "C"]

    def test_ignores_a_class_that_only_looks_like_one(self, make_project) -> None:
        # ``Model`` is a popular class name. Without the import it is evidence
        # of nothing, and treating it as a model would put a table where the
        # project has a pydantic schema.
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from pydantic import BaseModel as Model

                class Order(Model):
                    pass
                """,
            }
        )
        assert len(build_model_graph(ctx)) == 0

    def test_follows_a_base_defined_in_the_same_module(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Timestamped(models.Model):
                    class Meta:
                        abstract = True

                class Order(Timestamped):
                    pass
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert {m.name for m in graph} == {"Timestamped", "Order"}
        assert model(graph, "shop.Timestamped").is_abstract
        assert not model(graph, "shop.Order").is_abstract

    def test_records_a_base_it_could_not_place(self, make_project) -> None:
        # Cross-module ancestry is substep 2.1.6. Until then the unknown base
        # is remembered, because a model whose parents we cannot see is a model
        # whose fields we may be missing.
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models
                from common.mixins import Auditable

                class Order(Auditable, models.Model):
                    pass
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert graph.unresolved_bases["shop.Order"] == ("Auditable",)

    def test_finds_a_conditionally_defined_model(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                try:
                    class Legacy(models.Model):
                        pass
                except ImportError:
                    pass
                """,
            }
        )
        assert build_model_graph(ctx).get("shop.Legacy") is not None


class TestWhereModelsLive:
    def test_a_models_package_is_one_app(self) -> None:
        # NetBox splits every app this way, so getting it wrong would give
        # every model there an app label of "models".
        from pathlib import Path

        assert app_dir_for(Path("/p/dcim/models/racks.py")) == Path("/p/dcim/models")
        assert app_dir_for(Path("/p/dcim/models.py")) == Path("/p/dcim")

    def test_migrations_are_not_models(self) -> None:
        from pathlib import Path

        assert not is_model_module(Path("/p/shop/migrations/0001_initial.py"))

    def test_a_models_package_module_is(self, make_project) -> None:
        ctx = make_project(
            {
                "dcim/__init__.py": "",
                "dcim/models/__init__.py": "",
                "dcim/models/racks.py": """
                from django.db import models

                class Rack(models.Model):
                    pass
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert graph.get("dcim.Rack") is not None

    def test_a_class_outside_a_models_module_is_skipped(self, make_project) -> None:
        # Django imports each app's models module and nothing else, so a class
        # defined elsewhere never gets a table however it is written.
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/helpers.py": """
                from django.db import models

                class Draft(models.Model):
                    pass
                """,
            }
        )
        assert len(build_model_graph(ctx)) == 0

    def test_historical_models_in_migrations_are_skipped(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": "",
                "shop/migrations/__init__.py": "",
                "shop/migrations/0001_initial.py": """
                from django.db import models

                class Order(models.Model):
                    pass
                """,
            }
        )
        assert len(build_model_graph(ctx)) == 0


class TestAppLabel:
    def test_defaults_to_the_package_name(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Order(models.Model):
                    pass
                """,
            }
        )
        assert model(build_model_graph(ctx), "shop.Order").app_label == "shop"

    def test_an_appconfig_can_override_it(self, make_project) -> None:
        # Two installed apps whose directories share a name have to do this,
        # and the label is what every string reference in the project uses.
        ctx = make_project(
            {
                "vendor/shop/__init__.py": "",
                "vendor/shop/apps.py": """
                from django.apps import AppConfig

                class ShopConfig(AppConfig):
                    name = "vendor.shop"
                    label = "vendor_shop"
                """,
                "vendor/shop/models.py": """
                from django.db import models

                class Order(models.Model):
                    pass
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert graph.get("vendor_shop.Order") is not None
        assert graph.get("shop.Order") is None

    def test_a_dotted_name_falls_back_to_its_tail(self, make_project) -> None:
        ctx = make_project(
            {
                "vendor/shop/__init__.py": "",
                "vendor/shop/apps.py": """
                from django.apps import AppConfig

                class ShopConfig(AppConfig):
                    name = "vendor.shop"
                """,
                "vendor/shop/models.py": """
                from django.db import models

                class Order(models.Model):
                    pass
                """,
            }
        )
        assert build_model_graph(ctx).get("shop.Order") is not None


class TestMetaFlags:
    def test_reads_proxy_and_managed(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Order(models.Model):
                    pass

                class RecentOrder(Order):
                    class Meta:
                        proxy = True

                class LegacyRow(models.Model):
                    class Meta:
                        managed = False
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert model(graph, "shop.RecentOrder").is_proxy
        assert not model(graph, "shop.LegacyRow").managed
        assert model(graph, "shop.Order").managed

    def test_reads_swappable(self, make_project) -> None:
        # This is how a custom user model announces itself without an import.
        ctx = make_project(
            {
                "accounts/__init__.py": "",
                "accounts/models.py": """
                from django.db import models

                class User(models.Model):
                    class Meta:
                        swappable = "AUTH_USER_MODEL"
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert model(graph, "accounts.User").swappable == "AUTH_USER_MODEL"

    def test_a_computed_flag_is_not_guessed(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                import os
                from django.db import models

                class Order(models.Model):
                    class Meta:
                        managed = os.environ.get("MANAGED") == "1"
                """,
            }
        )
        # Django's default is True and we cannot show otherwise, so the graph
        # says True rather than inventing a value from an unreadable expression.
        assert model(build_model_graph(ctx), "shop.Order").managed


class TestGraphLookups:
    def _two_apps(self, make_project):
        return make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Order(models.Model):
                    pass

                class Comment(models.Model):
                    pass
                """,
                "blog/__init__.py": "",
                "blog/models.py": """
                from django.db import models

                class Comment(models.Model):
                    pass
                """,
            }
        )

    def test_a_qualified_reference_is_exact(self, make_project) -> None:
        graph = build_model_graph(self._two_apps(make_project))
        assert model(graph, "blog.Comment").app_label == "blog"

    def test_a_bare_name_prefers_the_local_app(self, make_project) -> None:
        graph = build_model_graph(self._two_apps(make_project))
        assert model(graph, "Comment", app_label="shop").app_label == "shop"

    def test_an_ambiguous_bare_name_resolves_to_nothing(self, make_project) -> None:
        # Two apps with a Comment each is ordinary. Picking one would put every
        # downstream finding on the wrong model, which is worse than silence.
        graph = build_model_graph(self._two_apps(make_project))
        assert graph.get("Comment") is None

    def test_an_unambiguous_bare_name_still_resolves(self, make_project) -> None:
        graph = build_model_graph(self._two_apps(make_project))
        assert model(graph, "Order").label == "shop.Order"

    def test_concrete_excludes_abstract_and_proxy(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Base(models.Model):
                    class Meta:
                        abstract = True

                class Order(Base):
                    pass

                class RecentOrder(Order):
                    class Meta:
                        proxy = True
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert [m.name for m in graph.concrete] == ["Order"]

    def test_apps_and_by_app(self, make_project) -> None:
        graph = build_model_graph(self._two_apps(make_project))
        assert graph.apps == ["blog", "shop"]
        assert [m.name for m in graph.by_app("shop")] == ["Comment", "Order"]


class TestContextIntegration:
    def test_the_graph_is_built_once(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Order(models.Model):
                    pass
                """,
            }
        )
        assert ctx.model_graph is ctx.model_graph
        assert len(ctx.model_graph) == 1

    def test_it_reads_the_existing_fixture_project(self, vulnerable_project) -> None:
        # The Phase 0 fixture already carried two models as a placeholder for
        # exactly this work, so it doubles as the first end-to-end check.
        from djaudit.discovery import build_context

        graph = build_context(vulnerable_project).model_graph
        assert sorted(m.label for m in graph) == ["app.Author", "app.Book"]

    def test_a_project_with_no_models_is_an_empty_graph(self, api_project) -> None:
        from djaudit.discovery import build_context

        assert len(build_context(api_project).model_graph) == 0
