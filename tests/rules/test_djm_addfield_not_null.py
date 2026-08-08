"""Tests for `DJM-001` and the scope decision the whole family rests on."""

from __future__ import annotations

import textwrap
from collections.abc import Callable

from djaudit.context import ProjectContext
from djaudit.rules._migrations import MigrationRule
from djaudit.rules.djm_addfield_not_null import NonNullableAddFieldWithoutDefault

Build = Callable[[dict[str, str]], ProjectContext]

SETTINGS = "INSTALLED_APPS = ['shop']\n"


def migration(operations: str, deps: str = "") -> str:
    """One migration module wrapping ``operations``.

    The template is dedented *before* the operations are substituted, because
    `textwrap.dedent` computes the common prefix over the interpolated result:
    a multi-line value indented less than the template makes dedent strip by
    that smaller amount and leave `class Migration` indented, which is a syntax
    error and parses to no migration at all.
    """
    template = textwrap.dedent(
        """
        from django.db import migrations, models
        import django.db.models.deletion


        class Migration(migrations.Migration):
            dependencies = [{deps}]
            operations = [
        {operations}
            ]
        """
    )
    return template.format(deps=deps, operations=textwrap.indent(operations, " " * 8))


def project(build: Build, files: dict[str, str]) -> ProjectContext:
    base = {
        "manage.py": "import os\n",
        "settings.py": SETTINGS,
        "shop/__init__.py": "",
        "shop/models.py": "from django.db import models\n",
        "shop/migrations/__init__.py": "",
    }
    return build({**base, **files})


def findings_for(rule: MigrationRule, ctx: ProjectContext) -> list[str]:
    """Messages from one rule, run the way the engine runs it.

    Shared with the other `DJM` test modules so they all exercise `check`,
    which is what resolves the family's scope. A test calling `inspect`
    directly would leave `scope` empty and quietly measure a different rule.
    """
    return [f.message for f in rule.check(ctx)]


def findings(ctx: ProjectContext) -> list[str]:
    return findings_for(NonNullableAddFieldWithoutDefault(), ctx)


CREATE = """\
migrations.CreateModel(
    name='Order',
    fields=[('id', models.AutoField(primary_key=True, serialize=False))],
),
"""


def add(field: str, name: str = "code") -> str:
    return f"migrations.AddField(model_name='order', name='{name}', field={field}),\n"


