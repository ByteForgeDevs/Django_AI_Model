"""Tests for `DJM-005`, and for the two shapes that make it more than a name match.

The classes worth reading first are `TestAPinnedColumnDoesNotMove` and
`TestAPinnedTableDoesNotMove`. Django emits a rename statement only when
`old_field.column != new_field.column`, so a rename with `db_column` held
steady is a Python-side change that touches no schema at all -- and it is
exactly what this rule's own remediation asks for. Both orderings of that
pattern are tested, because Django generates one and real projects write the
other.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Finding, Severity
from djaudit.rules.djm_rename_rolling_deploy import RenameBreaksRollingDeploy
from tests.rules.test_djm_addfield_not_null import Build, findings_for, migration, project

CREATE = """\
migrations.CreateModel(
    name='Order',
    fields=[
        ('id', models.AutoField(primary_key=True, serialize=False)),
        ('code', models.CharField(max_length=10)),
        ('note', models.CharField(max_length=10)),
        ('owner', models.ForeignKey(to='shop.User', on_delete=models.CASCADE)),
        ('tags', models.ManyToManyField(to='shop.Tag')),
    ],
),
"""


def findings(ctx: ProjectContext) -> list[str]:
    return findings_for(RenameBreaksRollingDeploy(), ctx)


def results(ctx: ProjectContext) -> list[Finding]:
    return list(RenameBreaksRollingDeploy().check(ctx))


def history(second: str) -> dict[str, str]:
    """An app whose leaf migration is ``second``, on a table created earlier."""
    return {
        "shop/migrations/0001_initial.py": migration(CREATE),
        "shop/migrations/0002_change.py": migration(second, deps="('shop', '0001_initial')"),
    }


RENAME = "migrations.RenameField(model_name='order', old_name='code', new_name='reference'),\n"


class TestTheOldNameIsStillQueried:
    def test_reports_a_plain_rename_field(self, make_project: Build) -> None:
        ctx = project(make_project, history(RENAME))

        assert findings(ctx) == [
            "`shop.0002_change` renames `order.code` to `reference`, moving "
            "column `code` to `reference`. Django names every concrete column "
            "in its `SELECT`, so every query the release still running makes "
            "against `order` fails for the length of the deploy."
        ]

    def test_the_finding_is_high(self, make_project: Build) -> None:
        ctx = project(make_project, history(RENAME))

        assert [f.severity for f in results(ctx)] == [Severity.HIGH]

    def test_reports_each_renamed_column_separately(self, make_project: Build) -> None:
        both = RENAME + (
            "migrations.RenameField(model_name='order', old_name='note', new_name='memo'),\n"
        )
        ctx = project(make_project, history(both))

        assert len(findings(ctx)) == 2

    def test_a_renamed_relation_moves_its_id_column(self, make_project: Build) -> None:
        """Django names a relation's column after `attname`, so the column that
        moves is `owner_id` rather than `owner`. Saying `owner` would send a
        reader looking for a column that never existed."""
        rename = "migrations.RenameField(model_name='order', old_name='owner', new_name='buyer'),\n"
        ctx = project(make_project, history(rename))

        assert "moving column `owner_id` to `buyer_id`" in findings(ctx)[0]

    def test_reports_a_rename_model(self, make_project: Build) -> None:
        rename = "migrations.RenameModel(old_name='Order', new_name='Purchase'),\n"
        ctx = project(make_project, history(rename))

        assert findings(ctx) == [
            "`shop.0002_change` renames model `Order` to `Purchase`, moving "
            "table `shop_order` to `shop_purchase`. Every query the release "
            "still running makes against it names the old table, and fails for "
            "the length of the deploy."
        ]

    def test_a_rename_model_is_high_too(self, make_project: Build) -> None:
        # A table has no lesser many-to-many case: moving it takes every query
        # with it.
        rename = "migrations.RenameModel(old_name='Order', new_name='Purchase'),\n"
        ctx = project(make_project, history(rename))

        assert [f.severity for f in results(ctx)] == [Severity.HIGH]


class TestTheEvidence:
    """The message argues; the evidence is what a reader checks it against."""

    def test_a_moved_column_names_both_ends(self, make_project: Build) -> None:
        ctx = project(make_project, history(RENAME))

        assert [e.content for e in results(ctx)[0].evidence][1] == (
            "column code -> reference; no db_column holds it in place"
        )

    def test_a_moved_table_names_both_ends(self, make_project: Build) -> None:
        rename = "migrations.RenameModel(old_name='Order', new_name='Purchase'),\n"
        ctx = project(make_project, history(rename))

        assert [e.content for e in results(ctx)[0].evidence][1] == (
            "table shop_order -> shop_purchase; no db_table holds it in place"
        )

    def test_a_moved_join_table_says_it_follows_the_field(self, make_project: Build) -> None:
        m2m = "migrations.RenameField(model_name='order', old_name='tags', new_name='labels'),\n"
        ctx = project(make_project, history(m2m))

        assert [e.content for e in results(ctx)[0].evidence][1] == (
            "join table follows the field name; tags -> labels"
        )

    def test_the_evidence_cites_the_operation_not_the_migration(self, make_project: Build) -> None:
        ctx = project(make_project, history(RENAME))
        finding = results(ctx)[0]

        assert finding.evidence[1].source == (
            f"shop/migrations/0002_change.py:{finding.location.line}"
        )


class TestAPinnedColumnDoesNotMove:
    """`db_column` is the remediation, so reporting it would be reporting the fix.

    `_alter_field` emits its rename statement only under `if old_field.column !=
    new_field.column`, so these migrations change the Python name and emit no
    DDL for it at all.
    """

    def test_a_pin_written_after_the_rename_is_seen(self, make_project: Build) -> None:
        # Django's own order: the autodetector runs generate_renamed_fields()
        # before generate_altered_fields(), so this is what makemigrations
        # writes and therefore the shape that matters most.
        ops = RENAME + (
            "migrations.AlterField(model_name='order', name='reference', "
            "field=models.CharField(db_column='code', max_length=10)),\n"
        )
        ctx = project(make_project, history(ops))

        assert findings(ctx) == []

    def test_a_pin_written_before_the_rename_is_seen_too(self, make_project: Build) -> None:
        # pretix's 0254_alter_logentry_organizer_link_and_more has this order.
        ops = (
            "migrations.AlterField(model_name='order', name='code', "
            "field=models.CharField(db_column='code', max_length=10)),\n"
        ) + RENAME
        ctx = project(make_project, history(ops))

        assert findings(ctx) == []

    def test_a_field_that_already_pinned_its_column_does_not_move_either(
        self, make_project: Build
    ) -> None:
        # No AlterField anywhere: the column was pinned when the model was
        # created, so renaming the attribute cannot move it.
        created = """\
