"""`DJM-002` -- an `AlterField` that locks the table for as long as it takes.

Two distinct changes share one operation and one consequence, so they share a
rule. Both hold `ACCESS EXCLUSIVE` -- which blocks reads as well as writes, so
the table is unavailable, not merely unwritable -- for a duration set by the
number of rows rather than by anything in the migration.

* **Changing a column's type** rewrites every row, because the on-disk
  representation changes. Postgres holds the lock for the whole rewrite.
* **Tightening `null=True` to `null=False`** issues `SET NOT NULL`, which scans
  the entire table to prove no row violates it. Postgres 12 can skip that scan
  when a matching `CHECK` constraint is already proven, but Django never emits
  one, so the scan always happens.

The precision problem is that not every type change rewrites. Postgres has
skipped the rewrite for a widened or dropped `varchar` limit since 9.2, and
those are common and deliberate -- a `max_length` raised from 100 to 200 is the
single most ordinary migration a Django project produces. Reporting them would
bury the changes that do cost something.
"""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.graph.nodes import FieldNode
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import MigrationRule

VARIABLE_LENGTH_TEXT = frozenset({"CharField", "TextField", "SlugField", "EmailField", "URLField"})
"""Fields backed by `varchar(n)` or `text`, which share a storage format.

Postgres rewrites nothing when a `varchar` limit grows or is dropped, because
the value on disk is unchanged and only the constraint differs. Moving between
these kinds, or raising `max_length`, is therefore free.
"""


RELATION_FIELDS = frozenset({"ForeignKey", "OneToOneField"})
"""Fields storing the referenced row's primary key in a column on this table.

Moving between them changes whether a `UNIQUE` constraint exists, not how the
value is stored, so Postgres builds or drops an index rather than rewriting the
table. That is not free -- the index build takes a lock and scans -- but it is a
different cost with a different remedy (`CREATE UNIQUE INDEX CONCURRENTLY`, then
add the constraint using it), and calling it a rewrite would send the reader
after the wrong fix. Found by triage: pretix turns a `ForeignKey` into a
`OneToOneField` in `multidomain.0003`, which this rule first reported as a
rewrite.
"""


def no_rewrite(before: FieldNode, after: FieldNode) -> bool:
    """Whether this type change leaves the stored bytes alone.

    Two cases are claimed, and only two. Integer widening (`integer` to
    `bigint`) is *not* among them -- the width on disk really does change -- and
    is common enough to be worth not getting backwards: pretix's `*_bigint.py`
    migrations are exactly that, and they are this rule's best real findings.
    """
    if before.kind in RELATION_FIELDS and after.kind in RELATION_FIELDS:
        return True
    if before.kind not in VARIABLE_LENGTH_TEXT or after.kind not in VARIABLE_LENGTH_TEXT:
        return False
    if after.max_length is None:
        # Dropping the limit entirely, e.g. CharField to TextField.
        return True
    if before.max_length is None:
        # text to varchar(n) adds a constraint Postgres has to verify.
        return False
    return after.max_length >= before.max_length


@register
class TableRewritingAlterField(MigrationRule):
    """`AlterField` whose lock is held for the length of the table."""

    meta = RuleMeta(
        id="DJM-002",
        title="AlterField holds an exclusive lock for the length of the table",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        rationale=(
            "Changing a column's type rewrites every row, and tightening a "
            "column to `NOT NULL` scans every row to prove the constraint "
            "holds. Postgres takes `ACCESS EXCLUSIVE` for both, which blocks "
            "reads as well as writes -- the table is unavailable for the "
            "duration, not merely unwritable. The duration is set by the row "
            "count, so this is instant in CI and an outage in production."
        ),
        remediation=(
            "For a type change, add the new column, backfill it in batches, "
            "switch reads over, then drop the old one. For a `NOT NULL`, add a "
            "`CHECK (column IS NOT NULL) NOT VALID` constraint, `VALIDATE` it "
            "under a lock that permits reads and writes, and only then set the "
            "column `NOT NULL` -- Postgres 12 and later recognise the proven "
            "constraint and skip the scan."
        ),
        references=(
            "https://www.postgresql.org/docs/current/sql-altertable.html",
            "https://docs.djangoproject.com/en/stable/howto/writing-migrations/",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported. The live tier "
            "reads `django_migrations` and does not have to guess.",
            "Says nothing about how many rows the table holds, which is what "
            "decides whether the lock is a blip or an outage. The live tier's "
            "`pg_class.reltuples` sizing answers that; static analysis cannot.",
            "Treats a change between text-shaped fields with a non-shrinking "
            "`max_length` as free, which holds for Postgres. A backend that "
            "rewrites on a widened `varchar` would be under-reported.",
            "Compares the field as the migration declares it against the state "
            "replayed from earlier migrations. Where an earlier migration was "
            "unreadable the prior column is unknown and nothing is reported.",
        ),
    )

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        op = applied.operation
        after = op.field
        before = applied.existing_field
        if op.name != "AlterField" or after is None or before is None:
            return
        # An empty table rewrites and scans instantly.
        if not self.populated(applied):
            return

        tightened = before.null and not after.null
        retyped = before.kind != after.kind and not no_rewrite(before, after)
        # A narrowed max_length within one kind rewrites too: Postgres has to
        # verify every existing value against the shorter limit.
        narrowed = (
            before.kind == after.kind
            and before.kind in VARIABLE_LENGTH_TEXT
            and not no_rewrite(before, after)
        )
        if not (tightened or retyped or narrowed):
            return
        # Only the *new* field's nullability needs guarding, and the asymmetry
        # is not an oversight. An unreadable `null` falls back to Django's
        # default of False, which on the prior column means `before.null` is
        # already False and no tightening is claimed -- but on the new column it
        # looks exactly like a deliberate `null=False` and would invent a
        # `SET NOT NULL` that the migration may not perform.
        if not after.knows("null"):
            return

        reasons = []
        if retyped or narrowed:
            reasons.append(
                f"`{before.kind}"
                f"{f'({before.max_length})' if before.max_length is not None else ''}` "
                f"becomes `{after.kind}"
                f"{f'({after.max_length})' if after.max_length is not None else ''}`, "
                f"which rewrites every row"
            )
        if tightened:
            reasons.append("the column becomes `NOT NULL`, which scans every row to prove it")

        migration = applied.migration
        yield self.finding(
            location=self.locate(ctx, migration, op),
            message=(
                f"`{migration.app}.{migration.name}` alters "
                f"`{op.model_name}.{op.field_name}`: {' and '.join(reasons)}. Postgres "
                f"holds `ACCESS EXCLUSIVE` throughout, so reads block too."
            ),
            evidence=(
                self.excerpt(ctx, migration, op),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"prior column, replayed from earlier migrations: "
                        f"{before.kind}(max_length={before.max_length}, null={before.null})"
                    ),
                    source=f"{ctx.rel(migration.path)}:{op.lineno}",
                ),
            ),
        )
