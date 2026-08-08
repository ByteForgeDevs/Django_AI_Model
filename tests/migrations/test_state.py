"""Replaying migrations into the state each one runs against.

`AlterField` says what a column becomes and nothing about what it was, so the
questions `DJM-002` exists to ask -- did this change the type, did this make a
nullable column NOT NULL -- are unanswerable from the operation alone. Replay
is what answers them.

The corpus says the replay reaches every model those three projects declare,
resolves the prior column for 100% of `RemoveField`s and `RenameField`s and
99.6% of `AlterField`s, and ends holding only 8 models it admits it cannot
speak for. These pin why, and in particular pin the three things the corpus
found: that a `RunSQL` body is not a model name, that a rename names its column
in `old_name`, and that a model no `CreateModel` was seen for is a guess rather
than an empty table.
"""

from __future__ import annotations

import textwrap

import pytest

from djaudit.migrations.graph import build_migration_graph
from djaudit.migrations.state import final_state, replay


def migration(operations: str, deps: str = "[]", replaces: str = "") -> str:
    # Dedent the template before substituting, not after: a multi-line
    # operations list has a shallower indent than the template, so dedenting the
    # interpolated result strips by that shallower amount and leaves the class
    # body indented -- a syntax error, and a migration that silently parses to
    # nothing at all.
    template = textwrap.dedent(
        """
        from django.db import migrations, models


        class Migration(migrations.Migration):
            __REPLACES__dependencies = __DEPS__
            operations = __OPS__
        """
    ).lstrip()
    return (
        template.replace("__DEPS__", deps)
        .replace("__OPS__", operations)
        .replace("__REPLACES__", f"replaces = {replaces}\n    " if replaces else "")
    )


@pytest.fixture
def replay_of(make_project):
    """Replay ``{app: {name: source}}`` and hand back the applied operations."""

    def build(apps: dict[str, dict[str, str]]):
        files: dict[str, str] = {}
        for app, migrations_ in apps.items():
            files[f"{app}/__init__.py"] = ""
            files[f"{app}/migrations/__init__.py"] = ""
            for name, source in migrations_.items():
                files[f"{app}/migrations/{name}.py"] = source
        return build_migration_graph(make_project(files))

    return build


def linear(*operations: str) -> dict[str, dict[str, str]]:
    """One app whose migrations each depend on the one before."""
    files = {}
    previous: str | None = None
    for index, ops in enumerate(operations, start=1):
        name = f"{index:04d}_step"
        deps = f"[('shop', '{previous}')]" if previous else "[]"
        files[name] = migration(ops, deps)
        previous = name
    return {"shop": files}


CREATE_AND_ADD = """[
        migrations.CreateModel(
            name='Order',
            fields=[('id', models.AutoField(primary_key=True))],
        ),
        migrations.AddField('order', 'paid', models.BooleanField(default=False)),
    ]"""
"""A table and a column for it, in one migration: the column is against no rows."""

CREATE_ORDER = """[
        migrations.CreateModel(
            name='Order',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('total', models.IntegerField(null=True)),
            ],
        ),
    ]"""


