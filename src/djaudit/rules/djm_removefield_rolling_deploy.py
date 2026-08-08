"""`DJM-004` -- a `RemoveField` that breaks the release still running beside it.

A rolling deploy runs two releases at once, on purpose: instances of the old
one keep serving while instances of the new one start. Migrations run once, at
the front of that window, so for its duration the previous release's code is
talking to the new release's schema.

Dropping a column in that window is worse than it first looks, and the reason
is in Django's query compiler rather than in the migration.
`SQLCompiler.get_default_columns` builds its select list by iterating
`opts.concrete_fields` and naming every one of them, so the SQL a plain
`Order.objects.get(pk=1)` emits lists every column on the table. Delete one and
the old release does not merely lose that value -- **every query it makes
against that model fails** with `column does not exist`, including queries that
never mentioned the dropped field. One removed column takes the whole model out
for the length of the deploy.

Django's own answer is to split the two halves across two releases, which is
what `SeparateDatabaseAndState` exists to express:

1. **This release** ships `state_operations=[RemoveField(...)]`. The model stops
   declaring the field, so the new code stops selecting it, and the column is
   still there for the old code that does.
2. **A later release** ships the matching `database_operations` half and drops
   the column, by which time nothing selects it.

That shape is why this rule cannot be a match on the operation name. The
replay flattens `SeparateDatabaseAndState` down to its database half (its state
half is a no-op against the database, and applying both would double every
rename the wrapper exists to express), so step 2 above arrives here as a bare
`RemoveField`, byte-identical to the reckless single-step version. Reporting on
the name alone would flag the careful pattern and the careless one alike, and
would tell anyone who had followed this rule's own remediation that they had
made things worse. `Applied.via_separate` is what keeps the two apart.

Two further cases are deliberately quiet:

* **Many-to-many fields** are not concrete columns -- they live in
  `opts.many_to_many` and never appear in that select list -- so removing one
  drops a join table without breaking ordinary queries. Only code traversing
  the relation fails. Still a defect, and reported as one, but at a lower
  severity and with a message that says what actually breaks. Six of the 185
  `RemoveField`s across the three corpora are this case.
* **A field removed from a model the same migration deletes** is reported by
  neither this rule nor any other. `makemigrations` emits `RemoveField` ahead of
  `DeleteModel` to break foreign-key cycles, so a single deletion can produce a
  burst of findings that all describe one decision and none of which name it.
  Six of the 185 are this case, and it is a documented gap rather than a claim
  that dropping a table mid-deploy is safe.
"""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.migrations.nodes import MigrationNode, Operation
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import TABLE_VALUED_FIELDS, MigrationRule


def _deletes_model(migration: MigrationNode, model_name: str | None) -> bool:
    """Whether this migration also drops the table the field belongs to.

    Inner operations are searched as well as top-level ones, because a
    `DeleteModel` wrapped in `SeparateDatabaseAndState` still drops the table.
    """
    operations: list[Operation] = list(migration.operations)
    operations += [inner for op in migration.operations for inner in op.inner]
    return any(op.name == "DeleteModel" and op.model_name == model_name for op in operations)


@register
class RemoveFieldBreaksRollingDeploy(MigrationRule):
    """`RemoveField` dropping a column the previous release still selects."""

    meta = RuleMeta(
        id="DJM-004",
        title="RemoveField drops a column the running release still selects",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        rationale=(
            "During a rolling deploy the previous release is still serving "
            "when the migration runs. Django's compiler names every concrete "
            "column in its `SELECT`, so dropping one does not just break "
            "queries that read it -- every query the old release makes "
            "against that model fails until the last old instance is gone."
        ),
        remediation=(
            "Split the change across two releases with "
            "`SeparateDatabaseAndState`. Ship "
            "`state_operations=[RemoveField(...)]` first, which stops the ORM "
            "selecting the column while leaving it in place for the release "
            "still running. Drop the column in a later deploy with the "
            "matching `database_operations` half, once nothing selects it."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/migration-operations/"
            "#separatedatabaseandstate",
            "https://docs.djangoproject.com/en/stable/topics/migrations/",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported. The live tier "
            "reads `django_migrations` and does not have to guess.",
            "Assumes the project deploys by rolling release. A project that "
            "takes downtime for migrations, or runs a single instance, has no "
            "window in which two releases overlap and nothing here applies.",
            "A field removed from a model the same migration deletes is not "
            "reported, because the finding worth making there is about the "
            "table rather than the column, and no rule makes it yet.",
            "When the replay cannot say what kind of field is being removed, "
            "it is treated as an ordinary column, which is the more damaging "
            "and by far the more common case.",
        ),
    )

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        op = applied.operation
        if op.name != "RemoveField":
            return
        # The database half of a deliberate two-step. The state half shipped in
        # an earlier release, so nothing running still selects this column.
        if applied.via_separate:
            return
        # A model created by a migration in this same deploy is a model no
        # released code has ever queried.
        if not self.populated(applied):
            return
        if _deletes_model(applied.migration, op.model_name):
            return

        migration = applied.migration
        existing = applied.existing_field
        joins = existing is not None and existing.kind in TABLE_VALUED_FIELDS
        target = f"{op.model_name}.{op.field_name}"

        if joins:
            message = (
                f"`{migration.app}.{migration.name}` removes `{target}`, dropping "
                f"its join table. Code in the release still running during the "
                f"deploy fails as soon as it traverses the relation."
            )
        else:
            message = (
                f"`{migration.app}.{migration.name}` removes `{target}`. Django "
                f"names every concrete column in its `SELECT`, so every query "
                f"the release still running makes against `{op.model_name}` "
                f"fails for the length of the deploy, not only those reading "
                f"`{op.field_name}`."
            )

        yield self.finding(
            severity=Severity.MEDIUM if joins else Severity.HIGH,
            location=self.locate(ctx, migration, op),
            message=message,
            evidence=(
                self.excerpt(ctx, migration, op),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"removed field kind={existing.kind if existing else 'unknown'}; "
                        f"not wrapped in SeparateDatabaseAndState"
                    ),
                    source=f"{ctx.rel(migration.path)}:{op.lineno}",
                ),
            ),
        )
