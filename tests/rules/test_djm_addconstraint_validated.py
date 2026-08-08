"""Tests for `DJM-009`, mostly about which remediation each constraint gets.

The rule's substance is not "report `AddConstraint`" -- that is three lines --
but the branch that decides what to tell the reader. `AddConstraintNotValid`
raises `TypeError` on anything that is not a `CheckConstraint`, so a rule that
gave one answer for every constraint would be handing most readers a fix that
does not run. `TestTheRemediationDependsOnTheConstraint` is where that is
measured.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.rules.djm_addconstraint_validated import ValidatedAddConstraint
from tests.rules.test_djm_addfield_not_null import Build, migration, project

CHECK = (
    "migrations.AddConstraint(model_name='order', "
    "constraint=models.CheckConstraint(condition=models.Q(code__gte=0), name='c')),\n"
)
UNIQUE = (
    "migrations.AddConstraint(model_name='order', "
    "constraint=models.UniqueConstraint(fields=['code'], name='u')),\n"
)
UNIQUE_CONDITIONAL = (
    "migrations.AddConstraint(model_name='order', "
    "constraint=models.UniqueConstraint(fields=['code'], name='u', "
    "condition=models.Q(code__gt=0))),\n"
)
CREATE = (
    "migrations.CreateModel(name='Order', "
    "fields=[('id', models.AutoField(primary_key=True, serialize=False))]),\n"
)


def build(build_project: Build, operations: str) -> ProjectContext:
    return project(build_project, {"shop/migrations/0001_initial.py": migration(operations)})


def report(ctx: ProjectContext) -> list[Finding]:
    """Findings, run through `check` so the family's scope is resolved.

    Calling `inspect` directly would leave `scope` empty and measure a rule
    that reports nothing.
    """
    return list(ValidatedAddConstraint().check(ctx))


def one(build_project: Build, operations: str) -> Finding:
    (finding,) = report(build(build_project, operations))
    return finding


class TestTheRemediationDependsOnTheConstraint:
    """`AddConstraintNotValid.constraint must be a check constraint.`

    Django raises `TypeError` with that message on anything else, so the
    `NOT VALID` advice everyone reaches for first is available to exactly one
    of the constraint classes. The other branches have to say something else,
    and the something else is not `AddIndexConcurrently` either -- `Index` has
    no `unique` argument.
    """

    def test_a_check_constraint_is_told_to_use_add_constraint_not_valid(
        self, make_project: Build
    ) -> None:
        assert "AddConstraintNotValid" in one(make_project, CHECK).remediation

    def test_a_check_constraint_is_told_to_validate_it_afterwards(
        self, make_project: Build
    ) -> None:
        """Half the fix is useless: `NOT VALID` alone leaves it unenforced."""
        assert "ValidateConstraint" in one(make_project, CHECK).remediation

    def test_a_unique_constraint_is_not_told_to_use_add_constraint_not_valid(
        self, make_project: Build
    ) -> None:
        assert "AddConstraintNotValid" not in one(make_project, UNIQUE).remediation

    def test_a_unique_constraint_is_told_why_not_valid_is_unavailable(
        self, make_project: Build
    ) -> None:
        remediation = one(make_project, UNIQUE).remediation
        assert "no `NOT VALID` for a unique constraint" in remediation
        assert "`CHECK` and `FOREIGN KEY` only" in remediation

    def test_a_unique_constraint_is_not_told_to_use_add_index_concurrently(
        self, make_project: Build
    ) -> None:
        """It takes an `Index`, and `Index` has no `unique` argument."""
        remediation = one(make_project, UNIQUE).remediation
        assert "`Index` has no `unique` argument" in remediation
        assert "CREATE UNIQUE INDEX CONCURRENTLY" in remediation

    def test_the_concurrent_route_keeps_migration_state_correct(self, make_project: Build) -> None:
        """Raw SQL alone would leave the constraint out of the state graph."""
        remediation = one(make_project, UNIQUE).remediation
        assert "SeparateDatabaseAndState" in remediation
        assert "atomic = False" in remediation

    def test_the_concurrent_route_admits_it_may_not_be_worth_it(self, make_project: Build) -> None:
        assert "reasonable thing to decline" in one(make_project, UNIQUE).remediation


class TestSeverityFollowsTheLock:
    """A conditional unique constraint is not an `ALTER TABLE` at all.

    `_create_unique_sql` switches to `sql_create_unique_index` when any of
    `condition`, `include`, `opclasses` or `expressions` is given, so what runs
    is a bare `CREATE UNIQUE INDEX`. That takes `SHARE`, which permits reads,
    and a readable table is not an unavailable one.
    """

    def test_a_check_constraint_is_high(self, make_project: Build) -> None:
        assert one(make_project, CHECK).severity is Severity.HIGH

    def test_a_plain_unique_constraint_is_high(self, make_project: Build) -> None:
        assert one(make_project, UNIQUE).severity is Severity.HIGH

    def test_a_conditional_unique_constraint_is_medium(self, make_project: Build) -> None:
        assert one(make_project, UNIQUE_CONDITIONAL).severity is Severity.MEDIUM

    def test_include_also_makes_it_index_backed(self, make_project: Build) -> None:
        operations = UNIQUE.replace("name='u'", "name='u', include=['id']")
        assert one(make_project, operations).severity is Severity.MEDIUM

    def test_opclasses_also_makes_it_index_backed(self, make_project: Build) -> None:
        operations = UNIQUE.replace("name='u'", "name='u', opclasses=['text_ops']")
        assert one(make_project, operations).severity is Severity.MEDIUM

    def test_expressions_also_makes_it_index_backed(self, make_project: Build) -> None:
        operations = UNIQUE.replace("name='u'", "name='u', expressions=[models.F('code')]")
        assert one(make_project, operations).severity is Severity.MEDIUM

    def test_a_positional_expression_makes_it_index_backed(self, make_project: Build) -> None:
        """`UniqueConstraint(*expressions, ...)` -- the arguments are positional."""
        operations = (
            "migrations.AddConstraint(model_name='order', "
            "constraint=models.UniqueConstraint(models.F('code'), name='u')),\n"
        )
        assert one(make_project, operations).severity is Severity.MEDIUM

    def test_the_high_cases_name_access_exclusive(self, make_project: Build) -> None:
        assert "`ACCESS EXCLUSIVE`" in one(make_project, CHECK).message
        assert "`ACCESS EXCLUSIVE`" in one(make_project, UNIQUE).message

    def test_the_medium_case_names_share_instead(self, make_project: Build) -> None:
        message = one(make_project, UNIQUE_CONDITIONAL).message
        assert "`SHARE`" in message
        assert "ACCESS EXCLUSIVE" not in message


class TestWhatItDeclinesToReport:
    def test_a_constraint_class_it_cannot_read_is_not_reported(self, make_project: Build) -> None:
        """Every remediation turns on the class, and the wrong one raises."""
        operations = (
            "migrations.AddConstraint(model_name='order', constraint=build_constraint()),\n"
        )
        assert report(build(make_project, operations)) == []

    def test_a_constraint_that_is_not_a_call_is_not_reported(self, make_project: Build) -> None:
        operations = "migrations.AddConstraint(model_name='order', constraint=CONSTRAINT),\n"
        assert report(build(make_project, operations)) == []

    def test_a_project_defined_constraint_subclass_is_not_reported(
        self, make_project: Build
    ) -> None:
        operations = (
            "migrations.AddConstraint(model_name='order', "
            "constraint=TenantScopedConstraint(name='t')),\n"
        )
        assert report(build(make_project, operations)) == []

    def test_a_constraint_on_a_table_this_migration_creates_is_not_reported(
        self, make_project: Build
    ) -> None:
        """The scan proves the constraint against no rows."""
        assert report(build(make_project, CREATE + CHECK)) == []

    def test_removing_a_constraint_is_not_reported(self, make_project: Build) -> None:
        operations = "migrations.RemoveConstraint(model_name='order', name='c'),\n"
        assert report(build(make_project, operations)) == []

    def test_add_constraint_not_valid_is_the_fix_and_is_not_reported(
        self, make_project: Build
    ) -> None:
        operations = (
            "AddConstraintNotValid(model_name='order', "
            "constraint=models.CheckConstraint(condition=models.Q(code__gte=0), name='c')),\n"
        )
        assert report(build(make_project, operations)) == []

    def test_an_add_index_is_djm003s_and_is_not_reported_here(self, make_project: Build) -> None:
        operations = (
            "migrations.AddIndex(model_name='order', "
            "index=models.Index(fields=['code'], name='i')),\n"
        )
        assert report(build(make_project, operations)) == []

    def test_a_migration_that_is_not_a_leaf_is_not_reported(self, make_project: Build) -> None:
        """The family reports only what has not run yet."""
        ctx = project(
            make_project,
            {
                "shop/migrations/0001_initial.py": migration(CHECK),
                "shop/migrations/0002_later.py": migration(
                    "migrations.AlterModelOptions(name='order', options={}),\n",
                    deps="('shop', '0001_initial')",
                ),
            },
        )
        assert report(ctx) == []


class TestHowItIsWrittenDown:
    def test_the_message_names_the_migration_and_the_model(self, make_project: Build) -> None:
        assert one(make_project, CHECK).message == (
            "`shop.0001_initial` adds a constraint to `order`, which Postgres "
            "validates by reading every existing row while holding "
            "`ACCESS EXCLUSIVE` on the table."
        )

    def test_it_points_at_the_operation_not_the_file(self, make_project: Build) -> None:
        """A migration is a list, and only one entry is the problem.

        Two findings landing on the same line would mean the rule was locating
        the file, which no assertion about a single finding can rule out.
        """
        first, second = report(build(make_project, CHECK + UNIQUE))
        assert first.location.line != second.location.line
        assert "AddConstraint" in (first.location.snippet or "")

    def test_the_evidence_quotes_the_operation(self, make_project: Build) -> None:
        excerpt, _ = one(make_project, CHECK).evidence
        assert excerpt.kind is EvidenceKind.AST
        assert "CheckConstraint" in excerpt.content
        assert excerpt.source.endswith("0001_initial.py:8")

    def test_the_evidence_names_the_statement_django_emits(self, make_project: Build) -> None:
        """The lock claim is only checkable if the statement is stated."""
        _, emitted = one(make_project, CHECK).evidence
        assert emitted.content == (
            "Django emits `ALTER TABLE ... ADD CONSTRAINT ... CHECK (...)`, "
            "which takes `ACCESS EXCLUSIVE`"
        )
        assert emitted.source.endswith("0001_initial.py:8")

    def test_the_index_backed_evidence_names_create_unique_index(self, make_project: Build) -> None:
        _, emitted = one(make_project, UNIQUE_CONDITIONAL).evidence
        assert emitted.content == (
            "Django emits `CREATE UNIQUE INDEX ... ON ...`, which takes `SHARE`"
        )

    def test_the_plain_unique_evidence_names_alter_table(self, make_project: Build) -> None:
        _, emitted = one(make_project, UNIQUE).evidence
        assert emitted.content == (
            "Django emits `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (...)`, "
            "which takes `ACCESS EXCLUSIVE`"
        )

    def test_it_is_a_firm_static_djm_finding(self, make_project: Build) -> None:
        finding = one(make_project, CHECK)
        assert finding.rule_id == "DJM-009"
        assert finding.family is Family.DJM
        assert finding.tier is Tier.STATIC
        assert finding.confidence is Confidence.FIRM

    def test_two_constraints_are_two_findings(self, make_project: Build) -> None:
        assert len(report(build(make_project, CHECK + UNIQUE))) == 2


class TestTheClassNameIsReadHoweverItWasImported:
    """`models.CheckConstraint`, `CheckConstraint` and the dotted path are one
    class, and all three spellings appear in real migrations."""

    def test_an_attribute_path_is_read(self, make_project: Build) -> None:
        assert one(make_project, CHECK).severity is Severity.HIGH

    def test_a_bare_name_is_read(self, make_project: Build) -> None:
        operations = CHECK.replace("models.CheckConstraint", "CheckConstraint")
        assert one(make_project, operations).severity is Severity.HIGH

    def test_a_fully_dotted_path_is_read(self, make_project: Build) -> None:
        operations = CHECK.replace("models.CheckConstraint", "django.db.models.CheckConstraint")
        assert one(make_project, operations).severity is Severity.HIGH

    def test_an_exclusion_constraint_is_read(self, make_project: Build) -> None:
        operations = (
            "migrations.AddConstraint(model_name='order', "
            "constraint=ExclusionConstraint(name='e', "
            "expressions=[('code', RangeOperators.EQUAL)])),\n"
        )
        finding = one(make_project, operations)
        assert finding.severity is Severity.HIGH
        assert finding.evidence[1].content == (
            "Django emits `ALTER TABLE ... ADD CONSTRAINT ... EXCLUDE USING ...`, "
            "which takes `ACCESS EXCLUSIVE`"
        )


class TestTheRuleDeclaresItself:
    def test_it_documents_that_it_only_sees_leaves(self) -> None:
        assert any("leaf" in text for text in ValidatedAddConstraint.meta.limitations)

    def test_it_documents_that_it_cannot_size_the_table(self) -> None:
        assert any("reltuples" in text for text in ValidatedAddConstraint.meta.limitations)

    def test_it_documents_declining_unreadable_constraints(self) -> None:
        assert any(
            "not reported at all" in text for text in ValidatedAddConstraint.meta.limitations
        )

    def test_it_documents_that_the_advice_is_postgres_specific(self) -> None:
        assert any("Postgres behaviour" in text for text in ValidatedAddConstraint.meta.limitations)