class TestCreateModel:
    def test_a_created_model_holds_the_fields_it_declared(self, replay_of):
        state = final_state(replay_of(linear(CREATE_ORDER)))
        assert set(state.models[("shop", "order")].fields) == {"id", "total"}

    def test_a_declared_field_keeps_what_it_was_declared_with(self, replay_of):
        state = final_state(replay_of(linear(CREATE_ORDER)))
        total = state.models[("shop", "order")].fields["total"]
        assert total.kind == "IntegerField"
        assert total.null is True

    def test_fields_given_positionally_are_read_too(self, replay_of):
        # makemigrations writes fields= but hand-written migrations often do not.
        state = final_state(
            replay_of(
                linear(
                    "[migrations.CreateModel('Order', "
                    "[('id', models.AutoField(primary_key=True))])]"
                )
            )
        )
        assert set(state.models[("shop", "order")].fields) == {"id"}

    def test_a_computed_field_list_makes_the_model_unknown(self, replay_of):
        state = final_state(
            replay_of(linear("[migrations.CreateModel(name='Order', fields=build_fields())]"))
        )
        assert ("shop", "order") in state.unknown

    def test_a_field_list_with_one_unreadable_entry_makes_the_model_unknown(self, replay_of):
        # A partial read is the dangerous answer. Two of these three columns are
        # readable, and recording just those two claims a table that is missing
        # one -- which reads exactly like a table that never had it.
        state = final_state(
            replay_of(
                linear(
                    """[
        migrations.CreateModel(
            name='Order',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('total', models.IntegerField()),
                ('extra', EXTRA_FIELD),
            ],
        ),
    ]"""
                )
            )
        )
        assert ("shop", "order") in state.unknown

    def test_a_field_built_by_an_expression_makes_the_model_unknown(self, replay_of):
        # A call the field reader cannot name: the entry is shaped exactly like
        # a field and still yields nothing, which is a different failure from an
        # entry that was never a call at all.
        state = final_state(
            replay_of(
                linear(
                    """[
        migrations.CreateModel(
            name='Order',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('extra', FIELD_TYPES['json']()),
            ],
        ),
    ]"""
                )
            )
        )
        assert ("shop", "order") in state.unknown

    def test_but_the_columns_it_could_read_are_still_recorded(self, replay_of):
        # Unknown means "do not trust this", not "throw it away": the columns
        # that were readable are still the best evidence there is.
        state = final_state(
            replay_of(
                linear(
                    """[
        migrations.CreateModel(
            name='Order',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('extra', EXTRA_FIELD),
            ],
        ),
    ]"""
                )
            )
        )
        assert "id" in state.models[("shop", "order")].fields

    def test_a_field_list_entry_that_is_not_a_pair_makes_the_model_unknown(self, replay_of):
        state = final_state(
            replay_of(
                linear("[migrations.CreateModel(name='Order', fields=[('id',), *extra_fields])]")
            )
        )
        assert ("shop", "order") in state.unknown

    def test_a_model_with_readable_fields_is_not_unknown(self, replay_of):
        # The contrast: unknown must be entered by the computed list, not by
        # every CreateModel.
        state = final_state(replay_of(linear(CREATE_ORDER)))
        assert state.unknown == set()


class TestColumnOperations:
    def test_add_field_adds_the_column(self, replay_of):
        state = final_state(
            replay_of(
                linear(
                    CREATE_ORDER,
                    "[migrations.AddField('order', 'paid', models.BooleanField(default=False))]",
                )
            )
        )
        assert "paid" in state.models[("shop", "order")].fields

    def test_alter_field_replaces_the_column_with_what_it_declares(self, replay_of):
        state = final_state(
            replay_of(
                linear(
                    CREATE_ORDER,
                    "[migrations.AlterField('order', 'total', models.CharField(max_length=10))]",
                )
            )
        )
        assert state.models[("shop", "order")].fields["total"].kind == "CharField"

    def test_remove_field_drops_the_column(self, replay_of):
        state = final_state(
            replay_of(linear(CREATE_ORDER, "[migrations.RemoveField('order', 'total')]"))
        )
        assert "total" not in state.models[("shop", "order")].fields

    def test_rename_field_moves_the_column_and_keeps_its_declaration(self, replay_of):
        state = final_state(
            replay_of(linear(CREATE_ORDER, "[migrations.RenameField('order', 'total', 'amount')]"))
        )
        fields = state.models[("shop", "order")].fields
        assert "total" not in fields
        assert fields["amount"].kind == "IntegerField"

    def test_renaming_a_column_that_is_not_there_makes_the_model_unknown(self, replay_of):
        state = final_state(
            replay_of(linear(CREATE_ORDER, "[migrations.RenameField('order', 'nope', 'amount')]"))
        )
        assert ("shop", "order") in state.unknown

    def test_delete_model_drops_the_table(self, replay_of):
        state = final_state(replay_of(linear(CREATE_ORDER, "[migrations.DeleteModel('Order')]")))
        assert ("shop", "order") not in state.models

    def test_rename_model_carries_the_columns_across(self, replay_of):
        state = final_state(
            replay_of(linear(CREATE_ORDER, "[migrations.RenameModel('Order', 'Purchase')]"))
        )
        assert ("shop", "order") not in state.models
        assert set(state.models[("shop", "purchase")].fields) == {"id", "total"}

    def test_an_unreadable_field_makes_the_model_unknown(self, replay_of):
        # AddField whose field= is a name rather than a call: the column exists
        # but its declaration does not, so the column set is no longer complete.
        state = final_state(
            replay_of(linear(CREATE_ORDER, "[migrations.AddField('order', 'paid', SOME_FIELD)]"))
        )
        assert ("shop", "order") in state.unknown


