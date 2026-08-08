"""Tests for rendering a migration to SQL through the target's own Django.

The fixtures in this module are not written by hand. `POSTGRES` and `SQLITE`
are the verbatim stdout of `manage.py sqlmigrate blog 0002` on a real project,
against a real PostgreSQL 18.1 server and a real SQLite file, for the same
three operations. They are pasted rather than invented because the format is
Django's and every guess about it has been wrong at least once -- the banner is
three lines, not one; the unsupported marker is a comment, not an error; and a
non-atomic migration has no `BEGIN` to anchor on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from djaudit.live.context import LiveContext
from djaudit.live.interpreter import Creator, Interpreter
from djaudit.live.locks import Work, classify, worst
from djaudit.live.runner import Outcome
from djaudit.live.sqlmigrate import (
    UNSUPPORTED,
    Emitted,
    Refused,
    Statement,
    Target,
    _refusal,
    emit,
    parse,
)

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")

POSTGRES = """BEGIN;
--
-- Add field body to post
--
ALTER TABLE "blog_post" ADD COLUMN "body" text DEFAULT '' NOT NULL;
ALTER TABLE "blog_post" ALTER COLUMN "body" DROP DEFAULT;
--
-- Create index blog_title_idx on field(s) title of model post
--
CREATE INDEX "blog_title_idx" ON "blog_post" ("title");
--
-- Alter field title on post
--
ALTER TABLE "blog_post" ALTER COLUMN "title" TYPE varchar(50);
COMMIT;
"""

SQLITE = """BEGIN;
--
-- Add field body to post
--
CREATE TABLE "new__blog_post" ("body" text NOT NULL, "id" integer NOT NULL PRIMARY KEY \
AUTOINCREMENT, "title" varchar(200) NOT NULL);
INSERT INTO "new__blog_post" ("id", "title", "body") SELECT "id", "title", '' FROM "blog_post";
DROP TABLE "blog_post";
ALTER TABLE "new__blog_post" RENAME TO "blog_post";
--
-- Create index blog_title_idx on field(s) title of model post
--
CREATE INDEX "blog_title_idx" ON "blog_post" ("title");
--
-- Alter field title on post
--
CREATE TABLE "new__blog_post" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "body" text \
NOT NULL, "title" varchar(50) NOT NULL);
INSERT INTO "new__blog_post" ("id", "body", "title") SELECT "id", "body", "title" FROM "blog_post";
DROP TABLE "blog_post";
ALTER TABLE "new__blog_post" RENAME TO "blog_post";
CREATE INDEX "blog_title_idx" ON "blog_post" ("title");
COMMIT;
"""

COMMENTED = """BEGIN;
--
-- Change Meta options on post
--
-- (no-op)
--
-- Raw SQL operation
--
-- a leading comment
SELECT 1;
-- a trailing comment;
COMMIT;
"""
"""Verbatim output for `AlterModelOptions` plus a `RunSQL` carrying comments.

