"""Reverse accessors, which Django names by four interacting rules.

Getting these wrong is not a cosmetic problem. A rule that follows a relation
backwards to check ownership has to name the accessor Django actually created,
and there are four ways to end up with a different one: ``related_name``,
``Meta.default_related_name``, the ``_set`` suffix that only applies when the
reverse side is multiple, and the placeholders an abstract base uses to give
each heir its own.

Confusing the accessor with the query name is the fifth. They fall back
differently, and a ``filter()`` built from the wrong one is a crash.
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


def project(make_project, models_source: str) -> ModelGraph:
    return build_model_graph(
        make_project({"shop/__init__.py": "", "shop/models.py": models_source})
    )


class TestDefaultAccessor:
    def test_a_foreign_key_gets_the_set_suffix(self, make_project) -> None:
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
        # author.book_set — many books per author.
        assert edge(graph, "shop.Book", "author").accessor == "book_set"

    def test_a_one_to_one_does_not(self, make_project) -> None:
        # Healthchecks reaches its Profile as project.owner.profile, and a
        # profile_set would be an AttributeError.
        graph = project(
            make_project,
            """
            from django.db import models

            class Account(models.Model):
                pass

            class Profile(models.Model):
                account = models.OneToOneField(Account, on_delete=models.CASCADE)
            """,
        )
        assert edge(graph, "shop.Profile", "account").accessor == "profile"

    def test_a_many_to_many_gets_the_suffix(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Tag(models.Model):
                pass

            class Book(models.Model):
                tags = models.ManyToManyField(Tag)
            """,
        )
        assert edge(graph, "shop.Book", "tags").accessor == "book_set"


class TestExplicitNaming:
    def test_related_name_wins(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="books"
                )
            """,
        )
        assert edge(graph, "shop.Book", "author").accessor == "books"

    def test_meta_default_related_name_applies_to_the_declaring_model(self, make_project) -> None:
        # Easy to get backwards: the option lives on the model declaring the
        # foreign key and names the relation *back* to it.
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(Author, on_delete=models.CASCADE)

                class Meta:
                    default_related_name = "books"
            """,
        )
        assert edge(graph, "shop.Book", "author").accessor == "books"

    def test_related_name_beats_the_meta_default(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="written"
                )

                class Meta:
                    default_related_name = "books"
            """,
        )
        assert edge(graph, "shop.Book", "author").accessor == "written"


class TestNoReverseRelationAtAll:
    def test_a_plus_suffix_hides_it(self, make_project) -> None:
        # NetBox does this seventeen times. Following a reverse relation that
        # does not exist is how a query-path rule invents an ORM error.
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="+"
                )
            """,
        )
        author = edge(graph, "shop.Book", "author")
        assert author.hidden
        assert author.accessor is None

    def test_a_named_plus_also_hides_it(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="ignored+"
                )
            """,
        )
        assert edge(graph, "shop.Book", "author").accessor is None

    def test_a_symmetrical_self_m2m_has_no_accessor(self, make_project) -> None:
        # symmetrical defaults to True for a relation to self, and a
        # symmetrical relation is its own reverse.
        graph = project(
            make_project,
            """
            from django.db import models

            class Person(models.Model):
                friends = models.ManyToManyField("self")
            """,
        )
        friends = edge(graph, "shop.Person", "friends")
        assert friends.symmetrical
        assert friends.accessor is None

    def test_an_asymmetrical_self_m2m_does(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Person(models.Model):
                follows = models.ManyToManyField("self", symmetrical=False)
            """,
        )
        assert edge(graph, "shop.Person", "follows").accessor == "person_set"

    def test_a_hidden_relation_is_not_in_the_reverse_index(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="+"
                )
            """,
        )
        assert "shop.Author" not in graph.incoming


class TestQueryNameIsNotTheAccessor:
    def test_they_diverge_by_default(self, make_project) -> None:
        # author.book_set is the accessor; Author.objects.filter(book__...) is
        # the query name. Django's fallbacks differ by exactly the suffix.
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
        author = edge(graph, "shop.Book", "author")
        assert author.accessor == "book_set"
        assert author.query_name == "book"

    def test_related_query_name_overrides_independently(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author,
                    on_delete=models.CASCADE,
                    related_name="books",
                    related_query_name="volume",
                )
            """,
        )
        author = edge(graph, "shop.Book", "author")
        assert author.accessor == "books"
        assert author.query_name == "volume"

    def test_a_hidden_relation_still_has_a_query_name(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="+"
                )
            """,
        )
        assert edge(graph, "shop.Book", "author").query_name == "book"


class TestPlaceholders:
    def test_class_is_expanded_against_the_owner(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="%(class)s_items"
                )
            """,
        )
        assert edge(graph, "shop.Book", "author").accessor == "book_items"

    def test_an_abstract_base_keeps_its_placeholder(self, make_project) -> None:
        # The reason placeholders exist: one declaration on an abstract base, a
        # distinct accessor on every heir. Django expands them per-heir and
        # gives the base itself no reverse relation at all, so naming one here
        # would invent an accessor nothing can use. The heirs get theirs when
        # inheritance is resolved.
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Base(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="%(class)s_items"
                )

                class Meta:
                    abstract = True
            """,
        )
        base = edge(graph, "shop.Base", "author")
        assert base.related_name == "%(class)s_items"
        assert base.accessor is None
        assert base.query_name is None

    def test_an_abstract_base_is_not_in_the_reverse_index(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Base(models.Model):
                author = models.ForeignKey(Author, on_delete=models.CASCADE)

                class Meta:
                    abstract = True
            """,
        )
        assert graph.incoming == {}

    def test_app_label_is_expanded(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author,
                    on_delete=models.CASCADE,
                    related_name="%(app_label)s_%(model_name)s",
                )
            """,
        )
        assert edge(graph, "shop.Book", "author").accessor == "shop_book"

    def test_a_stray_percent_is_left_alone(self, make_project) -> None:
        # Django would raise here too. Discovering that is not our job, and
        # crashing the audit over it certainly is not.
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(
                    Author, on_delete=models.CASCADE, related_name="100%_owned"
                )
            """,
        )
        assert edge(graph, "shop.Book", "author").accessor == "100%_owned"


