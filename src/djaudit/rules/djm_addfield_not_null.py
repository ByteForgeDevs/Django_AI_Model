"""`DJM-001` -- a non-nullable column added with nothing to fill it.

The plan specified this rule as "`AddField` non-nullable with a default,
rewriting the table". Measured against the versions of Postgres Django actually
supports, that premise is obsolete: Postgres 11 made `ADD COLUMN ... DEFAULT
<constant>` a catalogue-only change, and Django's own floor is Postgres 14. A
non-nullable column *with* a default is fast, and reporting it would have
flagged 307 operations across the three benchmark projects for a rewrite that
does not happen.

The failure that does still happen is the opposite one. `ADD COLUMN ... NOT
NULL` with no value to put in existing rows is rejected outright -- Postgres
raises `column "x" of relation "y" contains null values` -- and the migration
aborts part-applied. That is not a performance note; it is a deploy that stops
halfway.

Django defends against this in two places, and both have to be subtracted or
the rule is mostly wrong:

* `makemigrations` refuses to write such a field without prompting for a
  one-off default, so the ones that survive are largely **hand-written** -- and
  hand-written migrations are exactly this family's target.
* `Field.get_default()` returns `""` rather than `None` for fields whose
  `empty_strings_allowed` is set, so a `CharField` with no default still
  supplies a value for every existing row. Those are safe and must not be
  reported.
"""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.migrations.state import Applied
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._migrations import EMPTY_STRING_FIELDS, TABLE_VALUED_FIELDS, MigrationRule


@register
class NonNullableAddFieldWithoutDefault(MigrationRule):
    """`AddField` of a NOT NULL column with no value for the rows already there."""

    meta = RuleMeta(
        id="DJM-001",
        title="Non-nullable column added with no default aborts on a populated table",
        family=Family.DJM,
        tier=Tier.STATIC,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        rationale=(
            "`ADD COLUMN ... NOT NULL` has to put something in every row that "
            "already exists. With no default and no usable empty value, "
            "Postgres rejects the statement and the migration aborts -- after "
            "any earlier operation in the same file has already run. The table "
            "is empty in development and in CI, so this passes every test and "
            "fails only against production data."
        ),
        remediation=(
            "Give the field a `default`, which on Postgres 11 and later is a "
            "catalogue-only change and does not rewrite the table. Where the "
            "value has to be computed per row, use the three-step form "
            "instead: add the column nullable, backfill it in batches, then "
            "`AlterField` it to non-nullable once no row is null."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/howto/writing-migrations/",
            "https://www.postgresql.org/docs/current/sql-altertable.html",
        ),
        limitations=(
            "Static analysis cannot tell which migrations have been applied, so "
            "only the leaf of each app's history is reported -- the tip, where "
            "a migration being written now lands. A change adding two "
            "migrations to one app has only its second one examined. The live "
            "tier reads `django_migrations` and does not have to guess.",
            "Assumes the table already holds rows. A column added to a table "
            "that is empty in production succeeds, and nothing static "
            "distinguishes the two.",
            "Subtracts the field types whose `empty_strings_allowed` makes "
            "Django supply `''`. A third-party field that sets the same flag is "
            "not in that list and would be reported.",
            "A default supplied by a custom field class rather than by the "
            "`default=` argument is not visible to a parser and would be "
            "reported.",
        ),
    )

    def inspect(self, ctx: ProjectContext, applied: Applied) -> Iterator[Finding]:
        op = applied.operation
        field = op.field
        if op.name != "AddField" or field is None:
            return
        # A column added to a table with no rows in it cannot fail on NOT NULL,
        # whatever it declares.
        if not self.populated(applied):
            return
        if field.null or field.has_default:
            return
        # No column, so nothing to constrain: these build their own table.
        if field.kind in TABLE_VALUED_FIELDS:
            return
        # Django fills these itself, so the statement succeeds.
        if field.kind in EMPTY_STRING_FIELDS:
            return
        # "null" that could not be read is not "null=False". A field whose
        # nullability came from a setting or a version check is a field we
        # cannot speak about.
        if "null" in field.unreadable or "default" in field.unreadable:
            return

        migration = applied.migration
        yield self.finding(
            location=self.locate(ctx, migration, op),
            message=(
                f"`{migration.app}.{migration.name}` adds non-nullable "
                f"`{op.model_name}.{op.field_name}` ({field.kind}) with no default. "
                f"Postgres has nothing to write into the rows already in the table, "
                f"so the migration aborts."
            ),
            evidence=(
                self.excerpt(ctx, migration, op),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"table for {op.model_name} is created by "
                        f"{applied.created_by or 'an earlier migration'}, so it holds "
                        f"rows by the time this runs"
                    ),
                    source=f"{ctx.rel(migration.path)}:{op.lineno}",
                ),
            ),
        )
