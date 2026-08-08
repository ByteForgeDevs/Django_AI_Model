"""Tests for `DJM-010`, the first rule that reads the SQL instead of predicting it.

The end-to-end tests build a real Django project with a real virtualenv, point
it at a database of its own, leave its migrations unapplied, and run the rule
the way the engine does. Nothing about the finding is stubbed: the statement it
quotes is the statement `manage.py sqlmigrate` printed.

The unit tests below cover the shapes a live server cannot produce on demand --
a target that is not Postgres, a heading count that disagrees with the parsed
operations, a plan longer than the budget.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from djaudit.context import ProjectContext
from djaudit.live.context import LiveContext
from djaudit.live.sqlmigrate import Emitted, Statement
from djaudit.migrations.nodes import MigrationNode
from djaudit.models import Confidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.rules.djm_pending_blocking_lock import (
    BUDGET,
    BlockingPendingMigration,
    _headings,
    _summarise,
)

from .test_sqlmigrate import interpreter

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")

REWRITE = Statement(
    'ALTER TABLE "blog_post" ALTER COLUMN "title" TYPE varchar(50);', "Alter field title on post"
)
INDEX = Statement('CREATE INDEX "blog_title_idx" ON "blog_post" ("title");', "Create index on post")
HARMLESS = Statement('ALTER TABLE "blog_post" ADD COLUMN "body" text;', "Add field body to post")


def emitted(*statements: Statement, backend: str = "django.db.backends.postgresql") -> Emitted:
    return Emitted("blog", "0002_change", backend, statements)


NODE = MigrationNode(app="blog", name="0002_change", path=Path("/p/blog/migrations/0002.py"))


def judged(*statements: Statement, backend: str = "django.db.backends.postgresql") -> list[Finding]:
    """Run the judgement half of the rule with no subprocess involved."""
    rule = BlockingPendingMigration()
    ctx = ProjectContext(root=Path("/p"))
    return list(rule._judge(ctx, emitted(*statements, backend=backend), NODE))


class TestWhatItReports:
    def test_a_rewrite_under_a_blocking_lock(self) -> None:
        (finding,) = judged(REWRITE)
        assert "is not applied yet" in finding.message
        assert "TYPE varchar(50)" in finding.message

    def test_an_index_build_that_blocks_writes(self) -> None:
        """A weaker lock than the rewrite and still an outage: 1031ms of
        failed writes, measured. The lock-mode framing this rule replaced
        would not have reported it at all."""
        (finding,) = judged(INDEX)
        assert "SHARE" in finding.message

    def test_the_operation_the_author_wrote_is_named(self) -> None:
        """The SQL is the consequence; the operation is the thing to edit."""
        (finding,) = judged(REWRITE)
        assert "Operation: Alter field title on post." in finding.message

    def test_the_whole_sentence(self) -> None:
        """The assertions above are all substrings, and a substring says
        nothing about the punctuation, the joins or the backticks between them.
        Mutation blanked four separate pieces of this sentence without failing
        any of them. The message is the product, so one test reads it whole."""
        (finding,) = judged(REWRITE)
        assert finding.message == (
            "`blog.0002_change` is not applied yet and emits "
            '`ALTER TABLE "blog_post" ALTER COLUMN "title" TYPE varchar(50);`, '
            "which takes ACCESS EXCLUSIVE on blog_post while every row is "
            "rewritten. Operation: Alter field title on post."
        )

    def test_an_operation_django_did_not_name_leaves_the_sentence_closed(self) -> None:
        """The contrast: the trailing clause is conditional, so the sentence has
        to end cleanly without it."""
        (finding,) = judged(Statement(REWRITE.sql, None))
        assert finding.message.endswith("while every row is rewritten.")
        assert "Operation:" not in finding.message

    def test_the_emitted_sql_is_the_evidence(self) -> None:
        (finding,) = judged(REWRITE)
        sql = [e for e in finding.evidence if e.kind is EvidenceKind.SQL]
        assert [e.content for e in sql] == [REWRITE.sql]
        assert sql[0].source == "manage.py sqlmigrate blog 0002_change"

    def test_the_classification_travels_with_it(self) -> None:
        (finding,) = judged(REWRITE)
        (command,) = [e for e in finding.evidence if e.kind is EvidenceKind.COMMAND_OUTPUT]
        assert command.content == (
            "lock=ACCESS EXCLUSIVE work=rewrite table=blog_post "
            "backend=django.db.backends.postgresql atomic=False"
        )
        assert command.source == "manage.py sqlmigrate blog 0002_change"


class TestWhatItStaysQuietAbout:
    def test_a_catalogue_change_under_the_strongest_lock(self) -> None:
        """`ADD COLUMN` takes ACCESS EXCLUSIVE and finishes in 55ms. The rule
        this step was specified as would have reported it."""
        assert judged(HARMLESS) == []

    def test_a_migration_that_emits_nothing(self) -> None:
        assert judged() == []


class TestOneFindingPerMigration:
    def test_two_dangerous_statements_are_one_deploy_incident(self) -> None:
        assert len(judged(INDEX, REWRITE)) == 1

    def test_and_the_worst_one_is_the_one_reported(self) -> None:
        """A rewrite that blocks reads outranks an index build that blocks
        only writes, whichever order they arrive in."""
        for order in ((INDEX, REWRITE), (REWRITE, INDEX)):
            (finding,) = judged(*order)
            assert "TYPE varchar(50)" in finding.message

    def test_the_ordering_is_the_classifier_s_own(self) -> None:
        """Not a second copy of it. Two rankings over the same three axes
        would eventually disagree, and the rule would then contradict the
        evidence it prints."""
        from djaudit.live.locks import worst

        ranked = worst([INDEX.sql, REWRITE.sql])
        assert ranked is not None
        assert ranked.statement == REWRITE.sql


class TestTheHeadings:
    def test_they_come_back_in_order(self) -> None:
        assert _headings([REWRITE, INDEX]) == ["Alter field title on post", "Create index on post"]

    def test_one_operation_emitting_two_statements_is_named_once(self) -> None:
        """Django prints one banner per operation, so a repeat is the same
        operation continuing, not a new one."""
        second = Statement(
            'ALTER TABLE "blog_post" ALTER COLUMN "title" SET NOT NULL;',
            "Alter field title on post",
        )
        assert _headings([REWRITE, second]) == ["Alter field title on post"]

    def test_an_unheaded_statement_contributes_nothing(self) -> None:
        assert _headings([Statement("SELECT 1;")]) == []


class TestTheSummary:
    def test_a_short_statement_is_left_alone(self) -> None:
        assert _summarise("DROP TABLE t;") == "DROP TABLE t;"

    def test_a_long_one_is_cut_and_says_so(self) -> None:
        summary = _summarise("A" * 200)
        assert len(summary) == 90
        assert summary.endswith("\u2026")

    def test_a_wrapped_one_is_collapsed(self) -> None:
        assert _summarise("ALTER TABLE\n  t\n  ADD c int;") == "ALTER TABLE t ADD c int;"


class TestTheRuleDeclaresItself:
    def test_it_is_a_live_rule(self) -> None:
        assert BlockingPendingMigration.meta.tier is Tier.LIVE

    def test_it_reports_at_certain_because_it_read_the_sql(self) -> None:
        assert BlockingPendingMigration.meta.confidence is Confidence.CERTAIN

    def test_it_is_in_the_migration_family(self) -> None:
        assert BlockingPendingMigration.meta.family is Family.DJM
        assert BlockingPendingMigration.meta.severity is Severity.HIGH

    def test_it_names_what_is_lost_without_the_live_tier(self) -> None:
        """A live rule that skips silently tells a reader a clean run happened."""
        assert BlockingPendingMigration.meta.fallback
        assert BlockingPendingMigration.meta.fallback_rules

    def test_the_rules_it_falls_back_to_exist(self) -> None:
        from djaudit.registry import all_rules

        known = {rule.meta.id for rule in all_rules()}
        assert set(BlockingPendingMigration.meta.fallback_rules) <= known


class TestItDoesNothingWithoutConsent:
    def test_no_live_context_means_no_target(self) -> None:
        """The interpreter comes from the context that was disclosed. A rule
        that found its own would run an environment nobody was told about."""
        rule = BlockingPendingMigration()
        assert rule._target(ProjectContext(root=Path("/p"), manage_py=Path("/p/manage.py"))) is None

    def test_no_manage_py_means_no_target(self) -> None:
        rule = BlockingPendingMigration()
        ctx = ProjectContext(root=Path("/p"), live_context=_context())
        assert rule._target(ctx) is None

    def test_and_check_reports_nothing_at_all(self) -> None:
        rule = BlockingPendingMigration()
        assert list(rule.check(ProjectContext(root=Path("/p")))) == []


def _context(engine: str = "django.db.backends.postgresql") -> LiveContext:
    return LiveContext(
        interpreter=interpreter(),
        django_version="6.1",
        settings_module="proj.settings",
        databases=(("default", engine),),
    )


class TestTheBackendGate:
    def test_a_sqlite_target_renders_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Read through a Postgres classifier, SQLite's rendering of the same
        migration hides the rewrite and names a table that does not exist."""
        rule = BlockingPendingMigration()
        monkeypatch.setattr(
            "djaudit.live.sqlmigrate.emit",
            lambda *_a, **_k: emitted(REWRITE, backend="django.db.backends.sqlite3"),
        )
        target = rule._target(
            ProjectContext(
                root=Path("/p"),
                manage_py=Path("/p/manage.py"),
                live_context=_context("django.db.backends.sqlite3"),
            )
        )
        assert target is not None
        assert rule._emit(target, "blog", "0002") is None

    def test_a_postgres_target_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The control, so the test above is not passing on the stub alone."""
        rule = BlockingPendingMigration()
        monkeypatch.setattr("djaudit.live.sqlmigrate.emit", lambda *_a, **_k: emitted(REWRITE))
        target = rule._target(
            ProjectContext(root=Path("/p"), manage_py=Path("/p/manage.py"), live_context=_context())
        )
        assert target is not None
        assert rule._emit(target, "blog", "0002") is not None

    def test_a_refusal_is_not_a_finding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`sqlmigrate` fails for reasons that are the project's business.
        Inventing a lock claim from a failure is the fabrication this family
        exists to avoid."""
        from djaudit.live.sqlmigrate import Refused

        rule = BlockingPendingMigration()
        monkeypatch.setattr(
            "djaudit.live.sqlmigrate.emit", lambda *_a, **_k: Refused("blog", "0002", "no server")
        )
        target = rule._target(
            ProjectContext(root=Path("/p"), manage_py=Path("/p/manage.py"), live_context=_context())
        )
        assert target is not None
        assert rule._emit(target, "blog", "0002") is None