class TestTheColumnCannotBeFilled:
    def test_reports_non_nullable_integer_with_no_default(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField()"), deps="('shop', '0001_initial')"
                ),
            },
        )
        assert len(findings(ctx)) == 1
        assert "order.code" in findings(ctx)[0]

    def test_accepts_a_declared_default(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField(default=0)"), deps="('shop', '0001_initial')"
                ),
            },
        )
        assert findings(ctx) == []

    def test_accepts_a_nullable_column(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField(null=True)"), deps="('shop', '0001_initial')"
                ),
            },
        )
        assert findings(ctx) == []

    def test_accepts_a_char_field_django_fills_itself(self, make_project: Build) -> None:
        """`empty_strings_allowed` makes `get_default()` return `''`, not None."""
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.CharField(max_length=10)"), deps="('shop', '0001_initial')"
                ),
            },
        )
        assert findings(ctx) == []

    def test_accepts_a_many_to_many_which_has_no_column(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.ManyToManyField(to='shop.order')", name="related"),
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        assert findings(ctx) == []

    def test_reports_a_non_nullable_foreign_key(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add(
                        "models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,"
                        " to='shop.order')",
                        name="parent",
                    ),
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        assert len(findings(ctx)) == 1

    def test_ignores_alter_field_which_also_carries_a_field(self, make_project: Build) -> None:
        """`AlterField` of an existing column is `DJM-002`'s subject, not this one.

        It is the only other operation carrying a declared field, so without
        this the rule would report every tightened column twice over.
        """
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    "migrations.AlterField(model_name='order', name='id',"
                    " field=models.IntegerField()),\n",
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        assert findings(ctx) == []


class TestTheTableHasNoRowsYet:
    def test_accepts_a_column_added_by_the_migration_that_creates_the_table(
        self, make_project: Build
    ) -> None:
        ctx = project(
            make_project,
            {"shop/migrations/0001_initial.py": migration(CREATE + add("models.IntegerField()"))},
        )
        assert findings(ctx) == []

    def test_accepts_a_column_whose_table_is_created_by_a_pending_migration(
        self, make_project: Build
    ) -> None:
        """The squashed-initial case: both run in one deploy, so the table is empty.

        This is what keeps NetBox quiet -- `circuits.0002_squashed_0029` adds 36
        non-nullable foreign keys to tables `0001_squashed` creates.
        """
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField()"), deps="('shop', '0001_initial')"
                ),
            },
        )
        rule = NonNullableAddFieldWithoutDefault()
        # Both migrations pending, as on a fresh install.
        rule.scope = frozenset({("shop", "0001_initial"), ("shop", "0002_code")})
        assert [f for a in ctx.migration_history for f in rule.inspect(ctx, a)] == []

    def test_reports_when_the_creating_migration_has_already_run(self, make_project: Build) -> None:
        """The contrast to the case above: same tree, only the scope differs."""
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField()"), deps="('shop', '0001_initial')"
                ),
            },
        )
        rule = NonNullableAddFieldWithoutDefault()
        rule.scope = frozenset({("shop", "0002_code")})
        assert len([f for a in ctx.migration_history for f in rule.inspect(ctx, a)]) == 1

    def test_reports_a_column_on_a_table_nobody_here_created(self, make_project: Build) -> None:
        """A model created outside the parsed history belongs to an older app.

        Its table predates this change, so it holds rows -- the dangerous case,
        not the safe one.
        """
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(
                    "migrations.AddField(model_name='session', name='score',"
                    " field=models.IntegerField()),\n"
                ),
            },
        )
        assert len(findings(ctx)) == 1


class TestOnlyPendingMigrationsAreReported:
    def test_ignores_a_defect_buried_in_history(self, make_project: Build) -> None:
        """A migration that already ran cannot be fixed, and demonstrably worked."""
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField()"), deps="('shop', '0001_initial')"
                ),
                "shop/migrations/0003_later.py": migration(
                    "migrations.AlterModelOptions(name='order', options={}),\n",
                    deps="('shop', '0002_code')",
                ),
            },
        )
        assert findings(ctx) == []

    def test_the_leaf_is_what_is_in_scope(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField()"), deps="('shop', '0001_initial')"
                ),
            },
        )
        assert NonNullableAddFieldWithoutDefault().in_scope(ctx) == frozenset(
            {("shop", "0002_code")}
        )


class TestWhatCannotBeRead:
    def test_declines_when_nullability_was_not_readable(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField(null=SOME_SETTING)"),
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        assert findings(ctx) == []

    def test_reports_a_callable_default_as_satisfied(self, make_project: Build) -> None:
        """`default=uuid4` is unreadable as a value but is still a default."""
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField(default=make_code)"),
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        assert findings(ctx) == []


class TestTheFindingItself:
    def test_points_at_the_operation_not_the_file(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    "migrations.AlterModelOptions(name='order', options={}),\n"
                    + add("models.IntegerField()"),
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        found = list(NonNullableAddFieldWithoutDefault().check(ctx))
        assert len(found) == 1
        source = (ctx.root / "shop/migrations/0002_code.py").read_text().splitlines()
        assert "AddField" in source[found[0].location.line - 1]

    def test_carries_the_operation_as_evidence(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_code.py": migration(
                    add("models.IntegerField()"), deps="('shop', '0001_initial')"
                ),
            },
        )
        found = list(NonNullableAddFieldWithoutDefault().check(ctx))
        assert any("AddField" in e.content for e in found[0].evidence)
