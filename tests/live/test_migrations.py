"""Tests for reading which migrations have not been applied.

`PLAN` and `VERBOSE` are verbatim `showmigrations --plan` output from a real
project against a real server, and the format claim is additionally checked
against Django's own source, which writes::

    self.stdout.write("[X]  %s.%s%s" % (node.key[0], node.key[1], deps))
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from djaudit.live.migrations import EMPTY, Plan, Unknown, parse_plan, read_plan
from djaudit.live.runner import Outcome
from djaudit.live.sqlmigrate import Target

from .test_sqlmigrate import interpreter

SQLITE = "django.db.backends.sqlite3"
DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")

PLAN = """[X]  contenttypes.0001_initial
[X]  auth.0001_initial
[X]  blog.0001_initial
[X]  blog.0002_add_body
[ ]  blog.0003_data
"""

VERBOSE = """[X]  blog.0001_initial
[X]  blog.0002_add_body ... (blog.0001_initial)
[ ]  blog.0003_data ... (blog.0002_add_body)
"""


class TestReadingThePlan:
    def test_applied_and_unapplied_are_separated(self) -> None:
        applied, unapplied = parse_plan(PLAN)
        assert unapplied == (("blog", "0003_data"),)
        assert ("blog", "0002_add_body") in applied

    def test_every_app_is_read_not_only_the_first(self) -> None:
        applied, _ = parse_plan(PLAN)
        assert {app for app, _ in applied} == {"contenttypes", "auth", "blog"}

    def test_the_verbosity_two_dependency_suffix_is_not_part_of_the_name(self) -> None:
        """Measured: `--verbosity 2` appends ` ... (blog.0001_initial)`. The
        command is run at verbosity 1 so it cannot appear, but a caller passing
        `extra_environment` cannot know that nothing raises the default."""
        applied, unapplied = parse_plan(VERBOSE)
        assert unapplied == (("blog", "0003_data"),)
        assert ("blog", "0002_add_body") in applied

    def test_order_is_kept_because_it_is_the_order_they_will_run(self) -> None:
        """The plan is topological. A set would discard the answer to which of
        these takes the table first."""
        _, unapplied = parse_plan("[ ]  a.0001\n[ ]  b.0001\n[ ]  a.0002\n")
        assert unapplied == (("a", "0001"), ("b", "0001"), ("a", "0002"))

    def test_the_empty_marker_is_not_read_as_a_migration(self) -> None:
        assert parse_plan(EMPTY) == (frozenset(), ())

    def test_a_line_that_is_not_a_marker_is_skipped(self) -> None:
        """Warnings and `System check identified no issues` share the stream."""
        applied, unapplied = parse_plan("some warning\n[ ]  a.0001\n")
        assert unapplied == (("a", "0001"),)
        assert applied == frozenset()

    def test_a_name_with_no_app_is_skipped_rather_than_half_read(self) -> None:
        assert parse_plan("[ ]  nodot\n") == (frozenset(), ())

    def test_nothing_parses_to_nothing(self) -> None:
        assert parse_plan("") == (frozenset(), ())


class TestWhatAPlanReports:
    def test_it_counts_both_halves(self) -> None:
        plan = Plan(frozenset({("a", "1")}), (("a", "2"),))
        assert plan.total == 2
        assert plan.explain() == "1 of 2 migrations have not been applied"

    def test_a_plan_is_available(self) -> None:
        assert Plan(frozenset(), ()).available

    def test_an_unknown_is_not(self) -> None:
        assert not Unknown("no server").available

    def test_an_unknown_reports_nothing_pending_rather_than_everything(self) -> None:
        """A caller that ignores `available` must under-report, not over-report:
        the alternative is naming every migration in the project as pending."""
        assert Unknown("no server").unapplied == ()

    def test_an_unknown_says_why(self) -> None:
        assert Unknown("no server").explain() == "migration state is unknown: no server"


class TestAskingTheTarget:
    def target(self, tmp_path: Path) -> Target:
        return Target(
            interpreter=interpreter(),
            manage_py=tmp_path / "manage.py",
            backend="django.db.backends.postgresql",
        )

    def test_a_failure_is_carried_not_raised(self, tmp_path: Path, monkeypatch) -> None:
        def failed(*_args: object, **_kwargs: object) -> Outcome:
            return Outcome(
                command=("python",),
                returncode=1,
                stdout="",
                stderr="django.db.utils.OperationalError: connection refused",
                duration=0.1,
                truncated=False,
            )

        monkeypatch.setattr("djaudit.live.migrations.run_python", failed)
        result = read_plan(self.target(tmp_path))
        assert not result.available
        assert "connection refused" in result.explain()

    def test_an_empty_answer_is_unknown_not_a_clean_project(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`showmigrations` naming nothing means we failed to read it, not that
        the project has no migrations to worry about. Reporting a `Plan` here
        would tell every caller that nothing is pending."""

        def silent(*_args: object, **_kwargs: object) -> Outcome:
            return Outcome(
                command=("python",),
                returncode=0,
                stdout="",
                stderr="",
                duration=0.1,
                truncated=False,
            )

        monkeypatch.setattr("djaudit.live.migrations.run_python", silent)
        assert not read_plan(self.target(tmp_path)).available