Both comment forms are Django's own, not contrived: `-- (no-op)` is what an
options-only operation renders to, and the leading comment is the author's SQL
coming back unchanged.
"""

RUN_PYTHON = """--
-- Raw Python operation
--
-- THIS OPERATION CANNOT BE WRITTEN AS SQL
"""


def interpreter(executable: Path = Path("/usr/bin/python3")) -> Interpreter:
    return Interpreter(
        executable=executable,
        prefix=executable.parent.parent,
        creator=Creator.UV,
        version="3.13.9",
        found_by="a test",
    )


class TestParsingRealOutput:
    def test_it_finds_every_statement(self) -> None:
        statements, _, _ = parse(POSTGRES)
        assert len(statements) == 4

    def test_it_keeps_the_operation_that_produced_each_one(self) -> None:
        """The heading is the author's sentence and the SQL is the consequence.
        A finding that reports one without the other is half a finding."""
        statements, _, _ = parse(POSTGRES)
        assert [s.operation for s in statements] == [
            "Add field body to post",
            "Add field body to post",
            "Create index blog_title_idx on field(s) title of model post",
            "Alter field title on post",
        ]

    def test_it_does_not_keep_the_banner_dashes(self) -> None:
        statements, _, _ = parse(POSTGRES)
        assert not any("--" in s.sql for s in statements)

    def test_it_reads_begin_as_atomic(self) -> None:
        _, atomic, _ = parse(POSTGRES)
        assert atomic is True

    def test_begin_and_commit_are_not_statements(self) -> None:
        statements, _, _ = parse(POSTGRES)
        assert not any(s.sql.startswith(("BEGIN", "COMMIT")) for s in statements)

    def test_a_migration_with_no_begin_is_not_atomic(self) -> None:
        """`RunPython` with `atomic = False` renders no transaction at all, and
        a half-applied schema is a different incident from a rolled-back one."""
        _, atomic, _ = parse(RUN_PYTHON)
        assert atomic is False

    def test_an_unsupported_operation_is_named_not_dropped(self) -> None:
        statements, _, unsupported = parse(RUN_PYTHON)
        assert unsupported == ("Raw Python operation",)
        assert statements == ()

    def test_the_unsupported_marker_is_django_s_own_wording(self) -> None:
        """It is matched literally, so it is worth failing loudly if Django
        ever rewords it rather than silently reporting zero statements."""
        assert UNSUPPORTED in RUN_PYTHON

    def test_nothing_at_all_parses_to_nothing(self) -> None:
        assert parse("") == ((), False, ())

    def test_a_statement_stringifies_to_its_sql(self) -> None:
        assert str(Statement("SELECT 1;", "op")) == "SELECT 1;"


class TestTheThreeLineBanner:
    """The banner opens with `--`, describes, and closes with `--`. A parser
    that cannot tell the closing rule from the next opening one reads the
    first comment after a banner as a new heading -- and Django emits one
    routinely."""

    def test_a_comment_after_the_banner_is_not_a_heading(self) -> None:
        """The defect this state machine replaced a flag to fix. The flag
        version attributed `SELECT 1;` to `a leading comment`."""
        statements, _, _ = parse(COMMENTED)
        assert [s.operation for s in statements] == ["Raw SQL operation"]

    def test_a_no_op_operation_does_not_steal_the_next_heading(self) -> None:
        """`-- (no-op)` sits between two banners. Consumed as a heading it
        would also leave `Raw SQL operation` unread."""
        statements, _, _ = parse(COMMENTED)
        assert not any(s.operation == "(no-op)" for s in statements)

    def test_a_trailing_comment_is_not_a_statement(self) -> None:
        """It ends in `;`, which is the statement terminator."""
        statements, _, _ = parse(COMMENTED)
        assert [s.sql for s in statements] == ["SELECT 1;"]

    def test_a_lone_rule_before_sql_does_not_capture_the_comment_after_it(self) -> None:
        """The reset on a non-comment line, which stops an unclosed banner
        from running on past the SQL it was supposed to introduce."""
        statements, _, _ = parse("--\nSELECT 1;\n-- afterwards\nSELECT 2;\n")
        assert [s.operation for s in statements] == [None, None]


class TestStatementsSpanningLines:
    def test_a_wrapped_statement_is_joined(self) -> None:
        """Django wraps long `CREATE TABLE`s. Splitting on newlines would turn
        one statement into several fragments that classify as nothing."""
        statements, _, _ = parse(
            'CREATE TABLE "t" (\n    "id" integer NOT NULL,\n    "n" varchar(10)\n);\n'
        )
        assert len(statements) == 1
        assert statements[0].sql == 'CREATE TABLE "t" ( "id" integer NOT NULL, "n" varchar(10) );'

    def test_a_semicolon_inside_a_literal_is_not_a_boundary(self) -> None:
        """`RunSQL` bodies and default strings contain semicolons. Splitting on
        every `;` would cut a statement in half mid-literal."""
        statements, _, _ = parse("UPDATE t SET s = 'a;b' WHERE x = 1;\n")
        assert len(statements) == 1
        assert "a;b" in statements[0].sql

    def test_a_trailing_statement_with_no_semicolon_is_still_returned(self) -> None:
        statements, _, _ = parse("ALTER TABLE t ADD COLUMN c integer\n")
        assert len(statements) == 1


class TestTheBackendTravelsWithTheSql:
    """The claim this module exists to make safe."""

    def test_the_same_migration_is_not_the_same_sql(self) -> None:
        """Measured on one project, one migration, two backends."""
        pg, _, _ = parse(POSTGRES)
        lite, _, _ = parse(SQLITE)
        assert len(pg) == 4
        assert len(lite) == 10

    def test_the_real_danger_disappears_when_sqlite_output_is_read(self) -> None:
        """The reason `for_postgres` exists, stated as a measurement.

        The migration's dangerous operation is `AlterField(max_length=50)`.
        Postgres renders it as `ALTER COLUMN TYPE`, which is a full rewrite --
        2870ms on two million rows, measured. SQLite renders the same
        operation as a `CREATE TABLE` / `INSERT SELECT` / `DROP` / `RENAME`
        rebuild, and none of those four statements is a Postgres rewrite.

        So the classifier reading SQLite output does not merely get the
        severity wrong. The outage is *not in the output at all*, and the
        worst thing it can find is the index build.
        """
        pg_worst = worst([statement.sql for statement in parse(POSTGRES)[0]])
        lite_worst = worst([statement.sql for statement in parse(SQLITE)[0]])
        assert pg_worst is not None
        assert lite_worst is not None
        assert pg_worst.work is Work.REWRITE
        assert lite_worst.work is Work.SCAN
        assert "TYPE varchar(50)" in pg_worst.statement

    def test_and_it_invents_a_table_that_does_not_exist(self) -> None:
        """`new__blog_post` is SQLite's scratch table. It lives for four
        statements and never exists in the production database at all, so a
        finding citing it by name is a finding about nothing."""
        tables = {classify(statement.sql).table for statement in parse(SQLITE)[0]}
        assert "new__blog_post" in tables
        assert "new__blog_post" not in {
            classify(statement.sql).table for statement in parse(POSTGRES)[0]
        }

    def test_and_it_calls_dropping_the_production_table_harmless(self) -> None:
        """The rebuild really does emit `DROP TABLE "blog_post"`, naming the
        live table. Classified for Postgres that is honest -- dropping a table
        is instant catalogue work -- which is exactly the problem: the
        sentence is true of Postgres and false of what SQLite is doing."""
        drop = next(
            classify(statement.sql).table
            for statement in parse(SQLITE)[0]
            if statement.sql.startswith("DROP TABLE")
        )
        assert drop == "blog_post"

    def test_postgres_passes_the_gate(self) -> None:
        assert Emitted("blog", "0002", "django.db.backends.postgresql").for_postgres

    def test_postgis_passes_the_gate(self) -> None:
        """NetBox and every GeoDjango project run `postgis`, which is Postgres."""
        assert Emitted("blog", "0002", "django.contrib.gis.db.backends.postgis").for_postgres

    def test_sqlite_does_not(self) -> None:
        assert not Emitted("blog", "0002", "django.db.backends.sqlite3").for_postgres

    def test_mysql_does_not(self) -> None:
        assert not Emitted("blog", "0002", "django.db.backends.mysql").for_postgres

    def test_a_backend_merely_containing_the_word_does_not(self) -> None:
        """`endswith`, not `in`. A third-party wrapper named
        `postgresql_proxy_over_mysql` is not something we have measured."""
        assert not Emitted("blog", "0002", "myapp.postgresql.shim").for_postgres


class TestBuildingATargetFromALiveContext:
    def context(self, databases: tuple[tuple[str, str], ...]) -> LiveContext:
        return LiveContext(
            interpreter=interpreter(),
            django_version="6.1",
            settings_module="proj.settings",
            databases=databases,
            installed_apps=(),
            debug=False,
        )

    def test_it_reads_the_engine_off_the_alias(self) -> None:
        context = self.context((("default", "django.db.backends.postgresql"),))
        target = Target.of(context, Path("/p/manage.py"))
        assert target is not None
        assert target.backend == "django.db.backends.postgresql"

    def test_a_missing_alias_is_declined_not_guessed(self) -> None:
        """Returning a target with an assumed backend is how a Postgres claim
        gets made about a SQLite database."""
        context = self.context((("default", "django.db.backends.postgresql"),))
        assert Target.of(context, Path("/p/manage.py"), "replica") is None

    def test_an_emission_is_not_atomic_unless_it_says_so(self) -> None:
        """The default has to be the cautious one: claiming a transaction that
        is not there turns a half-applied schema into an unreported one."""
        assert Emitted("blog", "0001", "django.db.backends.postgresql").atomic is False

    def test_a_non_default_alias_is_passed_through(self) -> None:
        context = self.context(
            (
                ("default", "django.db.backends.postgresql"),
                ("replica", "django.db.backends.sqlite3"),
            )
        )
        target = Target.of(context, Path("/p/manage.py"), "replica")
        assert target is not None
        assert target.backend == "django.db.backends.sqlite3"
        assert target.database == "replica"

    def test_the_default_alias_is_not_passed_through(self) -> None:
        """`--database default` is the default; sending it anyway is noise in
        every command line we would show a reader as evidence."""
        context = self.context((("default", "django.db.backends.postgresql"),))
        target = Target.of(context, Path("/p/manage.py"))
        assert target is not None
        assert target.database is None


class TestRefusal:
    def test_it_carries_the_target_s_own_last_word(self) -> None:
        outcome = Outcome(
            command=("python", "manage.py"),
            returncode=0,
            stdout="",
            stderr="Traceback...\nCommandError: App 'nope' does not have migrations.\n",
            duration=0.1,
            truncated=False,
        )
        assert _refusal(outcome) == "CommandError: App 'nope' does not have migrations."

    def test_it_ignores_trailing_blank_lines(self) -> None:
        outcome = Outcome(
            command=("python",),
            returncode=0,
            stdout="",
            stderr="the real error\n\n\n",
            duration=0.1,
            truncated=False,
        )
        assert _refusal(outcome) == "the real error"

    def test_silence_falls_back_to_the_runner_s_description(self) -> None:
        """A timeout kills the process before it says anything. Reporting an
        empty string as the reason is how a bug becomes unreportable."""
        outcome = Outcome(
            command=("python",),
            returncode=None,
            stdout="",
            stderr="",
            duration=90.0,
            truncated=False,
            error="it was killed after 90.0s",
        )
        assert _refusal(outcome) == outcome.describe()
        assert _refusal(outcome) != ""

    def test_a_refusal_is_not_available(self) -> None:
        assert not Refused("blog", "0001", "no").available

    def test_an_emission_is(self) -> None:
        assert Emitted("blog", "0001", "django.db.backends.postgresql").available

    def test_both_explain_themselves_with_the_migration_named(self) -> None:
        """A run renders many migrations; an unattributed reason is useless."""
        assert Refused("blog", "0001", "no server").explain() == "blog.0001: no server"
        emitted = Emitted(
            "blog", "0002", "django.db.backends.postgresql", (Statement("SELECT 1;"),)
        )
        assert emitted.explain() == "blog.0002: 1 statements via django.db.backends.postgresql"


@postgres
class TestAgainstARealProject:
    """End-to-end, through a real `manage.py`, at a real server.

    Nothing below asserts against a fixture. If Django changes the format,
    these fail and the fixtures above become the lie they were protecting.
    """

    def target(self, live_project: Path) -> Target:
        return Target(
            interpreter=interpreter(live_project / ".venv" / "bin" / "python"),
            manage_py=live_project / "manage.py",
            backend="django.db.backends.postgresql",
        )

    def test_it_renders_the_real_statements(self, live_project: Path) -> None:
        result = emit(self.target(live_project), "blog", "0002")
        assert result.available, result.explain()
        assert isinstance(result, Emitted)
        assert [s.sql for s in result.statements] == [
            'ALTER TABLE "blog_post" ADD COLUMN "body" text DEFAULT \'\' NOT NULL;',
            'ALTER TABLE "blog_post" ALTER COLUMN "body" DROP DEFAULT;',
            'CREATE INDEX "blog_title_idx" ON "blog_post" ("title");',
            'ALTER TABLE "blog_post" ALTER COLUMN "title" TYPE varchar(50);',
        ]

    def test_the_fixture_is_what_django_really_prints(self, live_project: Path) -> None:
        """The guard on every fixture-based test above."""
        result = emit(self.target(live_project), "blog", "0002")
        assert isinstance(result, Emitted)
        assert result.statements == parse(POSTGRES)[0]

    def test_an_unknown_migration_is_refused_with_django_s_reason(self, live_project: Path) -> None:
        result = emit(self.target(live_project), "blog", "0099")
        assert not result.available
        assert "0099" in result.explain()

    def test_an_unknown_app_is_refused(self, live_project: Path) -> None:
        result = emit(self.target(live_project), "nosuchapp", "0001")
        assert not result.available

    def test_run_python_is_reported_as_unsupported_not_as_empty(self, live_project: Path) -> None:
        result = emit(self.target(live_project), "blog", "0003")
        assert isinstance(result, Emitted)
        assert result.unsupported == ("Raw Python operation",)

    def test_a_non_default_alias_reaches_django_as_a_flag(self, live_project: Path) -> None:
        """`--database replica` is sent on the real command line, so this
        fails if the flag is misspelled rather than merely if it is absent --
        Django rejects an unrecognised argument outright."""
        target = self.target(live_project)._replace(database="replica")
        result = emit(target, "blog", "0002")
        assert result.available, result.explain()
        assert isinstance(result, Emitted)
        assert len(result.statements) == 4

    def test_and_an_alias_django_does_not_have_is_refused(self, live_project: Path) -> None:
        """The control: the flag is genuinely being read, not ignored."""
        target = self.target(live_project)._replace(database="nosuchalias")
        assert not emit(target, "blog", "0002").available

    def test_a_target_built_from_a_live_context_works(self, live_project: Path) -> None:
        """`Target.of` is the intended entry point, so it is exercised end to
        end rather than only unit-tested against a synthesised context."""
        from djaudit.live.context import inspect_target

        context = inspect_target(
            interpreter(live_project / ".venv" / "bin" / "python"), live_project / "manage.py"
        )
        assert isinstance(context, LiveContext), context
        target = Target.of(context, live_project / "manage.py")
        assert target is not None
        assert target.backend == "django.db.backends.postgresql"
        result = emit(target, "blog", "0002")
        assert result.available, result.explain()


def build_project(root: Path, dsn: str) -> Path:
    """A minimal Django project with its own virtualenv, wired to `dsn`."""
    import shutil
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    (root / "proj").mkdir(parents=True)
    (root / "blog" / "migrations").mkdir(parents=True)
    (root / "proj" / "__init__.py").write_text("")
    (root / "proj" / "settings.py").write_text(
        "SECRET_KEY = 'x'\n"
        "INSTALLED_APPS = ['blog']\n"
        "_PG = {'ENGINE': 'django.db.backends.postgresql',\n"
        f"    'NAME': {(parsed.path or '/postgres')[1:]!r},\n"
        f"    'USER': {(parsed.username or '')!r},\n"
        f"    'HOST': {(parsed.hostname or '')!r},\n"
        f"    'PORT': {str(parsed.port or '')!r}}}\n"
        "DATABASES = {'default': _PG, 'replica': dict(_PG)}\n"
        "USE_TZ = True\n"
    )
    (root / "blog" / "__init__.py").write_text("")
    (root / "blog" / "models.py").write_text(
        "from django.db import models\n\n\n"
        "class Post(models.Model):\n"
        "    title = models.CharField(max_length=50)\n"
        "    body = models.TextField(default='')\n"
    )
    (root / "blog" / "migrations" / "__init__.py").write_text("")
    (root / "blog" / "migrations" / "0001_initial.py").write_text(
        "from django.db import migrations, models\n\n\n"
        "class Migration(migrations.Migration):\n"
        "    initial = True\n"
        "    dependencies = []\n"
        "    operations = [migrations.CreateModel(name='Post', fields=[\n"
        "        ('id', models.AutoField(primary_key=True, serialize=False)),\n"
        "        ('title', models.CharField(max_length=200))])]\n"
    )
    (root / "blog" / "migrations" / "0002_add_body.py").write_text(
        "from django.db import migrations, models\n\n\n"
        "class Migration(migrations.Migration):\n"
        "    dependencies = [('blog', '0001_initial')]\n"
        "    operations = [\n"
        "        migrations.AddField('post', 'body', models.TextField(default='')),\n"
        "        migrations.AddIndex('post', models.Index(fields=['title'],\n"
        "            name='blog_title_idx')),\n"
        "        migrations.AlterField('post', 'title', models.CharField(max_length=50))]\n"
    )
    (root / "blog" / "migrations" / "0003_data.py").write_text(
        "from django.db import migrations\n\n\n"
        "def forwards(apps, schema_editor):\n"
        "    pass\n\n\n"
        "class Migration(migrations.Migration):\n"
        "    atomic = False\n"
        "    dependencies = [('blog', '0002_add_body')]\n"
        "    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]\n"
    )
    (root / "manage.py").write_text(
        "#!/usr/bin/env python\nimport os, sys\n\n"
        "if __name__ == '__main__':\n"
        "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proj.settings')\n"
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n"
    )

    venv = root / ".venv"
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to build the target's virtualenv"
    subprocess.run([uv, "venv", str(venv), "-q"], check=True, timeout=300)
    subprocess.run(
        [uv, "pip", "install", "-q", "--python", str(venv / "bin" / "python"), "django", "psycopg"],
        check=True,
        timeout=900,
    )
    return root
