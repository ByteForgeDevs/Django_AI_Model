"""The migration dependency graph.

The corpus says the graph agrees with Django on all three projects: 29 apps,
one leaf each, no conflict, and a plan that never places a migration before one
it depends on. What the corpus cannot say is *why* each of those holds, and two
of them held only after a defect the corpus found. These pin the reasons.
"""

from __future__ import annotations

import textwrap

import pytest

from djaudit.migrations.graph import Conflict, build_migration_graph


def migration(deps: str = "[]", *, extra: str = "", operations: str = "[]") -> str:
    return textwrap.dedent(
        f"""
        from django.db import migrations, models


        class Migration(migrations.Migration):
            dependencies = {deps}
            {extra}
            operations = {operations}
        """
    ).lstrip()


@pytest.fixture
def graph_of(make_project):
    """Build a migration graph from ``{{app: {{name: source}}}}``."""

    def build(apps: dict[str, dict[str, str]]):
        files: dict[str, str] = {}
        for app, migrations_ in apps.items():
            files[f"{app}/__init__.py"] = ""
            files[f"{app}/migrations/__init__.py"] = ""
            for name, source in migrations_.items():
                files[f"{app}/migrations/{name}.py"] = source
        return build_migration_graph(make_project(files))

    return build


class TestWiring:
    def test_a_dependency_becomes_an_edge_from_the_dependency(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0002_total": migration("[('shop', '0001_initial')]"),
                }
            }
        )
        assert graph.edges[("shop", "0001_initial")] == {("shop", "0002_total")}
        assert graph.dependencies_of(("shop", "0002_total")) == [("shop", "0001_initial")]

    def test_a_dependency_on_another_app_is_wired_too(self, graph_of):
        graph = graph_of(
            {
                "shop": {"0001_initial": migration()},
                "billing": {"0001_initial": migration("[('shop', '0001_initial')]")},
            }
        )
        assert graph.edges[("shop", "0001_initial")] == {("billing", "0001_initial")}

    def test_a_dependency_on_an_app_outside_the_project_is_recorded_not_dropped(self, graph_of):
        # ``contenttypes.0002`` lives in site-packages and is a real dependency.
        # "depends on something outside" and "depends on something missing" are
        # different bugs, and collapsing them loses the difference.
        graph = graph_of(
            {"shop": {"0001_initial": migration("[('contenttypes', '0002_remove_x')]")}}
        )
        assert ("contenttypes", "0002_remove_x") in graph.external
        assert graph.dependencies_of(("shop", "0001_initial")) == []

    def test_an_unfollowable_dependency_is_recorded_against_its_migration(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(
                        "[migrations.swappable_dependency(settings.AUTH_USER_MODEL)]",
                    )
                }
            }
        )
        assert graph.unresolved[("shop", "0001_initial")][0].swappable
        assert graph.external == set()

    def test_apps_are_listed_once_and_sorted(self, graph_of):
        graph = graph_of(
            {
                "shop": {"0001_initial": migration(), "0002_a": migration()},
                "billing": {"0001_initial": migration()},
            }
        )
        assert graph.apps == ("billing", "shop")


class TestLeavesAndConflicts:
    def test_the_tip_of_a_chain_is_the_only_leaf(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0002_total": migration("[('shop', '0001_initial')]"),
                    "0003_ref": migration("[('shop', '0002_total')]"),
                }
            }
        )
        assert [node.name for node in graph.leaves("shop")] == ["0003_ref"]
        assert graph.conflicts() == []

    def test_two_migrations_off_the_same_parent_are_a_conflict(self, graph_of):
        # Django's "Conflicting migrations detected; multiple leaf nodes".
        # Two branches each added a migration and neither rebased.
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0002_a": migration("[('shop', '0001_initial')]"),
                    "0002_b": migration("[('shop', '0001_initial')]"),
                }
            }
        )
        assert graph.conflicts() == [Conflict(app="shop", leaves=("0002_a", "0002_b"))]

    def test_a_dependent_in_another_app_does_not_make_a_leaf(self, graph_of):
        # Cross-app edges must not count, or an app whose tip something else
        # depends on would report no leaf and the conflict would be invisible.
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0002_a": migration("[('shop', '0001_initial')]"),
                    "0002_b": migration("[('shop', '0001_initial')]"),
                },
                "billing": {"0001_initial": migration("[('shop', '0002_a')]")},
            }
        )
        assert [node.name for node in graph.leaves("shop")] == ["0002_a", "0002_b"]

    def test_a_conflict_renders_as_the_app_and_its_leaves(self, graph_of):
        conflict = Conflict(app="shop", leaves=("0002_a", "0002_b"))
        assert str(conflict) == "shop: 0002_a, 0002_b"


