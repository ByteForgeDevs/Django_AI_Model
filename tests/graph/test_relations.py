"""Relations, and the five ways Django lets you name the other end.

The tests that matter here are the ones about the user model. Every
authorization rule in this phase is a question about ownership, ownership
means a path to the user, and a graph that recognises four of the five
spellings goes quiet on the fifth — which will be the one that mattered.
"""

from __future__ import annotations

from djaudit.graph.builder import build_model_graph
from djaudit.graph.nodes import ModelGraph, ModelNode, RelationEdge


def model(graph: ModelGraph, ref: str) -> ModelNode:
    found = graph.get(ref)
    assert found is not None, f"{ref} not in graph: {sorted(graph.models)}"
    return found


def edge(graph: ModelGraph, ref: str, field: str) -> RelationEdge:
    found = next((e for e in model(graph, ref).relations if e.field_name == field), None)
    assert found is not None, f"{ref} has no relation {field}"
    return found


def project(make_project, models_source: str, settings_source: str = "") -> ModelGraph:
    files = {
        "shop/__init__.py": "",
        "shop/models.py": models_source,
    }
    if settings_source:
        files["config/__init__.py"] = ""
        files["config/settings.py"] = settings_source
    return build_model_graph(make_project(files))


class TestTargets:
    def test_a_direct_class_reference(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(Author, on_delete=models.CASCADE)
            """,
        )
        assert edge(graph, "shop.Book", "author").target == "shop.Author"

    def test_a_bare_string_reference(self, make_project) -> None:
        # Written before the target class exists, which is why strings are
        # allowed at all.
        graph = project(
            make_project,
            """
            from django.db import models

            class Book(models.Model):
                author = models.ForeignKey("Author", on_delete=models.CASCADE)

            class Author(models.Model):
                pass
            """,
        )
        assert edge(graph, "shop.Book", "author").target == "shop.Author"

    def test_a_qualified_string_reference(self, make_project) -> None:
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Book(models.Model):
                    author = models.ForeignKey("people.Author", on_delete=models.CASCADE)
                """,
                "people/__init__.py": "",
                "people/models.py": """
                from django.db import models

                class Author(models.Model):
                    pass
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert edge(graph, "shop.Book", "author").target == "people.Author"

    def test_self(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Category(models.Model):
                parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE)
            """,
        )
        parent = edge(graph, "shop.Category", "parent")
        assert parent.is_self
        assert parent.target == "shop.Category"

    def test_the_to_keyword(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(to=Author, on_delete=models.CASCADE)
            """,
        )
        assert edge(graph, "shop.Book", "author").target == "shop.Author"

    def test_a_target_we_do_not_have_is_not_invented(self, make_project) -> None:
        # contenttypes.ContentType is real, referenced constantly, and never in
        # the repository. NetBox alone has eleven of these.
        graph = project(
            make_project,
            """
            from django.db import models

            class Note(models.Model):
                kind = models.ForeignKey(
                    "contenttypes.ContentType", on_delete=models.CASCADE
                )
            """,
        )
        kind = edge(graph, "shop.Note", "kind")
        assert kind.target_ref == "contenttypes.ContentType"
        assert not kind.resolved


class TestTheUserModel:
    def test_settings_auth_user_model(self, make_project) -> None:
        # The correct spelling, and the one that says nothing on its own.
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Order(models.Model):
                owner = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )
            """,
        )
        owner = edge(graph, "shop.Order", "owner")
        assert owner.via_user_setting
        assert owner.points_at_user

    def test_a_swapped_user_model_is_read_from_settings(self, make_project) -> None:
        ctx = make_project(
            {
                "config/__init__.py": "",
                "config/settings.py": """
                SECRET_KEY = "x"
                INSTALLED_APPS = ["accounts", "shop"]
                AUTH_USER_MODEL = "accounts.Person"
                """,
                "accounts/__init__.py": "",
                "accounts/models.py": """
                from django.db import models

                class Person(models.Model):
                    pass
                """,
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.conf import settings
                from django.db import models

                class Order(models.Model):
                    owner = models.ForeignKey(
                        settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                    )
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert graph.user_model == "accounts.Person"
        assert edge(graph, "shop.Order", "owner").target == "accounts.Person"

    def test_the_default_when_nothing_swaps_it(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                pass
            """,
        )
        assert graph.user_model == "auth.User"

    def test_a_plain_reference_to_djangos_user_still_counts(self, make_project) -> None:
        # Healthchecks writes every one of its five user relations this way.
        # auth.User is not in the repository, so the edge cannot resolve -- but
        # it points at the user just as surely as the settings spelling does.
        graph = project(
            make_project,
            """
            from django.contrib.auth.models import User
            from django.db import models

            class Profile(models.Model):
                user = models.OneToOneField(User, on_delete=models.CASCADE)
            """,
        )
        user = edge(graph, "shop.Profile", "user")
        assert not user.resolved
        assert user.points_at_user

    def test_a_different_model_called_user_is_not_the_user(self, make_project) -> None:
        # The bare-name fallback only fires on an unresolved reference, so a
        # project with its own User elsewhere resolves to that model instead.
        ctx = make_project(
            {
                "config/__init__.py": "",
                "config/settings.py": """
                SECRET_KEY = "x"
                INSTALLED_APPS = ["accounts", "shop"]
                AUTH_USER_MODEL = "accounts.Person"
                """,
                "accounts/__init__.py": "",
                "accounts/models.py": """
                from django.db import models

                class Person(models.Model):
                    pass
                """,
                "legacy/__init__.py": "",
                "legacy/models.py": """
                from django.db import models

                class User(models.Model):
                    pass
                """,
                "shop/__init__.py": "",
                "shop/models.py": """
                from django.db import models

                class Order(models.Model):
                    who = models.ForeignKey("legacy.User", on_delete=models.CASCADE)
                """,
            }
        )
        graph = build_model_graph(ctx)
        who = edge(graph, "shop.Order", "who")
        assert who.target == "legacy.User"
        assert not who.points_at_user


class TestKindAndOptions:
    def test_on_delete_by_keyword_and_position(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                a = models.ForeignKey(Author, on_delete=models.PROTECT)
                b = models.ForeignKey(Author, models.SET_NULL, null=True)
                c = models.ForeignKey(Author, on_delete=models.SET(1))
            """,
        )
        assert edge(graph, "shop.Book", "a").on_delete == "PROTECT"
        assert edge(graph, "shop.Book", "b").on_delete == "SET_NULL"
        assert edge(graph, "shop.Book", "c").on_delete == "SET"

    def test_many_to_many_has_no_on_delete_and_may_have_through(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Tag(models.Model):
                pass

            class Book(models.Model):
                tags = models.ManyToManyField(Tag, through="Tagging")

            class Tagging(models.Model):
                pass
            """,
        )
        tags = edge(graph, "shop.Book", "tags")
        assert tags.on_delete is None
        assert tags.through == "Tagging"
        assert tags.is_multi

    def test_one_to_one_is_not_multi(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Profile(models.Model):
                pass

            class Account(models.Model):
                profile = models.OneToOneField(Profile, on_delete=models.CASCADE)
            """,
        )
        assert not edge(graph, "shop.Account", "profile").is_multi


class TestRealProjects:
    def test_the_fixture_project(self, vulnerable_project) -> None:
        from djaudit.discovery import build_context

        graph = build_context(vulnerable_project).model_graph
        author = edge(graph, "app.Book", "author")
        assert author.target == "app.Author"
        assert author.on_delete == "CASCADE"
        assert not author.points_at_user
