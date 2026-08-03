"""Walking the graph to the user, which is the question every IDOR rule asks.

A rule cannot say "this viewset is unscoped" without first knowing what
scoping would even look like. `Check` is reached through `project__owner`, so
that is the lookup a `get_queryset` override must contain; a rule that cannot
produce the string cannot check for it.

The traversal rules matter more than the traversal. Allowing many-valued hops
makes 133 of NetBox's 140 models look user-owned through chains like
`datafile__source__jobs__user`; requiring single-valued ones leaves 12, and
those are the ones with a real owner. Most of these tests exist to hold that
line.
"""

from __future__ import annotations

from djaudit.graph.builder import build_model_graph
from djaudit.graph.nodes import ModelGraph
from djaudit.graph.queries import (
    is_user_owned,
    path_to_user,
    reachable_fields,
    relation_path,
)


def project(make_project, models_source: str, **extra: str) -> ModelGraph:
    files = {"shop/__init__.py": "", "shop/models.py": models_source, **extra}
    return build_model_graph(make_project(files))


class TestPathToUser:
    def test_a_direct_foreign_key(self, make_project) -> None:
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
        found = path_to_user(graph, "shop.Order")
        assert found is not None
        assert found.hops == 1
        assert found.lookup == "owner"

    def test_two_hops_compose_into_a_lookup(self, make_project) -> None:
        """The Healthchecks shape: a Check belongs to a user via its project."""
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Project(models.Model):
                owner = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )

            class Check(models.Model):
                project = models.ForeignKey(Project, on_delete=models.CASCADE)
            """,
        )
        found = path_to_user(graph, "shop.Check")
        assert found is not None
        assert found.hops == 2
        assert found.lookup == "project__owner"

    def test_the_user_model_itself_needs_no_hops(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                pass
            """,
            **{
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
            },
        )
        found = path_to_user(graph, "accounts.Person")
        assert found is not None
        assert found.hops == 0
        assert found.lookup == ""

    def test_a_model_with_no_route_has_none(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Region(models.Model):
                name = models.CharField(max_length=50)
            """,
        )
        assert path_to_user(graph, "shop.Region") is None
        assert not is_user_owned(graph, "shop.Region")

    def test_a_swapped_user_model_is_still_found(self, make_project) -> None:
        """The FK names `settings.AUTH_USER_MODEL`, never the class."""
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
                    buyer = models.ForeignKey(
                        settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                    )

                class Line(models.Model):
                    order = models.ForeignKey(Order, on_delete=models.CASCADE)
                """,
            }
        )
        graph = build_model_graph(ctx)
        assert graph.user_model == "accounts.Person"
        found = path_to_user(graph, "shop.Line")
        assert found is not None
        assert found.lookup == "order__buyer"

    def test_the_shortest_route_wins(self, make_project) -> None:
        """Both routes are real; the one a reviewer would name is the short one."""
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Team(models.Model):
                lead = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )

            class Order(models.Model):
                team = models.ForeignKey(Team, on_delete=models.CASCADE)
                buyer = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )
            """,
        )
        found = path_to_user(graph, "shop.Order")
        assert found is not None
        assert found.lookup == "buyer"

    def test_the_hop_limit_is_enforced(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class A(models.Model):
                owner = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )

            class B(models.Model):
                a = models.ForeignKey(A, on_delete=models.CASCADE)

            class C(models.Model):
                b = models.ForeignKey(B, on_delete=models.CASCADE)
            """,
        )
        assert path_to_user(graph, "shop.C", max_hops=3) is not None
        assert path_to_user(graph, "shop.C", max_hops=2) is None
        assert not is_user_owned(graph, "shop.C", max_hops=2)

    def test_a_cycle_does_not_hang(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Node(models.Model):
                parent = models.ForeignKey("self", on_delete=models.CASCADE)
                peer = models.ForeignKey("shop.Other", on_delete=models.CASCADE)

            class Other(models.Model):
                back = models.ForeignKey(Node, on_delete=models.CASCADE)
            """,
        )
        assert path_to_user(graph, "shop.Node") is None

    def test_an_unknown_model_is_none_not_an_error(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                pass
            """,
        )
        assert path_to_user(graph, "shop.Nope") is None
        assert not is_user_owned(graph, "shop.Nope")


class TestOwnershipIsSingleValued:
    """A row reached through a set does not have *an* owner."""

    def test_a_many_to_many_is_not_ownership(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Order(models.Model):
                watchers = models.ManyToManyField(settings.AUTH_USER_MODEL)
            """,
        )
        assert path_to_user(graph, "shop.Order") is None

    def test_but_it_can_be_asked_for(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Order(models.Model):
                watchers = models.ManyToManyField(settings.AUTH_USER_MODEL)
            """,
        )
        found = path_to_user(graph, "shop.Order", allow_multi=True)
        assert found is not None
        assert found.is_multi
        assert found.lookup == "watchers"

    def test_a_generic_relation_is_not_a_forward_hop(self, make_project) -> None:
        """It is the reverse of a GenericForeignKey. NetBox declares 397."""
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.contrib.contenttypes.fields import GenericRelation
            from django.db import models

            class Job(models.Model):
                user = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )

            class Source(models.Model):
                jobs = GenericRelation(Job)
            """,
        )
        assert path_to_user(graph, "shop.Source") is None
        assert path_to_user(graph, "shop.Job") is not None

    def test_a_single_valued_hop_survives_the_filter(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Profile(models.Model):
                user = models.OneToOneField(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )

            class Setting(models.Model):
                profile = models.ForeignKey(Profile, on_delete=models.CASCADE)
            """,
        )
        found = path_to_user(graph, "shop.Setting")
        assert found is not None
        assert found.lookup == "profile__user"
        assert not found.is_multi