class TestReverseIndex:
    def test_edges_are_indexed_by_target(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(Author, on_delete=models.CASCADE)

            class Review(models.Model):
                author = models.ForeignKey(Author, on_delete=models.CASCADE)
            """,
        )
        assert sorted(e.source for e in graph.incoming["shop.Author"]) == [
            "shop.Book",
            "shop.Review",
        ]

    def test_an_unresolved_target_is_not_indexed(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Book(models.Model):
                kind = models.ForeignKey(
                    "contenttypes.ContentType", on_delete=models.CASCADE
                )
            """,
        )
        assert graph.incoming == {}


class TestRealProjects:
    def test_healthchecks_accessors(self, make_project) -> None:
        """The shapes Healthchecks actually uses, reproduced exactly.

        ``front/views.py`` reaches ``project.owner.profile``, so the one-to-one
        must not gain a ``_set``; the two ``related_name`` relations must win
        over the default; and the plain foreign key must keep it.
        """
        graph = project(
            make_project,
            """
            from django.contrib.auth.models import User
            from django.db import models

            class Profile(models.Model):
                user = models.OneToOneField(User, models.CASCADE)

            class Project(models.Model):
                owner = models.ForeignKey(User, models.CASCADE)

            class Member(models.Model):
                user = models.ForeignKey(
                    User, models.CASCADE, related_name="memberships"
                )
                project = models.ForeignKey(Project, models.CASCADE)
            """,
        )
        assert edge(graph, "shop.Profile", "user").accessor == "profile"
        assert edge(graph, "shop.Project", "owner").accessor == "project_set"
        assert edge(graph, "shop.Member", "user").accessor == "memberships"
        assert edge(graph, "shop.Member", "project").accessor == "member_set"