migrations.CreateModel(
    name='Order',
    fields=[
        ('id', models.AutoField(primary_key=True, serialize=False)),
        ('code', models.CharField(db_column='order_code', max_length=10)),
    ],
),
"""
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(created),
                "shop/migrations/0002_change.py": migration(
                    RENAME, deps="('shop', '0001_initial')"
                ),
            },
        )

        assert findings(ctx) == []

    def test_but_a_pin_that_moves_the_column_is_still_reported(self, make_project: Build) -> None:
        # The contrast, and the reason this cannot be a test for the mere
        # presence of db_column: pinning the column somewhere it was not moves
        # it just as surely as a rename does.
        ops = RENAME + (
            "migrations.AlterField(model_name='order', name='reference', "
            "field=models.CharField(db_column='elsewhere', max_length=10)),\n"
        )
        ctx = project(make_project, history(ops))

        assert "moving column `code` to `elsewhere`" in findings(ctx)[0]

    def test_a_pin_on_another_model_does_not_count(self, make_project: Build) -> None:
        ops = RENAME + (
            "migrations.AlterField(model_name='invoice', name='reference', "
            "field=models.CharField(db_column='code', max_length=10)),\n"
        )
        ctx = project(make_project, history(ops))

        assert len(findings(ctx)) == 1

    def test_a_pin_on_another_field_does_not_count(self, make_project: Build) -> None:
        ops = RENAME + (
            "migrations.AlterField(model_name='order', name='note', "
            "field=models.CharField(db_column='code', max_length=10)),\n"
        )
        ctx = project(make_project, history(ops))

        assert len(findings(ctx)) == 1

    def test_a_pin_nobody_can_read_is_not_reported_on(self, make_project: Build) -> None:
        # The value is a name rather than a string, so whether the column moves
        # is unknown -- and an unknown is not a finding.
        ops = RENAME + (
            "migrations.AlterField(model_name='order', name='reference', "
            "field=models.CharField(db_column=LEGACY, max_length=10)),\n"
        )
        ctx = project(make_project, history(ops))

        assert findings(ctx) == []


class TestAPinnedTableDoesNotMove:
    def test_a_model_pinned_to_its_old_table_is_not_reported(self, make_project: Build) -> None:
        # The autodetector runs generate_altered_db_table() after
        # generate_renamed_models(), so the pin names the model as it will be.
        ops = "migrations.RenameModel(old_name='Order', new_name='Purchase'),\n" + (
            "migrations.AlterModelTable(name='purchase', table='shop_order'),\n"
        )
        ctx = project(make_project, history(ops))

        assert findings(ctx) == []

    def test_a_pin_naming_the_model_as_it_was_is_seen_too(self, make_project: Build) -> None:
        ops = ("migrations.AlterModelTable(name='order', table='shop_order'),\n") + (
            "migrations.RenameModel(old_name='Order', new_name='Purchase'),\n"
        )
        ctx = project(make_project, history(ops))

        assert findings(ctx) == []

    def test_but_a_table_moved_somewhere_else_is_reported(self, make_project: Build) -> None:
        ops = "migrations.RenameModel(old_name='Order', new_name='Purchase'),\n" + (
            "migrations.AlterModelTable(name='purchase', table='shop_elsewhere'),\n"
        )
        ctx = project(make_project, history(ops))

        assert "moving table `shop_order` to `shop_elsewhere`" in findings(ctx)[0]

    def test_a_pin_on_another_model_does_not_count(self, make_project: Build) -> None:
        # The pin has to be this model's. Reading any AlterModelTable in the
        # migration would have the rule report the wrong destination table,
        # which is worse than reporting none.
        ops = "migrations.RenameModel(old_name='Order', new_name='Purchase'),\n" + (
            "migrations.AlterModelTable(name='invoice', table='shop_invoice'),\n"
        )
        ctx = project(make_project, history(ops))

        assert "moving table `shop_order` to `shop_purchase`" in findings(ctx)[0]


class TestTheDeliberateSeparationIsNotReported:
    """NetBox's `tenancy.0020_remove_contactgroupmembership` is this shape.

    It renames a table and a column inside `SeparateDatabaseAndState` to turn an
    explicit through-model into an implicit M2M. The replay flattens the wrapper
    to its database half, so the rename arrives here looking exactly like a
    careless one, and only `via_separate` tells them apart.
    """

    WRAPPED = """\