class TestPriorState:
    def alter(self, replay_of):
        return replay_of(
            linear(
                CREATE_ORDER,
                "[migrations.AlterField('order', 'total', models.IntegerField(null=False))]",
            )
        )

    def test_an_operation_sees_the_column_as_it_was_before_it_ran(self, replay_of):
        applied = replay(self.alter(replay_of))
        altered = next(a for a in applied if a.operation.name == "AlterField")
        assert altered.existing_field is not None
        assert altered.existing_field.null is True

    def test_and_the_operation_still_carries_what_it_makes_it(self, replay_of):
        # The pair is the point: before.null is True and after.null is False is
        # what DJM-002 reads. Asserting only one of the two would pass against a
        # replay that handed back the operation's own field as the prior one.
        applied = replay(self.alter(replay_of))
        altered = next(a for a in applied if a.operation.name == "AlterField")
        assert altered.operation.field is not None
        assert altered.operation.field.null is False

    def test_a_create_model_has_no_prior_column(self, replay_of):
        applied = replay(replay_of(linear(CREATE_ORDER)))
        assert applied[0].operation.name == "CreateModel"
        assert applied[0].existing_field is None

    def test_add_field_has_no_prior_column_because_it_is_making_one(self, replay_of):
        applied = replay(
            replay_of(
                linear(
                    CREATE_ORDER, "[migrations.AddField('order', 'paid', models.BooleanField())]"
                )
            )
        )
        added = next(a for a in applied if a.operation.name == "AddField")
        assert added.existing_field is None

    def test_a_rename_finds_its_column_under_its_old_name(self, replay_of):
        # The defect the corpus found: a rename names the column it acts on in
        # old_name, so reading field_name left all 49 corpus renames with no
        # prior column -- the operation whose safety depends most on what is
        # being renamed.
        applied = replay(
            replay_of(linear(CREATE_ORDER, "[migrations.RenameField('order', 'total', 'amount')]"))
        )
        renamed = next(a for a in applied if a.operation.name == "RenameField")
        assert renamed.existing_field is not None
        assert renamed.existing_field.kind == "IntegerField"

    def test_a_remove_field_finds_the_column_it_is_dropping(self, replay_of):
        applied = replay(
            replay_of(linear(CREATE_ORDER, "[migrations.RemoveField('order', 'total')]"))
        )
        removed = next(a for a in applied if a.operation.name == "RemoveField")
        assert removed.existing_field is not None
        assert removed.existing_field.kind == "IntegerField"

    def test_a_column_read_off_an_untracked_model_is_withheld(self, replay_of):
        # The model is read well enough to hold `total`, and then something
        # happens that the replay cannot follow. The column is still sitting
        # there in the state, so a version that forgot to check `model_tracked`
        # would hand it back and be wrong only about whether it is current --
        # which is the whole question. Reaching this needs a model that is both
        # untracked and populated; an untracked *empty* model would answer None
        # either way and prove nothing.
        applied = replay(
            replay_of(
                linear(
                    CREATE_ORDER,
                    "[migrations.RenameField('order', 'nope', 'amount')]",
                    "[migrations.AlterField('order', 'total', models.CharField(max_length=2))]",
                )
            )
        )
        altered = next(a for a in applied if a.operation.name == "AlterField")
        assert altered.model_tracked is False
        assert altered.existing_field is None

    def test_the_column_it_withholds_is_one_the_state_is_holding(self, replay_of):
        # The other half of the pair above: without this, the assertion that
        # `existing_field is None` could be passing because the column was never
        # there at all.
        state = final_state(
            replay_of(
                linear(
                    CREATE_ORDER,
                    "[migrations.RenameField('order', 'nope', 'amount')]",
                )
            )
        )
        assert "total" in state.models[("shop", "order")].fields

    def test_a_tracked_model_says_so(self, replay_of):
        applied = replay(self.alter(replay_of))
        altered = next(a for a in applied if a.operation.name == "AlterField")
        assert altered.model_tracked is True