@postgres
class TestAgainstARealProject:
    """The rule, end to end, with nothing stubbed."""

    def context(self, project: Path) -> ProjectContext:
        from djaudit.discovery import build_context

        ctx = build_context(project)
        return replace(
            ctx,
            live=True,
            live_context=LiveContext(
                interpreter=interpreter(project / ".venv" / "bin" / "python"),
                django_version="6.1",
                settings_module="proj.settings",
                databases=(("default", "django.db.backends.postgresql"),),
            ),
        )

    def test_it_reports_the_real_rewrite(self, live_project: Path) -> None:
        findings = list(BlockingPendingMigration().check(self.context(live_project)))
        messages = [f.message for f in findings]
        assert any("TYPE varchar(50)" in m for m in messages), messages

    def test_the_evidence_is_the_sql_django_printed(self, live_project: Path) -> None:
        findings = list(BlockingPendingMigration().check(self.context(live_project)))
        sql = [e.content for f in findings for e in f.evidence if e.kind is EvidenceKind.SQL]
        assert 'ALTER TABLE "blog_post" ALTER COLUMN "title" TYPE varchar(50);' in sql

    def test_it_points_at_the_operation_not_the_file(self, live_project: Path) -> None:
        """The heading index maps to the operation index, so the finding lands
        on the `AlterField` line rather than on line 1."""
        findings = [
            f
            for f in BlockingPendingMigration().check(self.context(live_project))
            if "TYPE varchar(50)" in f.message
        ]
        assert findings
        location = findings[0].location
        assert location.file.endswith("0002_add_body.py")
        assert location.line > 1
        assert location.snippet is not None
        assert "AlterField" in location.snippet

    def test_an_applied_migration_is_not_reported(self, live_project: Path) -> None:
        """The point of reading `django_migrations`: once it has run, its lock
        is not something anybody can act on."""
        import subprocess

        before = list(BlockingPendingMigration().check(self.context(live_project)))
        assert before
        subprocess.run(
            [str(live_project / ".venv" / "bin" / "python"), "manage.py", "migrate", "-v0"],
            cwd=live_project,
            check=True,
            timeout=300,
            capture_output=True,
        )
        assert list(BlockingPendingMigration().check(self.context(live_project))) == []

    def test_without_a_live_context_it_reports_nothing_on_the_same_project(
        self, live_project: Path
    ) -> None:
        """The control on every test above: the findings come from the live
        tier, not from the project merely existing."""
        from djaudit.discovery import build_context

        assert list(BlockingPendingMigration().check(build_context(live_project))) == []