migrations.SeparateDatabaseAndState(
    database_operations=[
        migrations.RenameField(model_name='order', old_name='code', new_name='reference'),
    ],
),
"""

    def test_a_wrapped_rename_is_not_reported(self, make_project: Build) -> None:
        ctx = project(make_project, history(self.WRAPPED))

        assert findings(ctx) == []

    def test_the_contrast_is_real(self, make_project: Build) -> None:
        # The same rename, unwrapped, in the same project: proof that the
        # silence above comes from the wrapper and not from the rename being
        # unreachable, mis-parsed, or against an untracked model.
        ctx = project(make_project, history(self.WRAPPED + RENAME))

        assert len(findings(ctx)) == 1

    def test_a_wrapped_rename_model_is_not_reported_either(self, make_project: Build) -> None:
        wrapped = """\
migrations.SeparateDatabaseAndState(
    database_operations=[
        migrations.RenameModel(old_name='Order', new_name='Purchase'),
    ],
),
"""
        ctx = project(make_project, history(wrapped))

        assert findings(ctx) == []


class TestJoinTablesAreALesserCase:
    M2M = "migrations.RenameField(model_name='order', old_name='tags', new_name='labels'),\n"

    def test_a_renamed_many_to_many_says_what_actually_breaks(self, make_project: Build) -> None:
        ctx = project(make_project, history(self.M2M))

        assert findings(ctx) == [
            "`shop.0002_change` renames `order.tags` to `labels`, renaming its "
            "join table with it. Code in the release still running during the "
            "deploy fails as soon as it traverses the relation."
        ]

    def test_and_is_reported_a_rank_lower(self, make_project: Build) -> None:
        ctx = project(make_project, history(self.M2M))

        assert [f.severity for f in results(ctx)] == [Severity.MEDIUM]


class TestATableNobodyHasQueriedYet:
    def test_a_rename_beside_its_own_create_model_is_not_reported(
        self, make_project: Build
    ) -> None:
        # No released code has ever queried this table under either name.
        ctx = project(make_project, {"shop/migrations/0001_initial.py": migration(CREATE + RENAME)})

        assert findings(ctx) == []

    def test_but_the_same_rename_a_migration_later_is(self, make_project: Build) -> None:
        ctx = project(make_project, history(RENAME))

        assert len(findings(ctx)) == 1


class TestScope:
    def test_only_the_leaf_of_the_history_is_reported(self, make_project: Build) -> None:
        # A rename in shipped history demonstrably ran, so a deploy finding
        # against it is about a deploy that already happened.
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_change.py": migration(
                    RENAME, deps="('shop', '0001_initial')"
                ),
                "shop/migrations/0003_later.py": migration(
                    "migrations.AddField(model_name='order', name='paid', "
                    "field=models.BooleanField(default=False)),\n",
                    deps="('shop', '0002_change')",
                ),
            },
        )

        assert findings(ctx) == []


class TestOperationsThisRuleIgnores:
    def test_a_remove_field_is_not_a_rename(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history("migrations.RemoveField(model_name='order', name='code'),\n"),
        )

        assert findings(ctx) == []

    def test_a_renamed_index_is_not_a_renamed_column(self, make_project: Build) -> None:
        # `RenameIndex` carries `old_name` and `new_name` exactly as the two
        # operations above do, so a rule that asked "does this have two names"
        # rather than "is this one of these two operations" would report it --
        # and renaming an index changes nothing a running release queries.
        ctx = project(
            make_project,
            history(
                "migrations.RenameIndex(model_name='order', "
                "old_name='shop_order_code_idx', new_name='shop_order_ref_idx'),\n"
            ),
        )

        assert findings(ctx) == []

    def test_a_rename_missing_a_name_is_not_reported(self, make_project: Build) -> None:
        # Nothing can be said about where a column went when one end of the
        # move cannot be read.
        ctx = project(
            make_project,
            history("migrations.RenameField(model_name='order', old_name='code'),\n"),
        )

        assert findings(ctx) == []

    def test_a_rename_model_missing_a_name_is_not_reported_either(
        self, make_project: Build
    ) -> None:
        # The model path derives the table it moves to from the new name, so
        # this is the shape that would fail rather than merely mislead.
        ctx = project(make_project, history("migrations.RenameModel(old_name='Order'),\n"))

        assert findings(ctx) == []


class TestTheRuleItself:
    def test_it_declares_the_leaf_scope_limitation(self) -> None:
        assert any("leaf" in item for item in RenameBreaksRollingDeploy.meta.limitations)

    def test_every_limitation_is_a_written_sentence(self) -> None:
        for item in RenameBreaksRollingDeploy.meta.limitations:
            assert item[0].isupper()
            assert item.endswith(".")