class TestTheTableThisTouches:
    def test_a_field_added_beside_its_own_create_model_is_against_an_empty_table(self, replay_of):
        applied = replay(replay_of(linear(CREATE_AND_ADD)))
        added = next(a for a in applied if a.operation.name == "AddField")
        assert added.creates_its_own_table is True

    def test_a_field_added_by_a_later_migration_is_against_a_populated_one(self, replay_of):
        applied = replay(
            replay_of(
                linear(
                    CREATE_ORDER, "[migrations.AddField('order', 'paid', models.BooleanField())]"
                )
            )
        )
        added = next(a for a in applied if a.operation.name == "AddField")
        assert added.creates_its_own_table is False
        assert added.created_by is not None
        assert added.created_by.key == ("shop", "0001_step")

    def test_a_model_nobody_created_claims_no_creating_migration(self, replay_of):
        applied = replay(
            replay_of(linear("[migrations.AddField('ghost', 'paid', models.BooleanField())]"))
        )
        assert applied[0].created_by is None
        assert applied[0].creates_its_own_table is False


class TestModelsNobodyCreated:
    def test_an_operation_against_an_unseen_model_makes_it_unknown(self, replay_of):
        state = final_state(
            replay_of(linear("[migrations.AddField('ghost', 'paid', models.BooleanField())]"))
        )
        assert ("shop", "ghost") in state.unknown

    def test_and_the_field_it_added_is_withheld_rather_than_reported(self, replay_of):
        # Recording the column would claim the table has exactly one column,
        # when all that is known is that it has at least one.
        state = final_state(
            replay_of(linear("[migrations.AddField('ghost', 'paid', models.BooleanField())]"))
        )
        assert state.field_of(("shop", "ghost"), "paid") is None

    def test_a_renamed_unknown_model_stays_unknown_under_its_new_name(self, replay_of):
        state = final_state(
            replay_of(
                linear(
                    "[migrations.AddField('ghost', 'paid', models.BooleanField())]",
                    "[migrations.RenameModel('Ghost', 'Spectre')]",
                )
            )
        )
        assert ("shop", "spectre") in state.unknown
        assert ("shop", "ghost") not in state.unknown

    def test_renaming_a_model_that_was_never_created_makes_it_unknown(self, replay_of):
        # Distinct from renaming a model that is tracked-but-unknown: here there
        # is no source state at all, so the new name starts life with no columns
        # and must not be mistaken for a table that genuinely has none.
        state = final_state(replay_of(linear("[migrations.RenameModel('Ghost', 'Spectre')]")))
        assert ("shop", "spectre") in state.unknown
        assert state.models[("shop", "spectre")].fields == {}

    def test_an_unrecognised_operation_makes_its_model_unknown(self, replay_of):
        # A third-party operation may do anything to the table; trusting the
        # column set afterwards would be trusting something nobody read.
        state = final_state(replay_of(linear(CREATE_ORDER, "[hierarkey.CleanDuplicates('order')]")))
        assert ("shop", "order") in state.unknown


