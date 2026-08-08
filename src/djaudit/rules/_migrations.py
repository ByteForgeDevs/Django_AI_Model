"""Shared machinery for the `DJM` family, and the scope question it turns on.

Every rule in this family reports on something a migration does *at the moment
it runs*: a lock it takes, a table it rewrites, a statement that aborts. That
makes the family unlike every other one in djaudit, because the thing it judges
is an event rather than a state, and almost every event it can see has already
happened.

The consequence is sharper than "old findings are noise". A migration sitting in
a shipped project's history **demonstrably ran** -- the deploy that applied it
succeeded, or the project would not be in this shape. So a static tier that
reported an abort or a long lock across all of history would not merely be
reporting things nobody can act on; on that half of the corpus it would be
*provably wrong*. Measured over three real projects, reporting family-wide would
have produced roughly sixteen hundred findings, against two hundred and forty
five for the entire rest of the tool, and the true-positive rate among them
would have been zero by construction.

So the family reports only where the finding is still live:

* **Live tier (authoritative).** `django_migrations` names exactly which
  migrations have not been applied. No heuristic, no noise.
* **Static tier (this module).** The leaves of each app's migration graph --
  the tip of the history, which is where a migration being written now lands.

The static scope is a heuristic and is declared as one in every rule's
`limitations`. It over-reports when a leaf shipped long ago, and under-reports
when a change adds two migrations to one app and only the second is a leaf.
That is a deliberate trade: both errors are bounded and explainable, whereas
reporting all of history is unboundedly wrong in a way no baseline can fix.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.migrations.nodes import MigrationNode, Operation
from djaudit.migrations.state import Applied
from djaudit.models import Evidence, EvidenceKind, Finding, Location
from djaudit.registry import Rule

EMPTY_STRING_FIELDS = frozenset(
    {
        "CharField",
        "TextField",
        "SlugField",
        "EmailField",
        "URLField",
        "FilePathField",
        "FileField",
        "ImageField",
        "CommaSeparatedIntegerField",
        "GenericIPAddressField",
    }
)
"""Fields whose `empty_strings_allowed` is true, so Django fills them itself.

`Field.get_default()` returns `""` rather than `None` for these when no default
is declared, so adding one to a populated table supplies a value for every
existing row and cannot fail on a NOT NULL constraint. Reporting them would be
the family's single largest source of false positives: they are 103 of NetBox's
199 non-nullable `AddField`s with no declared default.
"""

TABLE_VALUED_FIELDS = frozenset({"ManyToManyField", "TaggableManager"})
"""Fields that create their own table instead of a column on this one.

`null` is meaningless for them -- there is no column to constrain -- so every
column-level judgement in this family has to exclude them. `TaggableManager` is
`django-taggit`'s, and is here because NetBox uses 42 of them; the set is a
denylist of what we have measured rather than a claim to be exhaustive.
"""


class MigrationRule(Rule):
    """A rule about one operation in a migration that has not yet run.

    Handles the three things every rule in the family would otherwise re-derive:
    which migrations are in scope (see the module docstring -- this is the
    family's central decision, and it must not be made per rule), whether the
    table an operation touches already holds rows, and how to point at an
    operation inside a file the engine never parsed into a normal module tree.
    """

    scope: frozenset[tuple[str, str]] = frozenset()
    """The migrations this run is reporting on, resolved once in :meth:`check`."""

    def in_scope(self, ctx: ProjectContext) -> frozenset[tuple[str, str]]:
        """Keys of the migrations whose findings are still actionable.

        Static answer: the leaf of each app. Overridden by the live tier, which
        can read `django_migrations` and does not have to guess.
        """
        graph = ctx.migration_graph
        apps = {node.app for node in graph.plan()}
        return frozenset(leaf.key for app in apps for leaf in graph.leaves(app))

    def populated(self, applied: Applied) -> bool:
        """Whether the table this operation touches already holds rows.

        Three of this family's rules are only defects against existing data --
        a column that cannot be filled, a rewrite that takes a lock for the
        length of the table, a constraint validated against every row -- so
        each needs the same answer and must not guess at it separately.

        A table created by a migration that is itself still pending is empty
        when the operation reaches it, because both run in the same deploy.
        This is what makes squashed initial migrations quiet: NetBox's
        `circuits.0002_squashed_0029` adds 36 non-nullable foreign keys to
        tables `circuits.0001_squashed` creates in the same run, and every one
        of them would otherwise be reported as a migration that aborts. It also
        subsumes the case of a column added by the very migration that creates
        the table, which is in scope by construction whenever it is inspected.

        A table whose creating migration we never saw is assumed to hold rows.
        It belongs to an app that was installed before this change, which is
        the dangerous case rather than the safe one.
        """
        creator = applied.created_by
        if creator is None:
            return True
        return creator.key not in self.scope

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        self.scope = self.in_scope(ctx)
        for applied in ctx.migration_history:
            if applied.migration.key in self.scope:
                yield from self.inspect(ctx, applied)

    @abstractmethod
    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        """Judge one operation, with the state it acts against."""

    def locate(self, ctx: ProjectContext, migration: MigrationNode, op: Operation) -> Location:
        """Point at the operation itself, not at the migration file.

        A migration is a list of operations and only one of them is the
        finding, so a location naming just the file would make the reader hunt
        for it -- and would collapse two findings in one file to one
        fingerprint.
        """
        if op.node is not None:
            return ctx.location(migration.path, op.node)
        return Location(
            file=ctx.rel(migration.path),
            line=op.lineno,
            column=1,
            end_line=op.end_lineno,
            snippet=ctx.snippet(migration.path, op.lineno, op.end_lineno),
        )

    def excerpt(self, ctx: ProjectContext, migration: MigrationNode, op: Operation) -> Evidence:
        """The operation as written, quoted back as evidence."""
        return Evidence(
            kind=EvidenceKind.AST,
            content=ctx.snippet(migration.path, op.lineno, op.end_lineno) or f"{op.name}(...)",
            source=f"{ctx.rel(migration.path)}:{op.lineno}",
        )
