"""`DJM-009` -- an `AddConstraint` that checks every existing row under a lock.

Adding a constraint to a populated table is not a metadata edit. Postgres has
to prove the constraint already holds, which means reading every row, and it
does that holding a lock the whole time. As with the rest of this family the
duration is set by the row count, so it is instant in CI and an outage in
production.

**The remediation depends on which constraint it is, and getting that wrong is
worse than saying nothing.** Django's `AddConstraintNotValid` -- the operation
that the obvious advice "add it `NOT VALID`, then validate" is spelled as --
raises `TypeError` on anything that is not a `CheckConstraint`:

    if not isinstance(constraint, CheckConstraint):
        raise TypeError(
            "AddConstraintNotValid.constraint must be a check constraint."
        )

That is not a Django limitation but a Postgres one: `NOT VALID` exists for
`CHECK` and `FOREIGN KEY` constraints and for nothing else. A `UniqueConstraint`
has to build its index before it can be trusted, and there is no deferring
that. So this rule reads the constraint class and says something different for
each, rather than emitting one remediation that is right a third of the time.

The three cases, each traced to the statement Django actually emits:

* **`CheckConstraint`** -- `sql_create_check`, i.e. `ALTER TABLE ... ADD
  CONSTRAINT ... CHECK (...)`. `ACCESS EXCLUSIVE` for a full scan. This is the
  one `AddConstraintNotValid` was written for, and the fix is clean.
* **`UniqueConstraint` with no condition, include, opclasses or expressions** --
  `sql_create_unique`, i.e. `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (...)`.
  Also `ACCESS EXCLUSIVE`, and it builds a unique index underneath.
* **`UniqueConstraint` with any of them** -- `_create_unique_sql` switches to
  `sql_create_unique_index` at `if condition or include or opclasses or
  expressions`, so the statement is a bare `CREATE UNIQUE INDEX`. That takes
  `SHARE`, which permits reads. Ranked a step lower for the same reason
  `DJM-003` is ranked below `DJM-002`: a readable table is not an unavailable
  one.

The concurrent route is worth stating precisely, because the obvious version of
it does not exist. `AddIndexConcurrently` takes an `Index`, and `Index.__init__`
accepts `expressions, fields, name, db_tablespace, opclasses, condition,
include` -- there is **no** `unique` parameter, and no `UniqueIndex` class.
Django therefore cannot build a unique index concurrently, and the honest
remediation is `RunSQL("CREATE UNIQUE INDEX CONCURRENTLY ...")` inside
`SeparateDatabaseAndState` so that migration state still records the
constraint. Recommending `AddIndexConcurrently` here would not run.

Constraints whose class cannot be read are **not** reported. Every branch above
turns on the class name, so an unreadable one would have to fall back to a
generic remediation -- and a plausible-sounding fix that raises `TypeError` on
the reader's first attempt costs more than silence. Declining is a documented
limitation rather than a silent gap.

This overlaps `DJM-008` by design on a migration that does both. They are
different claims about different operations with different fixes: `DJM-008` is
about a *data* pass extending a lock an *earlier* operation took, and its fix is
to reorder or split; this rule is about the constraint's own validation scan,
and its fix is to defer the validation. Reordering, `DJM-008`'s remedy, does
nothing for this one.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import NamedTuple

from djaudit.context import ProjectContext
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import MigrationRule

INDEX_BACKED = ("condition", "include", "opclasses", "expressions")
"""The arguments that switch `_create_unique_sql` to `sql_create_unique_index`.

