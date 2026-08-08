"""Reading migration files without importing them.

The corpus is the real test here -- 875 migrations across Healthchecks, NetBox
and pretix all parse -- but a corpus proves the parser survives what exists, not
that it says the right thing about what it read. These pin the meaning.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from djaudit.graph.builder import app_label_of_dir
from djaudit.migrations.nodes import Dependency, MigrationNode, OperationKind, classify
from djaudit.migrations.parse import (
    is_migration_file,
    migration_files,
    parse_migration,
)

MINIMAL = """
    from django.db import migrations, models


    class Migration(migrations.Migration):
        dependencies = [('shop', '0001_initial')]

        operations = [
            migrations.AddField(
                model_name='order',
                name='total',
                field=models.IntegerField(default=0),
            ),
        ]
"""


@pytest.fixture
def parse(make_project):
    """Parse one migration written at ``shop/migrations/<name>.py``."""

    def build(source: str, name: str = "0002_total", app: str = "shop") -> MigrationNode:
        ctx = make_project(
            {
                f"{app}/__init__.py": "",
                f"{app}/migrations/__init__.py": "",
                f"{app}/migrations/{name}.py": textwrap.dedent(source).lstrip(),
            }
        )
        path = ctx.root / app / "migrations" / f"{name}.py"
        node = parse_migration(path, app_label_of_dir(path.parent.parent, ctx), ctx)
        assert node is not None
        return node

    return build


class TestWhichFilesAreMigrations:
    def test_a_numbered_file_under_migrations_is_one(self):
        assert is_migration_file(Path("shop/migrations/0001_initial.py"))

    def test_the_package_marker_is_not(self):
        assert not is_migration_file(Path("shop/migrations/__init__.py"))

    def test_a_module_beside_migrations_is_not(self):
        assert not is_migration_file(Path("shop/models.py"))

    def test_a_module_named_like_one_elsewhere_is_not(self):
        assert not is_migration_file(Path("shop/utils/0001_initial.py"))

    def test_an_editor_backup_is_not(self):
        assert not is_migration_file(Path("shop/migrations/~0001_initial.py"))

    def test_a_non_python_file_is_not(self):
        assert not is_migration_file(Path("shop/migrations/0001_initial.sql"))

    def test_the_list_is_sorted_so_two_runs_agree(self, make_project):
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/migrations/__init__.py": "",
                "shop/migrations/0002_b.py": "x = 1",
                "shop/migrations/0001_a.py": "x = 1",
                "shop/migrations/0003_c.py": "x = 1",
            }
        )
        names = [path.stem for path in migration_files(ctx)]
        assert names == ["0001_a", "0002_b", "0003_c"]


class TestFindingTheMigrationClass:
    def test_the_conventional_class_is_found(self, parse):
        node = parse(MINIMAL)
        assert node.name == "0002_total"
        assert node.app == "shop"

    def test_a_project_base_class_is_found_by_name(self, parse):
        # pretix and several large projects subclass their own base, so the
        # resolved base is the project's class and not Django's. Django itself
        # only ever loads the attribute called ``Migration``, so the name is
        # the fallback -- and it has to be, or these files read as empty.
        node = parse(
            """
            from django.db import migrations, models


            class ProjectMigration(migrations.Migration):
                pass


            class Migration(ProjectMigration):
                dependencies = [('shop', '0001_initial')]
                operations = [migrations.AddField(
                    model_name='order', name='total',
                    field=models.IntegerField(default=0))]
            """
        )
        assert len(node.operations) == 1
        assert node.operations[0].field_name == "total"

    def test_the_django_base_wins_over_a_class_merely_named_migration(self, parse):
        # Squashed migrations in some projects keep a helper class around. The
        # base test runs first so the real one is found regardless of order.
        node = parse(
            """
            from django.db import migrations, models


            class Migration(migrations.Migration):
                dependencies = [('shop', '0001_initial')]
                operations = [migrations.AddField(
                    model_name='order', name='total',
                    field=models.IntegerField(default=0))]
            """
        )
        assert len(node.operations) == 1

    def test_an_aliased_import_still_resolves(self, parse):
        node = parse(
            """
            from django.db.migrations import Migration as Base
            from django.db import models


            class Migration(Base):
                dependencies = [('shop', '0001_initial')]
                operations = []
            """
        )
        assert node.dependencies == (Dependency(app="shop", name="0001_initial"),)

    def test_a_file_with_no_migration_class_parses_to_nothing(self, make_project):
        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/migrations/__init__.py": "",
                "shop/migrations/0002_helper.py": "HELPERS = ['a']\n",
            }
        )
        path = ctx.root / "shop" / "migrations" / "0002_helper.py"
        assert parse_migration(path, "shop", ctx) is None


class TestDependencies:
    def test_a_pair_of_strings_becomes_one_dependency(self, parse):
        node = parse(MINIMAL)
        assert node.dependencies == (Dependency(app="shop", name="0001_initial"),)

    def test_a_swappable_dependency_names_no_app(self, parse):
        # ``AUTH_USER_MODEL`` is a setting, so the app is not in this file.
        # Guessing ``auth`` would be right for most projects and wrong for
        # exactly the projects that bothered to swap it.
        node = parse(
            """
            from django.conf import settings
            from django.db import migrations


            class Migration(migrations.Migration):
                dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
                operations = []
            """
        )
        assert node.dependencies[0].swappable
        assert node.dependencies[0].app == ""
        assert not node.dependencies[0].unreadable

    def test_a_computed_entry_is_marked_unreadable_not_guessed(self, parse):
        node = parse(
            """
            from django.db import migrations

            TARGET = ('shop', '0001_initial')


            class Migration(migrations.Migration):
                dependencies = [TARGET]
                operations = []
            """
        )
        assert node.dependencies[0].unreadable
        assert not node.dependencies[0].swappable

    def test_a_computed_dependency_list_is_recorded_as_unreadable(self, parse):
        node = parse(
            """
            from django.db import migrations

            DEPS = [('shop', '0001_initial')]


            class Migration(migrations.Migration):
                dependencies = DEPS
                operations = []
            """
        )
        assert "dependencies" in node.unreadable
        assert node.dependencies == ()

    def test_run_before_and_replaces_are_read_the_same_way(self, parse):
        node = parse(
            """
            from django.db import migrations


            class Migration(migrations.Migration):
                dependencies = [('shop', '0001_initial')]
                run_before = [('billing', '0004_invoice')]
                replaces = [('shop', '0002_a'), ('shop', '0003_b')]
                operations = []
            """
        )
        assert node.run_before == (Dependency(app="billing", name="0004_invoice"),)
        assert [dep.name for dep in node.replaces] == ["0002_a", "0003_b"]
        assert node.is_squash

    def test_a_migration_without_replaces_is_not_a_squash(self, parse):
        assert not parse(MINIMAL).is_squash


class TestAtomicAndInitial:
    def test_atomic_defaults_to_true_as_django_does(self, parse):
        # Django's default, not ours. A rule that assumes the opposite would
        # report every migration in the corpus as running outside a transaction.
        assert parse(MINIMAL).atomic

    def test_atomic_false_is_read(self, parse):
        node = parse(
            """
            from django.db import migrations


            class Migration(migrations.Migration):
                atomic = False
                dependencies = []
                operations = []
            """
        )
        assert not node.atomic

    def test_a_computed_atomic_keeps_djangos_default_and_says_so(self, parse):
        node = parse(
            """
            from django.conf import settings
            from django.db import migrations


            class Migration(migrations.Migration):
                atomic = settings.MIGRATION_ATOMIC
                dependencies = []
                operations = []
            """
        )
        assert node.atomic is True
        assert "atomic" in node.unreadable

    def test_initial_is_none_when_absent_rather_than_false(self, parse):
        # ``initial = False`` is a statement; an absent ``initial`` is silence.
        assert parse(MINIMAL).initial is None

    def test_initial_true_is_read(self, parse):
        node = parse(
            """
            from django.db import migrations


            class Migration(migrations.Migration):
                initial = True
                dependencies = []
                operations = []
            """
        )
        assert node.initial is True


class TestOperations:
    def test_an_add_field_carries_model_field_and_declaration(self, parse):
        operation = parse(MINIMAL).operations[0]
        assert operation.name == "AddField"
        assert operation.kind is OperationKind.SCHEMA
        assert operation.model_name == "order"
        assert operation.field_name == "total"
        assert operation.field is not None
        assert operation.field.kind == "IntegerField"
        assert operation.field.has_default

    def test_the_field_is_read_by_the_model_graphs_own_reader(self, parse):
        # Not a migration-shaped copy of it: ``null`` has to mean here what it
        # means on a model, including Django's defaults for absent keywords.
        node = parse(
            """
            from django.db import migrations, models


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.AddField(
                    model_name='order', name='ref',
                    field=models.OneToOneField(to='shop.Cart', on_delete=models.CASCADE))]
            """
        )
        field = node.operations[0].field
        assert field is not None
        assert field.unique, "OneToOneField implies unique, as Django does"
        assert field.db_index
        assert not field.null

    def test_a_third_party_field_is_read_despite_its_name(self, parse):
        # ``TreeForeignKey`` does not end in ``Field``. Under ``field=`` there
        # is nothing else it could be, and dropping it loses 55 of NetBox's
        # 681 AddField operations.
        node = parse(
            """
            import mptt.fields
            from django.db import migrations


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.AddField(
                    model_name='site', name='parent',
                    field=mptt.fields.TreeForeignKey(null=True, to='dcim.Region'))]
            """
        )
        field = node.operations[0].field
        assert field is not None
        assert field.kind == "TreeForeignKey"
        assert field.null

    def test_a_manager_in_a_class_body_is_still_not_a_field(self, make_project):
        # The contrast for the test above: positional trust is granted only
        # under ``field=``. A model body still needs the naming convention, or
        # every manager in the project becomes a column.
        from djaudit.graph.builder import build_model_graph

        ctx = make_project(
            {
                "shop/__init__.py": "",
                "shop/models.py": textwrap.dedent(
                    """
                    from django.db import models
                    import taggit.managers


                    class Order(models.Model):
                        objects = models.Manager()
                        tags = taggit.managers.TaggableManager()
                        total = models.IntegerField()
                    """
                ).lstrip(),
            }
        )
        model = build_model_graph(ctx).models["shop.Order"]
        assert set(model.fields) == {"total"}

    def test_operation_kinds_cover_djangos_own(self, parse):
        node = parse(
            """
            from django.db import migrations, models


            class Migration(migrations.Migration):
                dependencies = []
                operations = [
                    migrations.CreateModel(name='Cart', fields=[]),
                    migrations.AddIndex(model_name='order', index=models.Index(fields=['id'])),
                    migrations.AddConstraint(model_name='order', constraint=None),
                    migrations.AlterModelOptions(name='order', options={}),
                    migrations.RunSQL('SELECT 1'),
                ]
            """
        )
        assert [op.kind for op in node.operations] == [
            OperationKind.SCHEMA,
            OperationKind.INDEX,
            OperationKind.CONSTRAINT,
            OperationKind.STATE,
            OperationKind.RUN_SQL,
        ]

    def test_an_unrecognised_operation_is_unknown_rather_than_harmless(self):
        # pretix ships ``CleanHierarkeyDuplicates``. Classifying an unknown
        # operation as state-only would let a lock rule conclude a migration
        # emits no DDL when nobody looked.
        assert classify("CleanHierarkeyDuplicates") is OperationKind.UNKNOWN
        assert classify("AddField") is OperationKind.SCHEMA

    def test_a_concurrent_index_build_is_marked(self, parse):
        node = parse(
            """
            from django.contrib.postgres.operations import AddIndexConcurrently
            from django.db import migrations, models


            class Migration(migrations.Migration):
                atomic = False
                dependencies = []
                operations = [AddIndexConcurrently(
                    model_name='order', index=models.Index(fields=['id'], name='i'))]
            """
        )
        assert node.operations[0].concurrent
        assert not parse(MINIMAL).operations[0].concurrent

    def test_a_computed_operations_list_is_recorded(self, parse):
        node = parse(
            """
            from django.db import migrations

            OPS = []


            class Migration(migrations.Migration):
                dependencies = []
                operations = OPS
            """
        )
        assert "operations" in node.unreadable


class TestRunPython:
    def test_a_reverse_given_by_keyword_is_seen(self, parse):
        node = parse(
            """
            from django.db import migrations


            def forwards(apps, schema_editor):
                pass


            def backwards(apps, schema_editor):
                pass


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.RunPython(forwards, reverse_code=backwards)]
            """
        )
        operation = node.operations[0]
        assert operation.kind is OperationKind.RUN_PYTHON
        assert operation.reverse is True
        assert operation.callable_name == "forwards"
        assert operation.touches_data

    def test_a_reverse_given_positionally_is_seen(self, parse):
        # ``RunPython(forwards, backwards)`` is the commoner spelling in the
        # corpus. Checking only keywords would report every one as unreversible.
        node = parse(
            """
            from django.db import migrations


            def forwards(apps, schema_editor):
                pass


            def backwards(apps, schema_editor):
                pass


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.RunPython(forwards, backwards)]
            """
        )
        assert node.operations[0].reverse is True

    def test_noop_counts_as_a_reverse(self, parse):
        # ``RunPython.noop`` is somebody deciding that undoing this needs no
        # work. An absent argument is nobody deciding anything.
        node = parse(
            """
            from django.db import migrations


            def forwards(apps, schema_editor):
                pass


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
            """
        )
        assert node.operations[0].reverse is True

    def test_a_missing_reverse_is_false(self, parse):
        node = parse(
            """
            from django.db import migrations


            def forwards(apps, schema_editor):
                pass


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.RunPython(forwards)]
            """
        )
        assert node.operations[0].reverse is False


class TestRunSQL:
    def test_forward_sql_is_captured_as_text(self, parse):
        node = parse(
            """
            from django.db import migrations


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.RunSQL("CREATE INDEX i ON shop_order (id)")]
            """
        )
        operation = node.operations[0]
        assert operation.sql == "CREATE INDEX i ON shop_order (id)"
        assert operation.reverse is False
        assert operation.touches_data

    def test_a_list_of_statements_is_joined(self, parse):
        node = parse(
            """
            from django.db import migrations


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.RunSQL(
                    ["CREATE INDEX a ON t (x)", "CREATE INDEX b ON t (y)"],
                    reverse_sql=["DROP INDEX a", "DROP INDEX b"],
                )]
            """
        )
        operation = node.operations[0]
        assert operation.sql == "CREATE INDEX a ON t (x)\nCREATE INDEX b ON t (y)"
        assert operation.reverse is True

    def test_sql_built_at_import_time_is_left_unread(self, parse):
        node = parse(
            """
            from django.db import migrations

            STATEMENT = "CREATE INDEX i ON shop_order (id)"


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.RunSQL(STATEMENT)]
            """
        )
        assert node.operations[0].sql is None


class TestSeparateDatabaseAndState:
    def test_only_the_database_half_is_kept(self, parse):
        # The state half emits no DDL by construction. Including it would have
        # every lock rule report operations that never reach the database.
        node = parse(
            """
            from django.db import migrations, models


            class Migration(migrations.Migration):
                dependencies = []
                operations = [migrations.SeparateDatabaseAndState(
                    database_operations=[migrations.RunSQL("ALTER TABLE t RENAME TO u")],
                    state_operations=[migrations.RenameModel(old_name='T', new_name='U')],
                )]
            """
        )
        operation = node.operations[0]
        assert operation.kind is OperationKind.SEPARATE
        assert [inner.name for inner in operation.inner] == ["RunSQL"]


class TestMigrationIdentity:
    def test_the_key_is_app_and_name(self, parse):
        assert parse(MINIMAL).key == ("shop", "0002_total")

    def test_the_leading_number_is_read(self, parse):
        assert parse(MINIMAL).number == 2

    def test_an_unnumbered_migration_has_no_number(self, parse):
        assert parse(MINIMAL, name="add_total").number is None

    def test_the_app_label_follows_appconfig_not_the_directory(self, make_project):
        # ``dependencies`` entries name the label. A project that overrode it
        # would otherwise get a graph whose edges point at an app nothing has
        # heard of.
        ctx = make_project(
            {
                "vendor_shop/__init__.py": "",
                "vendor_shop/apps.py": textwrap.dedent(
                    """
                    from django.apps import AppConfig


                    class ShopConfig(AppConfig):
                        name = 'vendor_shop'
                        label = 'shop'
                    """
                ).lstrip(),
                "vendor_shop/migrations/__init__.py": "",
                "vendor_shop/migrations/0001_initial.py": textwrap.dedent(MINIMAL).lstrip(),
            }
        )
        path = ctx.root / "vendor_shop" / "migrations" / "0001_initial.py"
        assert app_label_of_dir(path.parent.parent, ctx) == "shop"