@postgres
class TestAgainstARealProject:
    def target(self, live_project: Path) -> Target:
        return Target(
            interpreter=interpreter(live_project / ".venv" / "bin" / "python"),
            manage_py=live_project / "manage.py",
            backend="django.db.backends.postgresql",
        )

    def test_it_reads_the_real_plan(self, live_project: Path) -> None:
        result = read_plan(self.target(live_project))
        assert result.available, result.explain()
        assert isinstance(result, Plan)
        assert ("blog", "0003_data") in result.unapplied

    def test_migrations_are_not_applied_before_migrate_runs(self, live_project: Path) -> None:
        """The project is built fresh, so every one of its own migrations is
        pending. This is the state DJM-010 is designed to report on."""
        result = read_plan(self.target(live_project))
        assert isinstance(result, Plan)
        blog = [name for app, name in result.unapplied if app == "blog"]
        assert blog == ["0001_initial", "0002_add_body", "0003_data"]

    def test_the_format_is_still_django_s(self, live_project: Path) -> None:
        """The guard on the fixtures above: every line the real command emits
        is a marker line, so nothing is being silently skipped."""
        from djaudit.live.runner import run_python

        outcome = run_python(
            self.target(live_project).interpreter,
            ["manage.py", "showmigrations", "--plan", "--verbosity", "1"],
            cwd=live_project,
        )
        assert outcome.ok, outcome.describe()
        lines = [line for line in outcome.stdout.splitlines() if line.strip()]
        assert lines
        assert all(line.startswith(("[X]  ", "[ ]  ")) for line in lines), lines


class TestOutputThatIsNotAPlan:
    """Every line the parser is handed is not necessarily a migration.

    Mutation testing blanked the `"[ ]"` prefix, making the unapplied branch
    match every line, and no test noticed: they all fed clean, marker-only
    output. A parser is defined as much by what it refuses as by what it reads.
    """

    def test_a_line_with_no_marker_is_not_a_migration(self) -> None:
        applied, unapplied = parse_plan("System check identified no issues.")
        assert (applied, unapplied) == (frozenset(), ())

    def test_the_empty_plan_line_is_not_a_migration(self) -> None:
        assert parse_plan(EMPTY) == (frozenset(), ())

    def test_a_line_shaped_like_a_migration_but_unmarked_is_refused(self) -> None:
        """The other refusals here pass through a backstop rather than the
        marker check: `parse_plan` also drops a key whose app or name is empty,
        and stray prose usually has no dot in the right place. This line does.
        Trim three characters off `>>> blog.0009_hotfix` and what is left is a
        well-formed migration key, so only the marker can reject it.
        """
        assert parse_plan(">>> blog.0009_hotfix") == (frozenset(), ())

    def test_blank_lines_are_not_migrations(self) -> None:
        assert parse_plan("\n\n   \n") == (frozenset(), ())

    def test_a_warning_beside_real_migrations_is_dropped(self) -> None:
        """The control. The three above would also pass against a parser that
        returned nothing at all, so one shape has to survive the same call."""
        applied, unapplied = parse_plan(
            "WARNING: something\n[X]  blog.0001_initial\nnot a migration\n[ ]  blog.0002_add_body"
        )
        assert applied == {("blog", "0001_initial")}
        assert unapplied == (("blog", "0002_add_body"),)


