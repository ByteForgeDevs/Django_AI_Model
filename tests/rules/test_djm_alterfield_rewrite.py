"""Tests for `DJM-002`, and for the widening cases it must stay quiet about."""

from __future__ import annotations

import pytest

from djaudit.graph.nodes import FieldNode
from djaudit.rules.djm_alterfield_rewrite import TableRewritingAlterField, no_rewrite
from tests.rules.test_djm_addfield_not_null import Build, findings_for, migration, project

CREATE = """\
migrations.CreateModel(
    name='Order',
    fields=[
        ('id', models.AutoField(primary_key=True, serialize=False)),
        ('code', models.CharField(max_length=64)),
        ('note', models.TextField(null=True)),
        ('total', models.IntegerField()),
    ],
),
"""


def alter(field: str, name: str = "code") -> str:
    return f"migrations.AlterField(model_name='order', name='{name}', field={field}),\n"


def run(build: Build, operation: str) -> list[str]:
    ctx = project(
        build,
        {
            "shop/migrations/0001_initial.py": migration(CREATE),
            "shop/migrations/0002_change.py": migration(operation, deps="('shop', '0001_initial')"),
        },
    )
    return findings_for(TableRewritingAlterField(), ctx)


def field(kind: str, max_length: int | None = None, null: bool = False) -> FieldNode:
    return FieldNode(
        name="code",
        kind=kind,
        dotted=f"django.db.models.{kind}",
        lineno=1,
        end_lineno=1,
        max_length=max_length,
        null=null,
    )


class TestWidens:
    @pytest.mark.parametrize(
        ("before", "after", "expected"),
        [
            (field("CharField", 64), field("CharField", 128), True),
            (field("CharField", 64), field("CharField", 64), True),
            (field("CharField", 64), field("CharField", 32), False),
            (field("CharField", 64), field("TextField"), True),
            (field("TextField"), field("CharField", 64), False),
            (field("IntegerField"), field("BigIntegerField"), False),
            (field("CharField", 64), field("IntegerField"), False),
            (field("ForeignKey"), field("OneToOneField"), True),
            (field("OneToOneField"), field("ForeignKey"), True),
            (field("ForeignKey"), field("IntegerField"), False),
        ],
    )
    def test_only_text_that_does_not_shrink_is_free(
        self, before: FieldNode, after: FieldNode, expected: bool
    ) -> None:
        assert no_rewrite(before, after) is expected

    def test_integer_widening_is_not_free(self) -> None:
        """`integer` to `bigint` really does change the bytes on disk.

        pretix's `*_bigint.py` migrations are exactly this, and getting the
        direction backwards would silence the family's best real finding.
        """
        assert no_rewrite(field("AutoField"), field("BigAutoField")) is False


class TestTheColumnIsRewritten:
    def test_reports_a_changed_type(self, make_project: Build) -> None:
        assert len(run(make_project, alter("models.IntegerField()"))) == 1

    def test_reports_a_narrowed_max_length(self, make_project: Build) -> None:
        found = run(make_project, alter("models.CharField(max_length=32)"))
        assert len(found) == 1
        assert "rewrites every row" in found[0]

    def test_accepts_a_widened_max_length(self, make_project: Build) -> None:
        assert run(make_project, alter("models.CharField(max_length=128)")) == []

    def test_accepts_an_unchanged_field(self, make_project: Build) -> None:
        assert run(make_project, alter("models.CharField(max_length=64)")) == []

    def test_accepts_char_to_text(self, make_project: Build) -> None:
        assert run(make_project, alter("models.TextField()")) == []

    def test_reports_auto_field_widening_to_big_auto(self, make_project: Build) -> None:
        found = run(make_project, alter("models.BigIntegerField()", name="total"))
        assert len(found) == 1


class TestTheColumnBecomesNotNull:
    def test_reports_a_tightened_column(self, make_project: Build) -> None:
        found = run(make_project, alter("models.TextField()", name="note"))
        assert len(found) == 1
        assert "scans every row" in found[0]

    def test_accepts_a_loosened_column(self, make_project: Build) -> None:
        assert run(make_project, alter("models.IntegerField(null=True)", name="total")) == []

    def test_reports_both_reasons_together(self, make_project: Build) -> None:
        """A type change and a tightening in one operation is one finding."""
        found = run(make_project, alter("models.IntegerField()", name="note"))
        assert len(found) == 1
        assert "rewrites every row" in found[0] and "scans every row" in found[0]


class TestWhatItDeclinesToSay:
    def test_declines_when_the_prior_column_is_unknown(self, make_project: Build) -> None:
        """No `CreateModel` anywhere, so there is nothing to compare against."""
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(
                    alter("models.IntegerField()", name="ghost")
                )
            },
        )
        assert findings_for(TableRewritingAlterField(), ctx) == []

    def test_declines_when_nullability_was_not_readable(self, make_project: Build) -> None:
        assert run(make_project, alter("models.TextField(null=SETTING)", name="note")) == []

    def test_ignores_add_field(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_change.py": migration(
                    "migrations.AddField(model_name='order', name='extra',"
                    " field=models.IntegerField(default=0)),\n",
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        assert findings_for(TableRewritingAlterField(), ctx) == []

    def test_ignores_add_field_of_a_column_that_already_exists(self, make_project: Build) -> None:
        """The only shape where another operation has both a field and a prior one.

        A plain `AddField` is excluded by having no prior column at all, so it
        cannot prove the operation-name guard does anything. Re-adding a column
        the state already holds -- what a badly merged migration produces --
        gives `AddField` both, and only the name distinguishes it.
        """
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_change.py": migration(
                    "migrations.AddField(model_name='order', name='code',"
                    " field=models.IntegerField()),\n",
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        assert findings_for(TableRewritingAlterField(), ctx) == []

    def test_accepts_an_alter_on_a_table_created_in_the_same_pending_set(
        self, make_project: Build
    ) -> None:
        ctx = project(
            make_project,
            {"shop/migrations/0001_initial.py": migration(CREATE + alter("models.IntegerField()"))},
        )
        assert findings_for(TableRewritingAlterField(), ctx) == []


class TestTheEvidence:
    def test_quotes_the_prior_column(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_change.py": migration(
                    alter("models.IntegerField()"), deps="('shop', '0001_initial')"
                ),
            },
        )
        found = list(TableRewritingAlterField().check(ctx))
        assert any(
            "CharField" in e.content and "null=False" in e.content for e in found[0].evidence
        )