class TestPathFlags:
    def test_a_nullable_hop_marks_the_path_optional(self, make_project) -> None:
        """Healthchecks' Notification.owner is null=True; filtering drops rows."""
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Check(models.Model):
                owner = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )

            class Notification(models.Model):
                owner = models.ForeignKey(Check, on_delete=models.CASCADE, null=True)
            """,
        )
        found = path_to_user(graph, "shop.Notification")
        assert found is not None
        assert found.is_optional

    def test_a_required_chain_is_not_optional(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Check(models.Model):
                owner = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )

            class Ping(models.Model):
                owner = models.ForeignKey(Check, on_delete=models.CASCADE)
            """,
        )
        found = path_to_user(graph, "shop.Ping")
        assert found is not None
        assert not found.is_optional
        assert found.target is not None


class TestRelationPath:
    def test_between_two_ordinary_models(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Region(models.Model):
                pass

            class Site(models.Model):
                region = models.ForeignKey(Region, on_delete=models.CASCADE)

            class Rack(models.Model):
                site = models.ForeignKey(Site, on_delete=models.CASCADE)
            """,
        )
        found = relation_path(graph, "shop.Rack", "shop.Region")
        assert found is not None
        assert found.lookup == "site__region"
        assert found.target == "shop.Region"

    def test_direction_matters(self, make_project) -> None:
        """Rack points at Site. Site does not point back, so there is no path."""
        graph = project(
            make_project,
            """
            from django.db import models

            class Site(models.Model):
                pass

            class Rack(models.Model):
                site = models.ForeignKey(Site, on_delete=models.CASCADE)
            """,
        )
        assert relation_path(graph, "shop.Rack", "shop.Site") is not None
        assert relation_path(graph, "shop.Site", "shop.Rack") is None

    def test_a_model_reaches_itself_in_zero_hops(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Site(models.Model):
                pass
            """,
        )
        found = relation_path(graph, "shop.Site", "shop.Site")
        assert found is not None
        assert found.hops == 0

    def test_an_unknown_source_is_none(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Site(models.Model):
                pass
            """,
        )
        assert relation_path(graph, "shop.Nope", "shop.Site") is None


class TestReachableFields:
    def test_local_fields_are_depth_zero(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                total = models.IntegerField()
                note = models.CharField(max_length=10)
            """,
        )
        found = reachable_fields(graph, "shop.Order")
        assert "total" in found
        assert "note" in found

    def test_related_fields_are_prefixed(self, make_project) -> None:
        """What `depth = 1` on a ModelSerializer actually exposes."""
        graph = project(
            make_project,
            """
            from django.db import models

            class Profile(models.Model):
                is_staff = models.BooleanField(default=False)

            class Order(models.Model):
                profile = models.ForeignKey(Profile, on_delete=models.CASCADE)
            """,
        )
        found = reachable_fields(graph, "shop.Order")
        assert "profile__is_staff" in found
        assert found["profile__is_staff"].kind == "BooleanField"

    def test_the_depth_cap_is_enforced(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class C(models.Model):
                secret = models.CharField(max_length=10)

            class B(models.Model):
                c = models.ForeignKey(C, on_delete=models.CASCADE)

            class A(models.Model):
                b = models.ForeignKey(B, on_delete=models.CASCADE)
            """,
        )
        assert "b__c__secret" not in reachable_fields(graph, "shop.A", max_depth=1)
        assert "b__c__secret" in reachable_fields(graph, "shop.A", max_depth=2)

    def test_reverse_relations_are_opt_in(self, make_project) -> None:
        """Forward is what a request can set; reverse is only what it can read."""
        graph = project(
            make_project,
            """
            from django.db import models

            class Author(models.Model):
                pass

            class Book(models.Model):
                author = models.ForeignKey(Author, on_delete=models.CASCADE)
                title = models.CharField(max_length=10)
            """,
        )
        assert "book__title" not in reachable_fields(graph, "shop.Author")
        opened = reachable_fields(graph, "shop.Author", include_reverse=True)
        assert "book__title" in opened

    def test_the_reverse_step_is_the_query_name_not_the_accessor(self, make_project) -> None:
        """`author.books` is attribute access; `filter(books__title=...)` is the
        lookup. They diverge by exactly `_set` when no related_name is given,
        and a path built from the accessor would not be a valid query."""
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
                title = models.CharField(max_length=10)
            """,
        )
        opened = reachable_fields(graph, "shop.Author", include_reverse=True)
        assert "books__title" in opened
        assert "books_set__title" not in opened

    def test_a_cycle_does_not_hang(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Node(models.Model):
                name = models.CharField(max_length=10)
                parent = models.ForeignKey("self", on_delete=models.CASCADE)
            """,
        )
        assert "name" in reachable_fields(graph, "shop.Node", max_depth=4)

    def test_an_unknown_model_is_empty(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.db import models

            class Order(models.Model):
                pass
            """,
        )
        assert reachable_fields(graph, "shop.Nope") == {}


class TestGraphMethods:
    """The same queries, reached the way a rule will reach them."""

    def test_the_methods_delegate(self, make_project) -> None:
        graph = project(
            make_project,
            """
            from django.conf import settings
            from django.db import models

            class Project(models.Model):
                owner = models.ForeignKey(
                    settings.AUTH_USER_MODEL, on_delete=models.CASCADE
                )

            class Check(models.Model):
                project = models.ForeignKey(Project, on_delete=models.CASCADE)
            """,
        )
        assert graph.is_user_owned("shop.Check")
        found = graph.path_to_user("shop.Check")
        assert found is not None
        assert found.lookup == "project__owner"
        assert graph.relation_path("shop.Check", "shop.Project") is not None
        assert "project__owner" in graph.reachable_fields("shop.Check")
