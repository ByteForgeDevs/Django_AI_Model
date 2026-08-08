"""`DJM-005` -- a rename that breaks the release still running beside it.

A rename is `DJM-004`'s problem without `DJM-004`'s escape. Dropping a column
breaks the previous release because Django's compiler names every concrete
column in its `SELECT`; renaming one breaks it for exactly the same reason, and
renaming the *model* breaks it harder still, because then it is the table in the
`FROM` clause that has gone. For the length of a rolling deploy the old release
is querying names that no longer exist.

What makes a rename worse than a removal is that there is no two-release version
of it. `RemoveField` can be split: stop selecting the column in one release,
drop it in the next. A rename has no such split, because the old name and the
new one are the same column and it can only have one name at a time. Whichever
release is not the one that renamed it is wrong.

So the fix is not to rename the column at all. `db_column` pins where a field's
data lives independently of what the field is called, which turns a rename into
a Python-side change that emits no DDL whatsoever. That is not an inference:
`RenameField.database_forwards` calls `schema_editor.alter_field`, and
`_alter_field` guards its rename statement with `if old_field.column !=
new_field.column`, so pinning both sides to one column emits nothing at all.
`RenameModel` has the same escape in `db_table`, written as `AlterModelTable`.

Both orders of that pattern are accepted, because both occur. Django's
autodetector runs `generate_renamed_fields()` before `generate_altered_fields()`,
so a generated migration pins the column *after* the rename; pretix's
`0254_alter_logentry_organizer_link_and_more` pins it before. Reading only the
state the replay carries into the operation would have caught pretix's shape and
missed the generated one, which is the common one.

Three cases are deliberately quiet:

* **A rename inside `SeparateDatabaseAndState`**, where somebody has already
  separated what the database does from what the ORM believes. Unlike
  `DJM-004`'s equivalent guard this one has corpus evidence: NetBox's
  `tenancy.0020_remove_contactgroupmembership` renames a table and a column
  inside the wrapper to convert an explicit through-model into an implicit M2M.
  That is expert hand-written work, and exactly what a name match would flag.
* **Many-to-many fields**, which own no column of their own. Renaming one
  renames the through table -- `_alter_many_to_many` calls `alter_db_table` when
  the through table's name changes -- so ordinary queries survive and only
  traversal breaks. Still a defect, reported a rank lower and with a message
  that says what actually fails.
* **A model created by a migration in the same deploy**, which no released code
  has ever queried under either name.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

from djaudit.astutils import literal
from djaudit.context import ProjectContext
from djaudit.graph.nodes import FieldNode
from djaudit.migrations.nodes import MigrationNode
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import TABLE_VALUED_FIELDS, MigrationRule


class _Moves(NamedTuple):
    """What a rename moves, and the words for it."""

    joins: bool
    """Whether what moves is a join table rather than a column."""

    message: str
    detail: str


def _column(name: str, declared: FieldNode | None) -> str | None:
    """Where a field's data sits, or `None` when its declaration cannot be read.

    `db_column` wins where it is present and readable. Otherwise Django names
    the column after the attribute, which for a relation is `attname` and so
    carries the `_id` suffix. A `db_column` that is there but is not a literal
    is the one case with no answer, and it is reported as none rather than
    guessed past.
    """
    if declared is None:
        return name
    pinned = declared.kwargs.get("db_column")
    if pinned is None:
        return f"{name}_id" if declared.is_relation else name
    value = literal(pinned)
    return value if isinstance(value, str) else None


def _repinned(migration: MigrationNode, model: str | None, names: set[str]) -> FieldNode | None:
    """A field this same migration redeclares with an `AlterField`.

    Either name is accepted, because the pin may be written against the field as
    it was or as it will be depending on whether the migration was generated or
    reordered by hand, and both occur in the corpora.
    """
    for op in migration.operations:
        if (
            op.name == "AlterField"
            and op.model_name == model
            and op.field_name in names
            and op.field is not None
        ):
            return op.field
    return None


def _table_pinned_to(migration: MigrationNode, names: set[str]) -> str | None:
    """The table an `AlterModelTable` in this same migration pins the model to.

    Either name is accepted for the same reason `_repinned` accepts either: the
    autodetector runs `generate_altered_db_table()` after
    `generate_renamed_models()`, so a generated migration names the model as it
    will be, while one written by hand may name it as it was.
    """
    for op in migration.operations:
        if op.name == "AlterModelTable" and op.model_name in names:
            value = literal(op.argument("table"))
            if isinstance(value, str):
                return value
    return None


@register
class RenameBreaksRollingDeploy(MigrationRule):
    """`RenameField` or `RenameModel` moving a name the running release uses."""

    meta = RuleMeta(
        id="DJM-005",
        title="Rename moves a name the running release still queries",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        rationale=(
            "During a rolling deploy the previous release is still serving "
            "when the migration runs, and it queries the old name. A rename "
            "cannot be split across two releases the way a removal can, "
            "because a column has only one name at a time, so every query the "
            "old release makes against the model fails until the last old "
            "instance is gone."
        ),
        remediation=(
            "Do not move the column. Keep the field where it is with "
            "`db_column='<old name>'`, or the model with `db_table`, so the "
            "rename becomes a Python-side change that emits no DDL and the "
            "release still running keeps finding its data. Drop the pin later, "
            "in its own migration, once no old release is left."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/fields/#db-column",
            "https://docs.djangoproject.com/en/stable/ref/migration-operations/#renamefield",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported. The live tier "
            "reads `django_migrations` and does not have to guess.",
            "Assumes the project deploys by rolling release. A project that "
            "takes downtime for migrations, or runs a single instance, has no "
            "window in which two releases overlap and nothing here applies.",
            "A model whose table was already fixed by a `db_table` in its own "
            "`Meta` is compared against the name Django would otherwise have "
            "given it, because the replay does not track model options. Such a "
            "model can be reported when its table does not in fact move.",
            "A `db_column` or `db_table` whose value is not a literal string "
            "cannot be compared, so the rename is left unreported rather than "
            "reported on a guess.",
        ),
    )

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        op = applied.operation
        # `RenameIndex` carries `old_name` and `new_name` too, and renaming an
        # index is invisible to a running release, so the name check has to be
        # positive rather than a test for having two names.
        if op.name not in {"RenameField", "RenameModel"}:
            return
        # Somebody has already separated what the database does from what the
        # ORM believes, which is the whole of the careful version of this.
        if applied.via_separate:
            return
        # A model created by a migration in this same deploy is one no released
        # code has ever queried, under either name.
        if not self.populated(applied):
            return
        old, new = op.old_name, op.new_name
        if not old or not new:
            return

        if op.name == "RenameModel":
            moves = self._model_moves(applied, old, new)
        else:
            moves = self._column_moves(applied, old, new)
        if moves is None:
            return

        yield self.finding(
            severity=Severity.MEDIUM if moves.joins else Severity.HIGH,
            location=self.locate(ctx, applied.migration, op),
            message=moves.message,
            evidence=(
                self.excerpt(ctx, applied.migration, op),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=moves.detail,
                    source=f"{ctx.rel(applied.migration.path)}:{op.lineno}",
                ),
            ),
        )

    def _column_moves(self, applied: Applied, old: str, new: str) -> _Moves | None:
        """Whether this `RenameField` moves the column, and what to say if it does."""
        op = applied.operation
        migration = applied.migration
        existing = applied.existing_field

        before = _column(old, existing)
        after = _column(new, _repinned(migration, op.model_name, {old, new}) or existing)
        if before is None or after is None or before == after:
            return None

        target = f"{op.model_name}.{old}"
        if existing is not None and existing.kind in TABLE_VALUED_FIELDS:
            return _Moves(
                joins=True,
                message=(
                    f"`{migration.app}.{migration.name}` renames `{target}` to "
                    f"`{new}`, renaming its join table with it. Code in the "
                    f"release still running during the deploy fails as soon as "
                    f"it traverses the relation."
                ),
                detail=f"join table follows the field name; {old} -> {new}",
            )
        return _Moves(
            joins=False,
            message=(
                f"`{migration.app}.{migration.name}` renames `{target}` to "
                f"`{new}`, moving column `{before}` to `{after}`. Django names "
                f"every concrete column in its `SELECT`, so every query the "
                f"release still running makes against `{op.model_name}` fails "
                f"for the length of the deploy."
            ),
            detail=f"column {before} -> {after}; no db_column holds it in place",
        )

    def _model_moves(self, applied: Applied, old: str, new: str) -> _Moves | None:
        """Whether this `RenameModel` moves the table, and what to say if it does."""
        migration = applied.migration
        before = f"{migration.app}_{old.lower()}"
        after = _table_pinned_to(migration, {old.lower(), new.lower()})
        if after is None:
            after = f"{migration.app}_{new.lower()}"
        if before == after:
            return None

        return _Moves(
            joins=False,
            message=(
                f"`{migration.app}.{migration.name}` renames model `{old}` to "
                f"`{new}`, moving table `{before}` to `{after}`. Every query "
                f"the release still running makes against it names the old "
                f"table, and fails for the length of the deploy."
            ),
            detail=f"table {before} -> {after}; no db_table holds it in place",
        )