class TestOperationsWithNoModel:
    def test_run_sql_does_not_name_a_model(self, replay_of):
        # The defect the corpus found: reading args[0] as a model turned 78
        # NetBox SQL statements into models, each then marked untrustworthy by a
        # replay that had nothing to distrust.
        graph = replay_of(linear(CREATE_ORDER, "[migrations.RunSQL('ALTER INDEX a RENAME TO b')]"))
        ran = next(a for a in replay(graph) if a.operation.name == "RunSQL")
        assert ran.operation.model_name is None
        assert ran.model_key is None

    def test_and_so_leaves_no_model_behind(self, replay_of):
        state = final_state(
            replay_of(linear(CREATE_ORDER, "[migrations.RunSQL('ALTER INDEX a RENAME TO b')]"))
        )
        assert set(state.models) == {("shop", "order")}
        assert state.unknown == set()

    def test_run_python_does_not_name_a_model(self, replay_of):
        graph = replay_of(linear(CREATE_ORDER, "[migrations.RunPython(forwards)]"))
        ran = next(a for a in replay(graph) if a.operation.name == "RunPython")
        assert ran.operation.model_name is None

    def test_an_extension_name_is_not_a_model(self, replay_of):
        state = final_state(replay_of(linear("[HStoreExtension()]", CREATE_ORDER)))
        assert set(state.models) == {("shop", "order")}

    def test_a_collation_name_is_not_a_model(self, replay_of):
        state = final_state(
            replay_of(linear("[CreateCollation('case_insensitive', 'und-u-ks-level2')]"))
        )
        assert state.models == {}

    def test_an_operation_that_does_name_a_model_still_finds_it_positionally(self, replay_of):
        # The contrast: the fix must exempt the operations that take no model,
        # not stop reading the first argument.
        state = final_state(
            replay_of(linear(CREATE_ORDER, "[migrations.RemoveField('order', 'total')]"))
        )
        assert "total" not in state.models[("shop", "order")].fields


class TestSeparateDatabaseAndState:
    SEPARATE = """[
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL('ALTER TABLE shop_order RENAME TO shop_purchase'),
            ],
            state_operations=[
                migrations.DeleteModel('Order'),
            ],
        ),
    ]"""

    def test_only_the_database_half_is_replayed(self, replay_of):
        # The state half is by construction a no-op against the database.
        # Applying it here would delete a table the migration takes care to keep.
        state = final_state(replay_of(linear(CREATE_ORDER, self.SEPARATE)))
        assert ("shop", "order") in state.models

    def test_the_database_half_appears_as_its_own_applied_operation(self, replay_of):
        applied = replay(replay_of(linear(CREATE_ORDER, self.SEPARATE)))
        assert [a.operation.name for a in applied if a.migration.number == 2] == ["RunSQL"]


class TestTheStateItself:
    def test_a_copy_shares_nothing_with_what_it_copied(self, replay_of):
        state = final_state(replay_of(linear(CREATE_ORDER)))
        clone = state.copy()
        clone.models[("shop", "order")].fields.pop("total")
        clone.unknown.add(("shop", "ghost"))
        assert "total" in state.models[("shop", "order")].fields
        assert state.unknown == set()

    def test_a_column_of_an_unknown_model_is_withheld_even_when_it_is_there(self, replay_of):
        # field_of answers "is it safe to speak", so a model that is tracked
        # badly answers the same as a model that has no such column.
        state = final_state(
            replay_of(
                linear(
                    CREATE_ORDER,
                    "[migrations.RenameField('order', 'nope', 'amount')]",
                )
            )
        )
        assert ("shop", "order") in state.unknown
        assert state.models[("shop", "order")].fields["total"] is not None
        assert state.field_of(("shop", "order"), "total") is None

    def test_replay_visits_migrations_in_dependency_order(self, replay_of):
        applied = replay(
            replay_of(
                linear(
                    CREATE_ORDER,
                    "[migrations.AddField('order', 'paid', models.BooleanField())]",
                    "[migrations.RemoveField('order', 'paid')]",
                )
            )
        )
        assert [a.operation.name for a in applied] == ["CreateModel", "AddField", "RemoveField"]

    def test_a_superseded_migration_is_not_replayed_twice(self, replay_of):
        # The squash and the migrations it replaces both create the model. If
        # both ran, the second CreateModel would silently reset the columns.
        graph = replay_of(
            {
                "shop": {
                    "0001_initial": migration(CREATE_ORDER),
                    "0002_paid": migration(
                        "[migrations.AddField('order', 'paid', models.BooleanField())]",
                        "[('shop', '0001_initial')]",
                    ),
                    "0001_squashed_0002_paid": migration(
                        CREATE_ORDER,
                        replaces="[('shop', '0001_initial'), ('shop', '0002_paid')]",
                    ),
                }
            }
        )
        applied = replay(graph)
        assert [a.operation.name for a in applied] == ["CreateModel"]
        assert "paid" not in final_state(graph).models[("shop", "order")].fields