class TestSquashes:
    def test_a_dependency_on_a_replaced_migration_points_at_the_squash(self, graph_of):
        # Without this the squash has nothing depending on it and reads as a
        # second leaf. It made pretix report nine leaves in one app and a
        # conflict Django does not have.
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0002_a": migration("[('shop', '0001_initial')]"),
                    "0001_squashed_0002": migration(
                        "[]", extra="replaces = [('shop', '0001_initial'), ('shop', '0002_a')]"
                    ),
                    "0003_b": migration("[('shop', '0002_a')]"),
                }
            }
        )
        assert graph.dependencies_of(("shop", "0003_b")) == [("shop", "0001_squashed_0002")]
        assert [node.name for node in graph.leaves("shop")] == ["0003_b"]

    def test_replaced_migrations_still_on_disk_are_superseded(self, graph_of):
        # pretix keeps all 157 of them; NetBox deletes all 476. Both are real.
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0001_squashed_0002": migration(
                        "[]", extra="replaces = [('shop', '0001_initial')]"
                    ),
                }
            }
        )
        assert graph.superseded == frozenset({("shop", "0001_initial")})

    def test_a_squash_over_deleted_migrations_supersedes_nothing(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0001_squashed_0002": migration(
                        "[]", extra="replaces = [('shop', '0001_initial')]"
                    )
                }
            }
        )
        assert graph.replaced == {("shop", "0001_initial"): ("shop", "0001_squashed_0002")}
        assert graph.superseded == frozenset()

    def test_a_superseded_migration_is_not_planned_twice(self, graph_of):
        # Planning both the squash and what it replaces would re-run every
        # operation in them -- for pretix, 157 migrations' worth of columns.
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0002_a": migration("[('shop', '0001_initial')]"),
                    "0001_squashed_0002": migration(
                        "[]", extra="replaces = [('shop', '0001_initial'), ('shop', '0002_a')]"
                    ),
                }
            }
        )
        assert [node.name for node in graph.plan()] == ["0001_squashed_0002"]

    def test_a_superseded_dependent_cannot_make_a_squash_a_non_leaf(self, graph_of):
        # pretix's ``sendmail.0012`` depends on the squash that replaces it.
        # Counting that dependent made the squash look required, and the app
        # reported *no* leaf at all -- which a DAG with no cycle cannot have.
        graph = graph_of(
            {
                "shop": {
                    "0011_a": migration(),
                    "0011_squashed_0012": migration(
                        "[]", extra="replaces = [('shop', '0011_a'), ('shop', '0012_b')]"
                    ),
                    "0012_b": migration("[('shop', '0011_squashed_0012')]"),
                }
            }
        )
        assert [node.name for node in graph.leaves("shop")] == ["0011_squashed_0012"]

    def test_a_chain_of_squashes_resolves_to_the_outermost(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0001_squashed_0002": migration(
                        "[]", extra="replaces = [('shop', '0001_initial')]"
                    ),
                    "0001_squashed_0004": migration(
                        "[]", extra="replaces = [('shop', '0001_squashed_0002')]"
                    ),
                }
            }
        )
        assert graph.canonical(("shop", "0001_initial")) == ("shop", "0001_squashed_0004")

    def test_a_replaces_loop_terminates_rather_than_hanging(self, graph_of):
        # ``replaces`` is written by hand often enough that a loop is a thing a
        # project can have, and a graph build that never returns is worse than
        # any finding it could have produced.
        graph = graph_of(
            {
                "shop": {
                    "0001_a": migration("[]", extra="replaces = [('shop', '0002_b')]"),
                    "0002_b": migration("[]", extra="replaces = [('shop', '0001_a')]"),
                }
            }
        )
        assert graph.canonical(("shop", "0001_a")) in graph.nodes


class TestPlan:
    def test_every_migration_comes_after_what_it_depends_on(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0002_total": migration("[('shop', '0001_initial')]"),
                },
                "billing": {
                    "0001_initial": migration("[('shop', '0002_total')]"),
                },
            }
        )
        order = [node.key for node in graph.plan()]
        for key in order:
            for dep in graph.dependencies_of(key):
                assert order.index(dep) < order.index(key)

    def test_ties_are_broken_by_name_so_two_runs_agree(self, graph_of):
        # The plan feeds state replay. A replay that reorders itself between
        # runs would move findings for no reason at all.
        apps = {
            "shop": {"0001_initial": migration(), "0002_a": migration()},
            "billing": {"0001_initial": migration()},
        }
        first = [node.key for node in graph_of(apps).plan()]
        assert first == [
            ("billing", "0001_initial"),
            ("shop", "0001_initial"),
            ("shop", "0002_a"),
        ]

    def test_a_cycle_is_reported_rather_than_raised(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0001_a": migration("[('shop', '0002_b')]"),
                    "0002_b": migration("[('shop', '0001_a')]"),
                }
            }
        )
        assert graph.cycle_keys() == [("shop", "0001_a"), ("shop", "0002_b")]

    def test_a_cycles_migrations_are_still_planned(self, graph_of):
        # A rule reading replayed state should see a project missing an
        # ordering, not a project missing tables.
        graph = graph_of(
            {
                "shop": {
                    "0001_a": migration("[('shop', '0002_b')]"),
                    "0002_b": migration("[('shop', '0001_a')]"),
                    "0003_c": migration(),
                }
            }
        )
        assert {node.name for node in graph.plan()} == {"0001_a", "0002_b", "0003_c"}

    def test_a_healthy_graph_has_no_cycles(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0001_initial": migration(),
                    "0002_total": migration("[('shop', '0001_initial')]"),
                }
            }
        )
        assert graph.cycle_keys() == []

    def test_a_self_dependency_does_not_stall_the_plan(self, graph_of):
        graph = graph_of({"shop": {"0001_a": migration("[('shop', '0001_a')]")}})
        assert [node.name for node in graph.plan()] == ["0001_a"]


class TestOfApp:
    def test_migrations_are_listed_in_name_order(self, graph_of):
        graph = graph_of(
            {
                "shop": {
                    "0002_b": migration(),
                    "0001_a": migration(),
                },
                "billing": {"0001_x": migration()},
            }
        )
        assert [node.name for node in graph.of_app("shop")] == ["0001_a", "0002_b"]

    def test_an_app_with_no_migrations_lists_nothing(self, graph_of):
        graph = graph_of({"shop": {"0001_a": migration()}})
        assert graph.of_app("billing") == []