class TestTheBudget:
    def test_it_is_stated_in_the_limitations(self) -> None:
        """A truncated run that does not say so reports a clean project."""
        assert any(str(BUDGET) in text for text in BlockingPendingMigration.meta.limitations)


class TestLocatingAnOperationWithNoAstNode:
    """`Operation.node` is optional, and where it is absent the finding falls
    back to the recorded line span. Nothing reached that branch: every operation
    the parser builds from source carries its `ast.Call`, so the fallback is for
    operations assembled some other way -- and an untested fallback is a guess.
    """

    @staticmethod
    def node(tmp_path: Path, *, with_ast: bool) -> tuple[ProjectContext, MigrationNode]:
        from djaudit.migrations.nodes import Operation, OperationKind

        path = tmp_path / "blog" / "migrations" / "0002.py"
        path.parent.mkdir(parents=True)
        path.write_text(
            "from django.db import migrations, models\n"
            "\n"
            "\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [\n"
            "        migrations.AlterField(\n"
            '            model_name="post",\n'
            '            name="title",\n'
            "            field=models.CharField(max_length=50),\n"
            "        ),\n"
            "    ]\n"
        )
        call = None
        if with_ast:
            import ast as ast_module

            tree = ast_module.parse(path.read_text())
            call = next(n for n in ast_module.walk(tree) if isinstance(n, ast_module.Call))
        operation = Operation(
            name="AlterField",
            kind=OperationKind.SCHEMA,
            lineno=6,
            end_lineno=10,
            node=call,
        )
        return (
            ProjectContext(root=tmp_path),
            MigrationNode(app="blog", name="0002_change", path=path, operations=(operation,)),
        )

    def test_the_line_span_is_used(self, tmp_path: Path) -> None:
        ctx, node = self.node(tmp_path, with_ast=False)
        rule = BlockingPendingMigration()
        location = rule._locate(ctx, emitted(REWRITE), REWRITE, node)
        assert location.line == 6
        assert location.end_line == 10
        assert location.column == 1
        assert location.snippet is not None
        assert "AlterField" in location.snippet

    def test_an_ast_node_is_preferred_when_there_is_one(self, tmp_path: Path) -> None:
        """The control. Both branches land on the same operation, so only a
        case carrying a node can show the node branch was taken."""
        ctx, node = self.node(tmp_path, with_ast=True)
        rule = BlockingPendingMigration()
        location = rule._locate(ctx, emitted(REWRITE), REWRITE, node)
        assert location.line == 6
        assert location.column != 1


class TestEvidenceWhenTheTableCannotBeNamed:
    """`table=` is filled from the classifier, which can decline to answer.

    Not hypothetical: the runner caps how much output it will read, and a
    statement cut off before its `ON <table>` still classifies as a blocking
    index build. The evidence has to say the table is unknown rather than read
    as though the field were empty.
    """

    def test_it_says_unknown(self) -> None:
        cut = Statement('CREATE INDEX "blog_title_idx"', "Create index on post")
        (finding,) = judged(cut)
        (command,) = [e for e in finding.evidence if e.kind is EvidenceKind.COMMAND_OUTPUT]
        assert "table=unknown" in command.content

    def test_a_complete_statement_names_the_table(self) -> None:
        """The control: the same field, filled, on the untruncated statement."""
        (finding,) = judged(INDEX)
        (command,) = [e for e in finding.evidence if e.kind is EvidenceKind.COMMAND_OUTPUT]
        assert "table=blog_post" in command.content
