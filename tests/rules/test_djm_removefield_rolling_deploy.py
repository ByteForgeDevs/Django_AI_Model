"""Tests for `DJM-004`, and for the distinction the whole rule turns on.

The case worth reading first is
`TestTheDeliberateTwoStepIsNotReported`. The replay flattens
`SeparateDatabaseAndState` to its database half, so the second release of the
*correct* pattern reaches the rule as a bare `RemoveField` -- identical to the
reckless one. Without those tests the rule would report the fix as the defect.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Finding, Severity
from djaudit.rules.djm_removefield_rolling_deploy import RemoveFieldBreaksRollingDeploy
from tests.rules.test_djm_addfield_not_null import Build, findings_for, migration, project

CREATE = """\
migrations.CreateModel(
    name='Order',
    fields=[
        ('id', models.AutoField(primary_key=True, serialize=False)),
        ('code', models.CharField(max_length=10)),
        ('note', models.CharField(max_length=10)),
        ('tags', models.ManyToManyField(to='shop.Tag')),
    ],
),
"""


def findings(ctx: ProjectContext) -> list[str]:
    return findings_for(RemoveFieldBreaksRollingDeploy(), ctx)


def results(ctx: ProjectContext) -> list[Finding]:
    return list(RemoveFieldBreaksRollingDeploy().check(ctx))


def history(second: str, atomic: bool | None = None) -> dict[str, str]:
    """An app whose leaf migration is ``second``, on a table created earlier."""
    return {
        "shop/migrations/0001_initial.py": migration(CREATE),
        "shop/migrations/0002_change.py": migration(
            second, deps="('shop', '0001_initial')", atomic=atomic
        ),
    }


REMOVE = "migrations.RemoveField(model_name='order', name='code'),\n"


class TestTheColumnIsStillSelected:
    def test_reports_a_plain_remove_field(self, make_project: Build) -> None:
        ctx = project(make_project, history(REMOVE))

        assert findings(ctx) == [
            "`shop.0002_change` removes `order.code`. Django names every concrete "
            "column in its `SELECT`, so every query the release still running "
            "makes against `order` fails for the length of the deploy, not only "
            "those reading `code`."
        ]

    def test_the_finding_is_high(self, make_project: Build) -> None:
        ctx = project(make_project, history(REMOVE))

        assert [f.severity for f in results(ctx)] == [Severity.HIGH]

    def test_reports_each_removed_column_separately(self, make_project: Build) -> None:
        both = REMOVE + "migrations.RemoveField(model_name='order', name='note'),\n"
        ctx = project(make_project, history(both))

        assert len(findings(ctx)) == 2

    def test_an_atomic_migration_is_not_the_question(self, make_project: Build) -> None:
        """Unlike `DJM-003`, atomicity has no bearing here: the column is gone
        the moment the transaction commits either way."""
        ctx = project(make_project, history(REMOVE, atomic=False))

        assert len(findings(ctx)) == 1


class TestTheDeliberateTwoStepIsNotReported:
    """The pattern the remediation recommends must not be the pattern it flags."""

    def test_the_state_half_is_invisible(self, make_project: Build) -> None:
        """Release one: the model stops declaring the field, the column stays.

        This half never reaches a rule at all -- the replay drops it, because
        it does nothing to the database -- so the assertion is that flattening
        does not somehow surface it.
        """
        ctx = project(
            make_project,
            history(
                "migrations.SeparateDatabaseAndState(\n"
                "    state_operations=[migrations.RemoveField("
                "model_name='order', name='code')],\n"
                "),\n"
            ),
        )

        assert findings(ctx) == []

    def test_the_database_half_is_not_reported(self, make_project: Build) -> None:
        """Release two: the drop itself, once nothing selects the column.

        This is the case that arrives here indistinguishable from the reckless
        version, and the only thing telling them apart is `via_separate`.
        """
        ctx = project(
            make_project,
            history(
                "migrations.SeparateDatabaseAndState(\n"
                "    database_operations=[migrations.RemoveField("
                "model_name='order', name='code')],\n"
                "),\n"
            ),
        )

        assert findings(ctx) == []

    def test_the_contrast_is_real(self, make_project: Build) -> None:
        """The same operation on the same model, wrapped and bare, side by side.

        Both shapes go in one migration rather than one per project: the
        `make_project` fixture writes every project into the same directory and
        `migration_history` is lazy, so building two contexts in one test would
        leave both reading whichever files were written last.

        Without this contrast the two tests above would pass just as well
        against a rule that reported nothing at all.
        """
        ctx = project(
            make_project,
            history(
                "migrations.SeparateDatabaseAndState(\n"
                "    database_operations=[migrations.RemoveField("
                "model_name='order', name='code')],\n"
                "),\n"
                "migrations.RemoveField(model_name='order', name='note'),\n"
            ),
        )

        found = findings(ctx)

        assert len(found) == 1
        assert "`order.note`" in found[0]
        assert "code" not in found[0]


class TestJoinTablesAreALesserCase:
    def test_a_many_to_many_says_what_actually_breaks(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history("migrations.RemoveField(model_name='order', name='tags'),\n"),
        )

        assert findings(ctx) == [
            "`shop.0002_change` removes `order.tags`, dropping its join table. "
            "Code in the release still running during the deploy fails as soon "
            "as it traverses the relation."
        ]

    def test_a_many_to_many_is_a_rank_lower(self, make_project: Build) -> None:
        """It is not in `concrete_fields`, so ordinary queries keep working."""
        ctx = project(
            make_project,
            history("migrations.RemoveField(model_name='order', name='tags'),\n"),
        )

        assert [f.severity for f in results(ctx)] == [Severity.MEDIUM]

    def test_an_unknown_field_is_treated_as_a_column(self, make_project: Build) -> None:
        """179 of the corpora's 185 removals are ordinary columns, so the
        guess that costs least when wrong is the ordinary one."""
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_change.py": migration(
                    "migrations.RemoveField(model_name='order', name='never_declared'),\n",
                    deps="('shop', '0001_initial')",
                ),
            },
        )

        assert [f.severity for f in results(ctx)] == [Severity.HIGH]


class TestWhatIsOutOfScope:
    def test_a_table_this_deploy_creates_is_not_reported(self, make_project: Build) -> None:
        """No released code has ever queried a model that does not exist yet."""
        ctx = project(
            make_project,
            {"shop/migrations/0001_initial.py": migration(CREATE + REMOVE)},
        )

        assert findings(ctx) == []

    def test_a_field_of_a_model_being_deleted_is_not_reported(self, make_project: Build) -> None:
        """`makemigrations` emits these ahead of `DeleteModel` to break FK
        cycles, so one deletion would otherwise produce a burst of findings
        that all describe it and none of which name it."""
        ctx = project(
            make_project,
            history(REMOVE + "migrations.DeleteModel(name='order'),\n"),
        )

        assert findings(ctx) == []

    def test_a_deletion_of_another_model_does_not_silence_it(self, make_project: Build) -> None:
        """The contrast: a guard keyed on the migration rather than the model
        would swallow this one too."""
        ctx = project(
            make_project,
            history(REMOVE + "migrations.DeleteModel(name='invoice'),\n"),
        )

        assert len(findings(ctx)) == 1

    def test_a_migration_that_is_not_a_leaf_is_not_reported(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_change.py": migration(
                    REMOVE, deps="('shop', '0001_initial')"
                ),
                "shop/migrations/0003_later.py": migration(
                    "migrations.AlterModelOptions(name='order', options={}),\n",
                    deps="('shop', '0002_change')",
                ),
            },
        )

        assert findings(ctx) == []

    def test_other_operations_are_ignored(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(
                "migrations.AddField(model_name='order', name='note', "
                "field=models.CharField(max_length=5, null=True)),\n"
            ),
        )

        assert findings(ctx) == []


class TestTheEvidence:
    def test_it_quotes_the_operation_and_names_the_field_kind(self, make_project: Build) -> None:
        ctx = project(make_project, history(REMOVE))
        (found,) = results(ctx)

        assert found.evidence[1].content == (
            "removed field kind=CharField; not wrapped in SeparateDatabaseAndState"
        )

    def test_an_unknown_field_kind_says_unknown(self, make_project: Build) -> None:
        """The fallback has to name itself.

        Found by mutation testing: blanking the word left the evidence reading
        `removed field kind=`, which looks like a bug in the tool rather than a
        limit of what the replay could see.
        """
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_change.py": migration(
                    "migrations.RemoveField(model_name='order', name='never_declared'),\n",
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        (found,) = results(ctx)

        assert found.evidence[1].content == (
            "removed field kind=unknown; not wrapped in SeparateDatabaseAndState"
        )

    def test_the_evidence_cites_a_file_and_a_line(self, make_project: Build) -> None:
        """Cross-checked against the finding's own location rather than a
        literal, which would pin how this fixture happens to lay out a file."""
        ctx = project(make_project, history(REMOVE))
        (found,) = results(ctx)

        assert found.evidence[1].source == f"{found.location.file}:{found.location.line}"

    def test_the_excerpt_is_the_operation_as_written(self, make_project: Build) -> None:
        ctx = project(make_project, history(REMOVE))
        (found,) = results(ctx)

        assert "RemoveField" in found.evidence[0].content
        assert "code" in found.evidence[0].content
