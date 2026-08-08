"""`DJM-003` -- an `AddIndex` that locks writes out while the index builds.

Postgres builds an index under `SHARE`, which permits reads and blocks every
write for as long as the build takes. That is a real outage for anything that
writes, and its duration is set by the row count rather than by anything
visible in the migration.

**This is deliberately a rank below `DJM-002`, and the difference is the
point.** `ACCESS EXCLUSIVE` makes a table unavailable; `SHARE` leaves it
readable. A site whose migration is building an index still serves pages and
still fails checkouts. Flattening the two into one severity would tell a reader
the two situations need the same urgency, and they do not.

`CREATE INDEX CONCURRENTLY` takes `SHARE UPDATE EXCLUSIVE` instead, which
blocks neither, at the cost of two table scans and an index that is left
`INVALID` if the build fails. Django exposes it as
`django.contrib.postgres.operations.AddIndexConcurrently`, which sets
`atomic = False` and refuses to run inside a transaction -- so the remedy is
always two edits, the operation *and* the migration's `atomic` flag, and a
remediation naming only the first would not run.

Two operations are deliberately not reported here.

* `AddIndexConcurrently` is the fix, so matching on the class name excludes it
  without a separate guard.
* `AlterIndexTogether` sets the whole `index_together` collection, so whether
  it builds an index, drops one, or does both depends on what the collection
  held before. This rule does not replay that, and reporting it unconditionally
  would flag removals as though they took a build lock. It is a documented gap
  rather than a silent one -- and a narrow one, since `makemigrations` has not
  emitted `index_together` since it was deprecated in Django 4.2.
"""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import MigrationRule


@register
class BlockingAddIndex(MigrationRule):
    """`AddIndex` on a populated table, which blocks writes while it builds."""

    meta = RuleMeta(
        id="DJM-003",
        title="AddIndex blocks writes for the length of the index build",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        rationale=(
            "Postgres builds an index under a `SHARE` lock, which permits "
            "reads and blocks every write until the build finishes. On a "
            "table with millions of rows that is minutes of failed writes "
            "during a deploy, and the migration gives no sign of it: the "
            "duration depends on the row count, so it is instant in CI."
        ),
        remediation=(
            "Use `django.contrib.postgres.operations.AddIndexConcurrently`, "
            "which emits `CREATE INDEX CONCURRENTLY` and blocks neither reads "
            "nor writes. It cannot run inside a transaction, so the migration "
            "also needs `atomic = False`, and it must be the only operation in "
            "that migration for the rest to stay transactional. The build is "
            "slower and leaves an `INVALID` index behind if it fails, which is "
            "dropped and retried rather than repaired."
        ),
        references=(
            "https://www.postgresql.org/docs/current/sql-createindex.html",
            "https://docs.djangoproject.com/en/stable/ref/contrib/postgres/operations/",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported. The live tier "
            "reads `django_migrations` and does not have to guess.",
            "Says nothing about how many rows the table holds, which is what "
            "decides whether the build is instant or a deploy-long write "
            "outage. The live tier's `pg_class.reltuples` sizing answers that.",
            "Concurrent index builds are a Postgres feature. On SQLite or "
            "MySQL the lock differs and the remediation does not apply, and "
            "this rule does not detect which backend the project uses.",
            "Operations that replace the whole `index_together` collection "
            "are not reported, because whether `AlterIndexTogether` builds an "
            "index or drops one depends on prior state this rule does not "
            "replay.",
        ),
    )

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        op = applied.operation
        if op.name != "AddIndex":
            return
        # Indexing a table created in this same deploy builds over no rows.
        if not self.populated(applied):
            return

        migration = applied.migration
        # `AddIndexConcurrently` cannot run in a transaction, so a migration
        # that is still atomic needs both edits. Saying which one is missing
        # keeps the reader from making the change that then refuses to run.
        also_atomic = (
            " The migration is atomic, so it also needs `atomic = False`."
            if migration.atomic
            else ""
        )
        yield self.finding(
            location=self.locate(ctx, migration, op),
            message=(
                f"`{migration.app}.{migration.name}` adds an index to "
                f"`{op.model_name}`, which Postgres builds under a `SHARE` "
                f"lock -- writes to the table block until it completes."
                f"{also_atomic}"
            ),
            evidence=(
                self.excerpt(ctx, migration, op),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"migration atomic={migration.atomic}; "
                        f"`AddIndexConcurrently` requires atomic=False"
                    ),
                    source=f"{ctx.rel(migration.path)}:{op.lineno}",
                ),
            ),
        )