Taken from the condition in `django/db/backends/base/schema.py`:
`if condition or include or opclasses or expressions:`. Any one of them makes
the emitted statement a `CREATE UNIQUE INDEX` rather than an `ALTER TABLE`,
which changes both the lock and the severity.
"""

NOT_VALID_ROUTE = (
    "Add it unvalidated first and validate it separately: "
    "`django.contrib.postgres.operations.AddConstraintNotValid` emits the same "
    "constraint with `NOT VALID`, which takes the lock only long enough to "
    "record it, and `ValidateConstraint` in a later migration runs the scan "
    "under `SHARE UPDATE EXCLUSIVE`, blocking neither reads nor writes. Rows "
    "written between the two are checked from the moment the constraint "
    "exists, so only the pre-existing rows are ever in question."
)

CONCURRENT_ROUTE = (
    "There is no `NOT VALID` for a unique constraint -- Postgres offers it for "
    "`CHECK` and `FOREIGN KEY` only -- and no Django operation builds a unique "
    "index concurrently, because `AddIndexConcurrently` takes an `Index` and "
    '`Index` has no `unique` argument. The working route is `RunSQL("CREATE '
    'UNIQUE INDEX CONCURRENTLY ...")` in a migration with `atomic = False`, '
    "wrapped in `SeparateDatabaseAndState` so migration state still records "
    "the constraint. It is more code than the constraint is, so on a table "
    "small enough for the scan to be quick this is a reasonable thing to "
    "decline."
)


class Emitted(NamedTuple):
    """What Django will actually run for one constraint, and what it costs.

    A `NamedTuple` rather than a frozen dataclass so that immutability is a
    property of the type instead of two keyword arguments a reader has to
    trust were passed.
    """

    statement: str
    lock: str
    severity: Severity
    remediation: str


CHECK = Emitted(
    statement="ALTER TABLE ... ADD CONSTRAINT ... CHECK (...)",
    lock="ACCESS EXCLUSIVE",
    severity=Severity.HIGH,
    remediation=NOT_VALID_ROUTE,
)
UNIQUE = Emitted(
    statement="ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (...)",
    lock="ACCESS EXCLUSIVE",
    severity=Severity.HIGH,
    remediation=CONCURRENT_ROUTE,
)
UNIQUE_INDEX = Emitted(
    statement="CREATE UNIQUE INDEX ... ON ...",
    lock="SHARE",
    severity=Severity.MEDIUM,
    remediation=CONCURRENT_ROUTE,
)
EXCLUSION = Emitted(
    statement="ALTER TABLE ... ADD CONSTRAINT ... EXCLUDE USING ...",
    lock="ACCESS EXCLUSIVE",
    severity=Severity.HIGH,
    remediation=CONCURRENT_ROUTE,
)


def _class_name(expr: ast.expr | None) -> str | None:
    """The constraint class as written, ignoring however it was imported.

    `models.CheckConstraint(...)`, `CheckConstraint(...)` and
    `django.db.models.CheckConstraint(...)` are the same class and all three
    appear in real migrations, so only the trailing name is read.
    """
    if not isinstance(expr, ast.Call):
        return None
    func = expr.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _emitted(constraint: ast.expr | None) -> Emitted | None:
    """Which statement Django will emit, or `None` when it cannot be told."""
    name = _class_name(constraint)
    if name == "CheckConstraint":
        return CHECK
    if name == "ExclusionConstraint":
        return EXCLUSION
    if name != "UniqueConstraint":
        return None
    assert isinstance(constraint, ast.Call)
    supplied = {kw.arg for kw in constraint.keywords if kw.arg is not None}
    if supplied.intersection(INDEX_BACKED) or constraint.args:
        # Positional arguments to `UniqueConstraint` are `*expressions`.
        return UNIQUE_INDEX
    return UNIQUE


@register
class ValidatedAddConstraint(MigrationRule):
    """`AddConstraint` on a populated table, which scans it under a lock."""

    meta = RuleMeta(
        id="DJM-009",
        title="AddConstraint scans the whole table under a lock to validate it",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        rationale=(
            "Postgres will not take a constraint's word for it. Adding one to "
            "a populated table makes it read every existing row to prove the "
            "constraint already holds, and it holds a lock for the whole scan. "
            "The migration gives no sign of the cost, because the duration "
            "depends on the row count and the table is empty in CI."
        ),
        remediation=(
            "Depends on the constraint: a `CheckConstraint` can be added "
            "`NOT VALID` and validated in a later migration, while a "
            "`UniqueConstraint` has to build its index and needs the "
            "concurrent route instead. Each finding carries the one that "
            "applies to it."
        ),
        references=(
            "https://www.postgresql.org/docs/current/sql-altertable.html",
            "https://docs.djangoproject.com/en/stable/ref/contrib/postgres/operations/",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported. The live tier "
            "reads `django_migrations` and does not have to guess.",
            "Says nothing about how many rows the table holds, which is what "
            "decides whether the scan is instant or a deploy-long outage. The "
            "live tier's `pg_class.reltuples` sizing answers that.",
            "A constraint whose class cannot be read from the migration -- one "
            "built by a helper function, or a project-defined subclass -- is "
            "not reported at all, because every remediation this rule gives "
            "depends on which class it is and the wrong one raises `TypeError`.",
            "Deferred validation, concurrent index builds and the lock modes "
            "named here are all Postgres behaviour. On SQLite or MySQL the "
            "costs and the remedies both differ, and this rule does not detect "
            "which backend the project uses.",
        ),
    )

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        op = applied.operation
        if op.name != "AddConstraint":
            return
        # A constraint on a table this deploy creates is proved against no rows.
        if not self.populated(applied):
            return
        emitted = _emitted(op.argument("constraint"))
        if emitted is None:
            return

        migration = applied.migration
        yield self.finding(
            location=self.locate(ctx, migration, op),
            severity=emitted.severity,
            remediation=emitted.remediation,
            message=(
                f"`{migration.app}.{migration.name}` adds a constraint to "
                f"`{op.model_name}`, which Postgres validates by reading every "
                f"existing row while holding `{emitted.lock}` on the table."
            ),
            evidence=(
                self.excerpt(ctx, migration, op),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=f"Django emits `{emitted.statement}`, which takes `{emitted.lock}`",
                    source=f"{ctx.rel(migration.path)}:{op.lineno}",
                ),
            ),
        )
