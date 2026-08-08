"""Tests for `DJM-006`, and for the guard that keeps it from being a nag.

The class to read first is `TestIrreversibilityAloneIsNotTheDefect`. A data
migration with no reverse is usually correct -- the corpus's only leaf instance
deletes rows, and no reverse can undo a delete. What this rule reports is the
narrower shape where that irreversibility is sitting in the same `operations`
list as a schema change, because `Migration.unapply` checks every operation's
`reversible` before running any of them and so revokes the schema change's
reverse too.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Finding, Severity
from djaudit.rules.djm_irreversible_blocks_rollback import IrreversibleDataOperationBlocksRollback
from tests.rules.test_djm_addfield_not_null import Build, findings_for, migration, project

CREATE = """\
migrations.CreateModel(
    name='Order',
    fields=[
        ('id', models.AutoField(primary_key=True, serialize=False)),
        ('code', models.CharField(max_length=10)),
    ],
),
"""

ADD = (
    "migrations.AddField(model_name='order', name='kind', "
    "field=models.CharField(default='x', max_length=4)),\n"
)
RUN = "migrations.RunPython(code=fill_kind),\n"


def findings(ctx: ProjectContext) -> list[str]:
    return findings_for(IrreversibleDataOperationBlocksRollback(), ctx)


def results(ctx: ProjectContext) -> list[Finding]:
    return list(IrreversibleDataOperationBlocksRollback().check(ctx))


def history(second: str) -> dict[str, str]:
    """An app whose leaf migration is ``second``, on a table created earlier."""
    return {
        "shop/migrations/0001_initial.py": migration(CREATE),
        "shop/migrations/0002_change.py": migration(second, deps="('shop', '0001_initial')"),
    }


def line_of(ctx: ProjectContext, needle: str) -> int:
    """The line ``needle`` sits on in the leaf migration, read back from source.

    The message names the blocked operation's line, and writing that number
    into the expected string by hand would pin it to the exact shape of the
    shared `migration` template. Reading it out of the file is an independent
    derivation: the rule is not consulted, so a rule that reported the wrong
    line still fails.
    """
    text = (ctx.root / "shop/migrations/0002_change.py").read_text()
    for number, source in enumerate(text.splitlines(), start=1):
        if needle in source:
            return number
    raise AssertionError(f"no line containing {needle!r} in the fixture")


class TestARevokedReverse:
    def test_reports_a_run_python_beside_a_schema_change(self, make_project: Build) -> None:
        ctx = project(make_project, history(ADD + RUN))

        assert findings(ctx) == [
            "`shop.0002_change` has no reverse for this `RunPython(fill_kind)`, "
            "so unapplying the migration raises `IrreversibleError` before any "
            f"operation runs. The `AddField` at line {line_of(ctx, 'AddField')} "
            "is reversible on its own and cannot be unwound while it shares a "
            "migration with this one."
        ]

    def test_reports_a_run_sql_the_same_way(self, make_project: Build) -> None:
        # `RunSQL.reversible` is `reverse_sql is not None`, the same property
        # under a different name, so the two cannot be told apart by `unapply`.
        ctx = project(
            make_project,
            history(ADD + "migrations.RunSQL(sql='UPDATE shop_order SET kind = 1'),\n"),
        )

        assert len(findings(ctx)) == 1
        assert "`RunSQL`" in findings(ctx)[0]

    def test_the_finding_is_medium(self, make_project: Build) -> None:
        ctx = project(make_project, history(ADD + RUN))

        assert [f.severity for f in results(ctx)] == [Severity.MEDIUM]

    def test_the_order_of_the_two_operations_does_not_matter(self, make_project: Build) -> None:
        # `unapply` raises in a pass over the whole list, so a data operation
        # written above the schema change revokes its reverse just the same.
        ctx = project(make_project, history(RUN + ADD))

        assert len(findings(ctx)) == 1

    def test_each_reverseless_operation_is_reported(self, make_project: Build) -> None:
        # Two findings rather than one, because the remediation is to move each
        # of them out and the reader needs both lines.
        ctx = project(make_project, history(ADD + RUN + "migrations.RunPython(code=fill_two),\n"))

        assert len(findings(ctx)) == 2

    def test_an_index_is_a_reverse_worth_losing(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(
                "migrations.AddIndex(model_name='order', "
                "index=models.Index(fields=['code'], name='shop_order_code')),\n" + RUN
            ),
        )

        assert len(findings(ctx)) == 1
        assert "`AddIndex`" in findings(ctx)[0]

    def test_so_is_a_constraint(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(
                "migrations.AddConstraint(model_name='order', "
                "constraint=models.CheckConstraint(name='c', condition=models.Q(code=''))),\n" + RUN
            ),
        )

        assert len(findings(ctx)) == 1
        assert "`AddConstraint`" in findings(ctx)[0]


class TestIrreversibilityAloneIsNotTheDefect:
    """The corpus's only leaf instance, and the reason this rule has a guard.

    pretix's `sendmail.0011_remove_cross_event_scheduled_mails` is a bare
    `RunPython` that deletes rows. Nothing can undo it, `RunPython.noop` would
    be a lie that lets `migrate` walk backwards past the deletion, and the
    migration moves no schema -- so there is no reverse being revoked.
    """

    def test_a_data_migration_on_its_own_is_not_reported(self, make_project: Build) -> None:
        ctx = project(make_project, history(RUN))

        assert findings(ctx) == []

    def test_the_contrast_is_real(self, make_project: Build) -> None:
        # The same `RunPython`, in the same position, with a schema change
        # added beside it. If this did not report, the test above would be
        # measuring nothing.
        ctx = project(make_project, history(ADD + RUN))

        assert len(findings(ctx)) == 1

    def test_two_data_operations_with_no_schema_between_them_are_quiet(
        self, make_project: Build
    ) -> None:
        ctx = project(make_project, history(RUN + "migrations.RunSQL(sql='DELETE FROM t'),\n"))

        assert findings(ctx) == []


class TestTheAuthorAnsweredTheQuestion:
    def test_a_reverse_function_is_accepted(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(ADD + "migrations.RunPython(code=fill_kind, reverse_code=unfill_kind),\n"),
        )

        assert findings(ctx) == []

    def test_a_noop_reverse_is_accepted(self, make_project: Build) -> None:
        # `RunPython.noop` is a real answer where unapplying drops the column
        # the backfill wrote, which is four of the corpus's eleven cases.
        ctx = project(
            make_project,
            history(
                ADD + "migrations.RunPython(code=fill_kind, "
                "reverse_code=migrations.RunPython.noop),\n"
            ),
        )

        assert findings(ctx) == []

    def test_a_reverse_given_by_position_is_accepted(self, make_project: Build) -> None:
        # `RunPython(code, reverse_code, ...)`, so the second positional
        # argument is the reverse and a rule reading only keywords misses it.
        ctx = project(make_project, history(ADD + "migrations.RunPython(fill_kind, unfill),\n"))

        assert findings(ctx) == []

    def test_reverse_sql_is_accepted(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(
                ADD + "migrations.RunSQL(sql='UPDATE t SET a = 1', "
                "reverse_sql='UPDATE t SET a = NULL'),\n"
            ),
        )

        assert findings(ctx) == []


class TestOperationsWithNothingToUnwind:
    """Co-located operations that emit no DDL, so losing their reverse costs nothing."""

    def test_alter_model_options_is_not_a_schema_change(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(
                "migrations.AlterModelOptions(name='order', "
                "options={'ordering': ['code']}),\n" + RUN
            ),
        )

        assert findings(ctx) == []

    def test_alter_model_managers_is_not_either(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history("migrations.AlterModelManagers(name='order', managers=[]),\n" + RUN),
        )

        assert findings(ctx) == []

    def test_an_operation_we_cannot_classify_is_left_alone(self, make_project: Build) -> None:
        # A third-party operation might emit no DDL at all. Assuming it does
        # would be the same mistake as calling it harmless, in the direction
        # that reports rather than the one that stays quiet.
        ctx = project(make_project, history("psqlextra.PartitionedModel(name='order'),\n" + RUN))

        assert findings(ctx) == []

    def test_but_a_schema_change_among_them_is_still_reported(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(
                "migrations.AlterModelOptions(name='order', options={}),\n" + ADD + RUN,
            ),
        )

        assert len(findings(ctx)) == 1
        assert "`AddField`" in findings(ctx)[0]


class TestTheWrapperIsNotAnEscape:
    """`SeparateDatabaseAndState` does not override `reversible`.

    `Operation.reversible` is a plain class attribute set to `True`, so a
    reverse-less operation inside the wrapper passes `unapply`'s first phase
    and raises from the second instead -- after the operations listed below it
    have already been unapplied. Under `atomic = False` that partial unwind
    stays. Both halves of the nesting are reported.
    """

    def test_a_wrapped_data_operation_is_reported(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(
                ADD + "migrations.SeparateDatabaseAndState(\n"
                "    database_operations=[migrations.RunPython(code=fill_kind)],\n"
                "),\n"
            ),
        )

        assert len(findings(ctx)) == 1

    def test_a_wrapped_schema_change_is_still_a_reverse_worth_losing(
        self, make_project: Build
    ) -> None:
        ctx = project(
            make_project,
            history(
                "migrations.SeparateDatabaseAndState(\n"
                "    database_operations=[migrations.AddField(model_name='order', "
                "name='kind', field=models.CharField(default='x', max_length=4))],\n"
                "),\n" + RUN
            ),
        )

        assert len(findings(ctx)) == 1
        assert "`AddField`" in findings(ctx)[0]

    def test_the_wrapper_itself_is_not_counted_as_a_schema_change(
        self, make_project: Build
    ) -> None:
        # `SeparateDatabaseAndState` is its own kind. If the wrapper counted,
        # a state-only two-step beside a data pass would report on nothing.
        ctx = project(
            make_project,
            history(
                "migrations.SeparateDatabaseAndState(\n"
                "    state_operations=[migrations.AlterModelOptions(name='order', "
                "options={})],\n"
                "),\n" + RUN
            ),
        )

        assert findings(ctx) == []


class TestTheEvidence:
    def test_the_excerpt_quotes_the_data_operation(self, make_project: Build) -> None:
        ctx = project(make_project, history(ADD + RUN))

        assert "RunPython" in results(ctx)[0].evidence[0].content

    def test_the_evidence_names_what_the_reverse_blocks(self, make_project: Build) -> None:
        ctx = project(make_project, history(ADD + RUN))

        assert (
            results(ctx)[0].evidence[1].content
            == "no reverse supplied; blocks the reverse of AddField"
        )

    def test_the_blocked_operations_are_named_once_each_in_order(self, make_project: Build) -> None:
        ctx = project(
            make_project,
            history(
                ADD + "migrations.AddField(model_name='order', name='rank', "
                "field=models.IntegerField(default=0)),\n"
                "migrations.AddIndex(model_name='order', "
                "index=models.Index(fields=['code'], name='shop_order_code')),\n" + RUN
            ),
        )

        assert (
            results(ctx)[0].evidence[1].content
            == "no reverse supplied; blocks the reverse of AddField, AddIndex"
        )

    def test_the_message_names_the_first_blocked_operation_and_the_evidence_holds_order(
        self, make_project: Build
    ) -> None:
        # Two things at once, both invisible to a suite that only ever has one
        # schema operation. The message points at the *first* blocked
        # operation, so its line is somewhere to start reading; the evidence
        # lists them in the order they appear in the file. A set would have
        # given the finding a different fingerprint on different runs, and
        # `sorted` would have been deterministic but untestable -- either
        # ordering of two names is the sorted one for some pair of names.
        ctx = project(
            make_project,
            history(
                "migrations.AddIndex(model_name='order', "
                "index=models.Index(fields=['code'], name='shop_order_code')),\n" + ADD + RUN
            ),
        )
        finding = results(ctx)[0]

        assert f"The `AddIndex` at line {line_of(ctx, 'AddIndex')} " in finding.message
        assert (
            finding.evidence[1].content
            == "no reverse supplied; blocks the reverse of AddIndex, AddField"
        )

    def test_the_evidence_cites_the_operation_not_the_migration(self, make_project: Build) -> None:
        ctx = project(make_project, history(ADD + RUN))
        finding = results(ctx)[0]

        assert finding.evidence[1].source.endswith(f":{finding.location.line}")
        assert finding.location.line > 1

    def test_a_run_python_nobody_can_name_is_still_reported(self, make_project: Build) -> None:
        # A lambda has no dotted name, so the message falls back to the bare
        # operation rather than printing `RunPython(None)`.
        ctx = project(
            make_project,
            history(ADD + "migrations.RunPython(code=lambda apps, editor: None),\n"),
        )

        assert "this `RunPython`, so unapplying" in findings(ctx)[0]


class TestScope:
    def test_only_the_leaf_of_the_history_is_reported(self, make_project: Build) -> None:
        # A migration in shipped history already ran, and the deploy it would
        # have blocked the rollback of is over.
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_change.py": migration(
                    ADD + RUN, deps="('shop', '0001_initial')"
                ),
                "shop/migrations/0003_later.py": migration(
                    "migrations.AddField(model_name='order', name='paid', "
                    "field=models.BooleanField(default=False)),\n",
                    deps="('shop', '0002_change')",
                ),
            },
        )

        assert findings(ctx) == []

    def test_a_schema_change_in_another_migration_does_not_count(self, make_project: Build) -> None:
        # The reverse this rule cares about is the one in the same `operations`
        # list. A separate migration unapplies on its own, which is exactly
        # what the remediation asks for.
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE),
                "shop/migrations/0002_kind.py": migration(ADD, deps="('shop', '0001_initial')"),
                "shop/migrations/0003_fill.py": migration(RUN, deps="('shop', '0002_kind')"),
            },
        )

        assert findings(ctx) == []


class TestOperationsThisRuleIgnores:
    def test_a_schema_operation_is_not_a_data_operation(self, make_project: Build) -> None:
        # `AddField.reverse` is `None`, not `False`, because the question does
        # not apply to it. Only `RunPython` and `RunSQL` are asked.
        ctx = project(make_project, history(ADD))

        assert findings(ctx) == []

    def test_a_create_model_beside_a_data_pass_still_counts(self, make_project: Build) -> None:
        # The table is empty either way, so `populated` is deliberately not
        # consulted here: the rollback is blocked whether or not there is data.
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CREATE + RUN),
            },
        )

        assert len(findings(ctx)) == 1
        assert "`CreateModel`" in findings(ctx)[0]


class TestTheRuleItself:
    def test_it_declares_the_leaf_scope_limitation(self) -> None:
        assert any(
            "leaf" in item for item in IrreversibleDataOperationBlocksRollback.meta.limitations
        )

    def test_it_declares_that_a_lone_data_migration_is_not_reported(self) -> None:
        assert any(
            "irreversible on its own" in item
            for item in IrreversibleDataOperationBlocksRollback.meta.limitations
        )

    def test_the_remediation_warns_against_a_dishonest_noop(self) -> None:
        # The obvious fix is the wrong one for a destructive pass, and a
        # remediation that did not say so would be actively harmful advice.
        assert "walk backwards past" in IrreversibleDataOperationBlocksRollback.meta.remediation

    def test_every_limitation_is_a_written_sentence(self) -> None:
        for item in IrreversibleDataOperationBlocksRollback.meta.limitations:
            assert item[0].isupper()
            assert item.endswith(".")