class TestNothingToReport:
    """A project with no migrations, and output nobody could read, both parse to
    nothing. They are not the same answer and must not share one."""

    @staticmethod
    def bare_project(tmp_path: Path) -> Path:
        """A Django project with `INSTALLED_APPS = []`, which is the only way to
        make the real command print `(no migrations)` -- `contenttypes` and
        `auth` each ship their own, so any ordinary project has some."""
        root = tmp_path / "bare"
        root.mkdir()
        (root / "settings.py").write_text(
            'SECRET_KEY = "x"\n'
            "INSTALLED_APPS = []\n"
            'DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",'
            ' "NAME": str(__import__("pathlib").Path(__file__).parent / "db.sqlite3")}}\n'
            "USE_TZ = True\n"
        )
        (root / "manage.py").write_text(
            "import os, sys\n"
            'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")\n'
            "from django.core.management import execute_from_command_line\n"
            "execute_from_command_line(sys.argv)\n"
        )
        return root

    def test_a_project_with_no_migrations_is_a_plan_not_a_failure(self, tmp_path: Path) -> None:
        """Measured: the real command prints `(no migrations)` and exits 0. That
        is a complete answer -- there is nothing pending -- and calling it
        `Unknown` would report a fully-migrated project as unreadable."""
        root = self.bare_project(tmp_path)
        result = read_plan(Target(interpreter(), root / "manage.py", SQLITE))
        assert result.available, result.explain()
        assert isinstance(result, Plan)
        assert result.unapplied == ()
        assert result.applied == frozenset()

    def test_output_that_names_nothing_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contrast. Same empty parse, opposite verdict, because the marker
        Django prints for a real empty plan is absent."""
        import djaudit.live.migrations as module

        monkeypatch.setattr(
            module,
            "run_python",
            lambda *a, **k: Outcome(("showmigrations",), 0, "unexpected chatter", "", 0.1, False),
        )
        result = read_plan(Target(interpreter(), Path("/nonexistent/manage.py"), SQLITE))
        assert not result.available
        assert isinstance(result, Unknown)
        assert result.why == "`showmigrations --plan` named no migrations"


class TestTheDatabaseAliasReachesTheCommand:
    """`--database` was blanked by mutation and nothing failed, because every
    test used the default alias. A rule that reads the wrong connection reports
    the wrong migration state, so the flag has to be shown arriving."""

    def test_an_unknown_alias_is_rejected_by_name(self, live_project: Path) -> None:
        result = read_plan(
            Target(interpreter(), live_project / "manage.py", SQLITE, database="warehouse")
        )
        assert not result.available
        # Django resolves the alias, so its complaint names it. Dropping the
        # flag would instead make `warehouse` a positional argument and produce
        # an argparse error -- a different message, which is the point.
        assert "warehouse" in result.explain()
        assert "unrecognized arguments" not in result.explain()

    def test_the_default_alias_is_accepted(self, live_project: Path) -> None:
        """The control: the same project answers when the alias is real."""
        result = read_plan(
            Target(interpreter(), live_project / "manage.py", SQLITE, database="default")
        )
        assert result.available, result.explain()
