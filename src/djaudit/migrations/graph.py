"""The migration dependency graph, and the order Django would apply it in.

Two things make this worth building rather than reading each migration alone.

The first is that almost every interesting question about a migration is a
question about its *neighbours*: whether a `RemoveField` lands before the deploy
that stops referencing the column, whether a backfill shares a transaction with
the schema change that created the column it fills. Neither is visible in one
file.

The second is that a migration only describes a *delta*. `AlterField` says what
the column becomes and never what it was, so deciding whether it changed type at
all means replaying everything before it -- and replaying needs an order.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from djaudit.graph.builder import app_label_of_dir
from djaudit.migrations.nodes import Dependency, MigrationNode
from djaudit.migrations.parse import migration_files, parse_migration

if TYPE_CHECKING:
    from djaudit.context import ProjectContext

Key = tuple[str, str]


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two or more migrations in one app with nothing ordering them.

    Django refuses to migrate in this state -- it is the "Conflicting migrations
    detected; multiple leaf nodes" error -- and it is almost always the result
    of two branches each adding a migration and neither rebasing.
    """

    app: str
    leaves: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.app}: {', '.join(self.leaves)}"


@dataclass(slots=True)
class MigrationGraph:
    """Every migration in the project, and what has to run before what.

    Edges point from a dependency to the migration that declares it, so a
    topological sort yields the order Django would apply them in.
    """

    nodes: dict[Key, MigrationNode] = field(default_factory=dict)
    edges: dict[Key, set[Key]] = field(default_factory=lambda: defaultdict(set))
    """``dependency -> {migrations that require it}``."""

    external: set[Key] = field(default_factory=set)
    """Dependencies on apps that are not in this project.

    ``('contenttypes', '0002_remove_content_type_name')`` is a real dependency
    and its file lives in site-packages. Recording it rather than dropping it
    keeps the distinction between "depends on something outside" and "depends on
    something missing", which are different bugs.
    """

    unresolved: dict[Key, tuple[Dependency, ...]] = field(default_factory=dict)
    """Dependencies that named nothing we could follow, by the migration."""

    replaced: dict[Key, Key] = field(default_factory=dict)
    """``replaced migration -> the squash that replaces it``.

    Recorded whether or not the replaced file still exists, because the two
    cases are both real and look nothing alike: NetBox deletes the migrations a
    squash replaces (476 replaced keys, none on disk) and pretix keeps them (157
    replaced keys, all 157 still on disk).
    """

    @property
    def superseded(self) -> frozenset[Key]:
        """Migrations still on disk that a squash has replaced.

        These must not be planned or replayed. Django's loader drops them and
        uses the squash; applying both would run pretix's first 157 migrations
        twice, adding every column in them a second time.
        """
        return frozenset(key for key in self.replaced if key in self.nodes)

    def canonical(self, key: Key) -> Key:
        """The migration that actually runs in place of ``key``.

        Follows a chain, because a squash can itself be squashed. Bounded by a
        seen-set rather than trusted to terminate: `replaces` is written by hand
        often enough that a loop is a thing a project can have.
        """
        seen: set[Key] = set()
        while key in self.replaced and key not in seen:
            seen.add(key)
            key = self.replaced[key]
        return key

    def __post_init__(self) -> None:
        if not isinstance(self.edges, defaultdict):
            self.edges = defaultdict(set, self.edges)

    @property
    def apps(self) -> tuple[str, ...]:
        return tuple(sorted({app for app, _ in self.nodes}))

    def of_app(self, app: str) -> list[MigrationNode]:
        """Every migration in one app, in name order.

        Name order is Django's own convention and matches numeric order for the
        overwhelming majority of files; it is a display order, not the apply
        order. :meth:`plan` gives the apply order.
        """
        return sorted(
            (node for key, node in self.nodes.items() if key[0] == app),
            key=lambda node: node.name,
        )

    def leaves(self, app: str) -> list[MigrationNode]:
        """Migrations in ``app`` that nothing else in ``app`` depends on.

        Restricted to the app deliberately: a migration in another app depending
        on this one does not make it a non-leaf for Django's purposes, and
        counting cross-app edges would hide exactly the conflicts this looks for.

        Superseded migrations are excluded from both sides. Excluding them as
        *candidates* is obvious; excluding them as *dependents* is the half that
        was missing, and pretix has the shape that proves it: ``sendmail.0012``
        depends on the very squash that replaces it, so counting it made the
        squash look required and the app reported no leaf at all. A migration
        Django will never run cannot be the reason another one is not the tip.
        """
        superseded = self.superseded
        required: set[Key] = set()
        for key, dependents in self.edges.items():
            if key[0] != app:
                continue
            if any(dependent[0] == app and dependent not in superseded for dependent in dependents):
                required.add(key)
        return [
            node
            for key, node in sorted(self.nodes.items())
            if key[0] == app and key not in required and key not in superseded
        ]

    def conflicts(self) -> list[Conflict]:
        """Apps with more than one leaf, which Django refuses to migrate."""
        found = []
        for app in self.apps:
            leaves = self.leaves(app)
            if len(leaves) > 1:
                found.append(Conflict(app=app, leaves=tuple(node.name for node in leaves)))
        return found

    def dependencies_of(self, key: Key) -> list[Key]:
        """The in-project migrations ``key`` declares a dependency on.

        Resolved through :meth:`canonical`, so a migration depending on one that
        a squash replaced depends on the squash instead -- which is what Django
        does, and without it the squash has nothing depending on it and reads as
        a second leaf of its own app. That is not a hypothetical: it made pretix
        report nine leaves in ``pretixbase`` and a conflict Django does not have.
        """
        node = self.nodes.get(key)
        if node is None:
            return []
        resolved = []
        for dep in node.dependencies:
            target = self.canonical((dep.app, dep.name))
            if target in self.nodes and target != key:
                resolved.append(target)
        return resolved

    def plan(self) -> list[MigrationNode]:
        """Every migration in an order that respects its dependencies.

        Superseded migrations are left out: Django uses the squash in their
        place, and planning both would replay pretix's first 157 migrations a
        second time, re-adding every column they create.

        Ties are broken by ``(app, name)`` rather than left to dictionary order,
        because the plan feeds state replay and a replay that reorders itself
        between runs would move findings for no reason. Django's own planner
        makes the same choice for the same reason.

        A cycle cannot be applied by Django either. The migrations in it are
        appended in name order rather than dropped: a rule that reads state
        should see a project that is missing an ordering, not a project that is
        missing tables.
        """
        pending = self._pending()
        ordered: list[MigrationNode] = []

        while pending:
            ready = sorted(key for key, deps in pending.items() if not deps)
            if not ready:
                ordered.extend(self.nodes[key] for key in sorted(pending))
                break
            for key in ready:
                ordered.append(self.nodes[key])
                del pending[key]
            self._discharge(pending, set(ready))

        return ordered

    def cycle_keys(self) -> list[Key]:
        """Migrations that no dependency order can satisfy.

        Reported rather than raised: a cycle is a real defect in the project and
        a crash would take the other twenty rules down with it.
        """
        pending = self._pending()
        while True:
            ready = {key for key, deps in pending.items() if not deps}
            if not ready:
                return sorted(pending)
            for key in ready:
                del pending[key]
            self._discharge(pending, ready)

    def _pending(self) -> dict[Key, set[Key]]:
        """Each planned migration's unsatisfied in-project dependencies."""
        planned = set(self.nodes) - self.superseded
        return {
            key: {dep for dep in self.dependencies_of(key) if dep in planned} for key in planned
        }

    @staticmethod
    def _discharge(pending: dict[Key, set[Key]], done: set[Key]) -> None:
        """Drop the migrations just ordered from everything still waiting."""
        for deps in pending.values():
            deps.difference_update(done)


def build_migration_graph(ctx: ProjectContext) -> MigrationGraph:
    """Parse every migration in the project and wire up its dependencies."""
    graph = MigrationGraph()

    for path in migration_files(ctx):
        app = app_label_of_dir(path.parent.parent, ctx)
        node = parse_migration(path, app, ctx)
        if node is not None:
            graph.nodes[node.key] = node

    for key, node in graph.nodes.items():
        for replaced in node.replaces:
            if not replaced.unreadable:
                graph.replaced[(replaced.app, replaced.name)] = key

    # Edges are wired in a second pass because they resolve through
    # ``canonical``, and canonicalising against a half-built ``replaced`` map
    # would leave every migration parsed before its own squash pointing at a
    # node Django is going to drop.
    for key, node in graph.nodes.items():
        unresolved: list[Dependency] = []
        for dep in node.dependencies:
            if dep.unreadable or dep.swappable:
                unresolved.append(dep)
                continue
            target = graph.canonical((dep.app, dep.name))
            if target in graph.nodes:
                if target != key:
                    graph.edges[target].add(key)
            else:
                graph.external.add(target)
        if unresolved:
            graph.unresolved[key] = tuple(unresolved)

    return graph
