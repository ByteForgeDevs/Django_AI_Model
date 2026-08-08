"""Tests for `DJM-008`, and for the line between it and `DJM-006`.

The class carrying the design is `TestTheOrderIsTheRule`. `DJM-006` looks at
the same two operation kinds in the same migration and reaches a different
answer, so several tests here are written as contrasts against it rather than
as assertions about this rule alone.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Severity, Tier
from djaudit.rules.djm_irreversible_blocks_rollback import (
    IrreversibleDataOperationBlocksRollback,
)
from djaudit.rules.djm_schema_and_data_in_one_transaction import SchemaAndDataInOneTransaction
from tests.rules.test_djm_addfield_not_null import Build, findings_for, migration, project

ADD = (
    "migrations.AddField(model_name='order', name='code', field=models.IntegerField(default=0)),\n"
)
ALTER = "migrations.AlterField(model_name='order', name='code', field=models.IntegerField()),\n"
INDEX = (
    "migrations.AddIndex(model_name='order', "
    "index=models.Index(fields=['code'], name='order_code_idx')),\n"
)
OPTIONS = "migrations.AlterModelOptions(name='order', options={'ordering': ['code']}),\n"
DATA = "migrations.RunPython(code=forward, reverse_code=migrations.RunPython.noop),\n"
SQL = (
    "migrations.RunSQL(sql='UPDATE app_order SET code = 0', reverse_sql=migrations.RunSQL.noop),\n"
)

FORWARD = "def forward(apps, schema_editor):\n    pass\n\n"


def build(build_project: Build, operations: str, atomic: bool | None = None) -> ProjectContext:
    """One migration holding ``operations``, with a `forward` for `RunPython`."""
    source = migration(operations, atomic=atomic).replace(
        "class Migration(", f"{FORWARD}\nclass Migration("
    )
    return project(build_project, {"shop/migrations/0001_initial.py": source})


def findings(ctx: ProjectContext) -> list[str]:
    return findings_for(SchemaAndDataInOneTransaction(), ctx)


class TestTheOrderIsTheRule:
    """A lock is held from when it is taken until the transaction ends.

    So a data pass *after* a schema change extends that hold, and the same two
    operations the other way round do not.
    """

    def test_schema_then_data_is_reported(self, make_project: Build) -> None:
        assert len(findings(build(make_project, ADD + DATA))) == 1

    def test_data_then_schema_is_not(self, make_project: Build) -> None:
        """The lock is taken at the end and released almost at once."""
        assert findings(build(make_project, DATA + ADD)) == []

    def test_data_alone_is_not(self, make_project: Build) -> None:
        assert findings(build(make_project, DATA)) == []

    def test_schema_alone_is_not(self, make_project: Build) -> None:
        assert findings(build(make_project, ADD)) == []

    def test_a_later_schema_operation_does_not_rescue_an_earlier_one(
        self, make_project: Build
    ) -> None:
        """`ADD, DATA, ADD` still holds the first lock across the backfill."""
        assert len(findings(build(make_project, ADD + DATA + ADD))) == 1


class TestItIsNotDjm006:
    """Same two kinds, same migration, different question.

    `DJM-006` asks whether a failed deploy can be unwound. This asks what the
    table does while the deploy is still going forwards. Each fires where the
    other is silent, and the two tests below are that claim measured rather
    than asserted in a docstring.
    """

    def test_a_reversible_backfill_after_a_schema_change_is_this_rule_only(
        self, make_project: Build
    ) -> None:
        ctx = build(make_project, ADD + DATA)
        assert len(findings(ctx)) == 1
        assert findings_for(IrreversibleDataOperationBlocksRollback(), ctx) == []

    def test_an_irreversible_backfill_before_a_schema_change_is_djm_006_only(
        self, make_project: Build
    ) -> None:
        irreversible = "migrations.RunPython(code=forward),\n"
        ctx = build(make_project, irreversible + ADD)
        assert findings(ctx) == []
        assert len(findings_for(IrreversibleDataOperationBlocksRollback(), ctx)) == 1


class TestAtomicIsTheOtherRemediation:
    def test_a_non_atomic_migration_commits_each_operation_separately(
        self, make_project: Build
    ) -> None:
        assert findings(build(make_project, ADD + DATA, atomic=False)) == []

    def test_an_explicit_atomic_true_is_reported_like_the_default(
        self, make_project: Build
    ) -> None:
        """Django's default is True and most migrations do not say so.

        Writing it out has to reach the same answer as leaving it off, or the
        rule would be reporting a spelling rather than a behaviour.
        """
        assert len(findings(build(make_project, ADD + DATA, atomic=True))) == 1

    def test_the_remediation_names_both_ways_out(self) -> None:
        remediation = SchemaAndDataInOneTransaction.meta.remediation
        assert "own" in remediation and "atomic = False" in remediation


class TestWhichOperationsTakeALock:
    def test_an_index_build_takes_one(self, make_project: Build) -> None:
        assert len(findings(build(make_project, INDEX + DATA))) == 1

    def test_a_state_only_operation_does_not(self, make_project: Build) -> None:
        """`AlterModelOptions` emits no SQL, so it is holding nothing."""
        assert findings(build(make_project, OPTIONS + DATA)) == []

    def test_an_unrecognised_operation_is_not_assumed_to_emit_ddl(
        self, make_project: Build
    ) -> None:
        """The documented quiet direction, held to by a test.

        A third-party operation may emit no DDL at all. Reporting one on the
        chance that it does would put the burden of proof on the wrong side.
        """
        assert findings(build(make_project, "myapp.operations.Warm(),\n" + DATA)) == []

    def test_run_sql_is_a_data_operation_too(self, make_project: Build) -> None:
        assert len(findings(build(make_project, ADD + SQL))) == 1

    def test_a_nested_schema_operation_takes_its_lock_where_the_wrapper_sits(
        self, make_project: Build
    ) -> None:
        wrapper = (
            "migrations.SeparateDatabaseAndState(\n"
            "    database_operations=[migrations.AddField(model_name='order', "
            "name='code', field=models.IntegerField(default=0))],\n"
            "),\n"
        )
        assert len(findings(build(make_project, wrapper + DATA))) == 1

    def test_a_nested_data_operation_is_found_too(self, make_project: Build) -> None:
        wrapper = (
            "migrations.SeparateDatabaseAndState(\n"
            "    database_operations=[migrations.RunPython(code=forward, "
            "reverse_code=migrations.RunPython.noop)],\n"
            "),\n"
        )
        assert len(findings(build(make_project, ADD + wrapper))) == 1


CREATE = """\
migrations.CreateModel(
    name='Invoice',
    fields=[('id', models.AutoField(primary_key=True, serialize=False))],
),
"""


class TestALockOnANewTableBlocksNobody:
    """The most common honest reason to mix schema and data in one migration.

    A migration that creates its tables and then seeds them takes
    `ACCESS EXCLUSIVE` on tables no other session can see until the transaction
    commits -- by which time the lock is gone. Reporting it would be pure cost,
    and its remediation would make the seed data land in a separate deploy step
    for no gain.
    """

    def test_creating_a_table_and_seeding_it_is_not_reported(self, make_project: Build) -> None:
        assert findings(build(make_project, CREATE + DATA)) == []

    def test_a_new_table_beside_an_old_one_names_only_the_old_one(
        self, make_project: Build
    ) -> None:
        """`populated` cannot answer for a `CreateModel`.

        It reports the state *preceding* the operation, and before a
        `CreateModel` the table has no creator on record, so it reads as one
        that has been there all along. Here `order` really has -- `0001`
        created it and `0001` is no longer a leaf -- and `invoice` has not.
        """
        first = migration(
            "migrations.CreateModel(name='Order', fields=["
            "('id', models.AutoField(primary_key=True, serialize=False))]),\n"
        )
        second = migration(CREATE + ADD + DATA, deps="('shop', '0001_initial')").replace(
            "class Migration(", f"{FORWARD}\nclass Migration("
        )
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": first,
                "shop/migrations/0002_mixed.py": second,
            },
        )
        (message,) = findings(ctx)
        assert "`order`" in message
        assert "invoice" not in message
        assert "1 earlier schema operation," in message


class TestOneFindingPerMigration:
    def test_two_backfills_report_once(self, make_project: Build) -> None:
        """Moving the first one out is the fix; naming the second adds nothing."""
        assert len(findings(build(make_project, ADD + DATA + DATA))) == 1

    def test_the_finding_lands_on_the_first_data_operation(self, make_project: Build) -> None:
        ctx = build(make_project, ADD + SQL + DATA)
        (finding,) = list(SchemaAndDataInOneTransaction().check(ctx))
        assert "RunSQL" in finding.evidence[0].content.split("<-- data")[0].split("->")[-1]


class TestScope:
    def test_a_superseded_migration_is_out_of_scope(self, make_project: Build) -> None:
        later = (
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = []\n"
        )
        source = migration(ADD + DATA).replace("class Migration(", f"{FORWARD}\nclass Migration(")
        ctx = project(
            make_project,
            {"shop/migrations/0001_initial.py": source, "shop/migrations/0002_later.py": later},
        )
        assert findings(ctx) == []


class TestTheMessageAndEvidence:
    def test_the_message_counts_the_locks_and_names_the_tables(self, make_project: Build) -> None:
        (message,) = findings(build(make_project, ADD + ALTER + DATA))
        assert "2 earlier schema operations" in message
        assert "`order`" in message
        assert "ACCESS EXCLUSIVE" in message

    def test_the_count_is_singular_for_one_operation(self, make_project: Build) -> None:
        """A plural that ignores the count reads as a bug to whoever gets it."""
        (message,) = findings(build(make_project, ADD + DATA))
        assert "1 earlier schema operation," in message

    def test_the_tables_are_deduplicated_but_keep_file_order(self, make_project: Build) -> None:
        other = (
            "migrations.AddField(model_name='invoice', name='code', "
            "field=models.IntegerField(default=0)),\n"
        )
        (message,) = findings(build(make_project, ADD + other + ALTER + DATA))
        assert "`order`, `invoice`" in message

    def test_the_whole_message_reads_as_written(self, make_project: Build) -> None:
        """Asserted in full rather than by substring.

        Every fragment of this sentence survived mutation while the tests
        checked pieces of it, because a blanked backtick or a blanked clause
        still contains the piece each `in` was looking for.
        """
        (message,) = findings(build(make_project, ADD + DATA))
        assert message == (
            "`shop.0001_initial` runs `RunPython` in the same transaction as 1 "
            "earlier schema operation, so the `ACCESS EXCLUSIVE` lock on `order` "
            "is held for as long as this data pass takes. Move the data "
            "operation into its own migration, or set `atomic = False`."
        )

    def test_both_evidence_entries_cite_the_operations_line(self, make_project: Build) -> None:
        ctx = build(make_project, ADD + DATA)
        (finding,) = list(SchemaAndDataInOneTransaction().check(ctx))
        cited = f"shop/migrations/0001_initial.py:{finding.location.line}"
        assert finding.evidence[0].source == cited
        assert finding.evidence[1].source == cited

    def test_the_evidence_lays_out_the_operation_order(self, make_project: Build) -> None:
        ctx = build(make_project, ADD + ALTER + DATA)
        (finding,) = list(SchemaAndDataInOneTransaction().check(ctx))
        assert finding.evidence[0].kind is EvidenceKind.AST
        assert finding.evidence[0].content == (
            "AddField(order) -> AlterField(order) -> RunPython  <-- data"
        )

    def test_the_second_evidence_states_the_postgres_rule(self, make_project: Build) -> None:
        ctx = build(make_project, ADD + DATA)
        (finding,) = list(SchemaAndDataInOneTransaction().check(ctx))
        assert finding.evidence[1].kind is EvidenceKind.CONFIG
        assert finding.evidence[1].content == (
            "atomic = True; Postgres holds a lock until the end of the transaction"
        )


class TestMeta:
    def test_identity(self) -> None:
        meta = SchemaAndDataInOneTransaction.meta
        assert meta.id == "DJM-008"
        assert meta.family is Family.DJM
        assert meta.tier is Tier.STATIC
        assert meta.severity is Severity.HIGH
        assert meta.confidence is Confidence.FIRM

    def test_the_leaf_scope_caveat_is_declared(self) -> None:
        assert any(
            "leaf" in limitation and "django_migrations" in limitation
            for limitation in SchemaAndDataInOneTransaction.meta.limitations
        )

    def test_the_duration_is_declared_unknowable(self) -> None:
        assert any(
            "not knowable from source" in limitation
            for limitation in SchemaAndDataInOneTransaction.meta.limitations
        )

    def test_the_sqlite_divergence_is_declared(self) -> None:
        assert any(
            "SQLite" in limitation for limitation in SchemaAndDataInOneTransaction.meta.limitations
        )

    def test_the_unknown_operation_gap_is_declared(self) -> None:
        assert any(
            "third-party" in limitation
            for limitation in SchemaAndDataInOneTransaction.meta.limitations
        )

    def test_the_postgres_lock_page_is_cited(self) -> None:
        assert any(
            "explicit-locking" in reference
            for reference in SchemaAndDataInOneTransaction.meta.references
        )
