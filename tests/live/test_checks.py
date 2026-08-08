"""Django's deployment check, read the way Django actually writes it.

Every claim about the output format here was measured against Django 6.0 before
it was coded, because the format is designed for a terminal and none of the
convenient assumptions about it hold: issues go to stderr and the all-clear
goes to stdout, warnings exit 0, and a project that cannot be imported prints a
traceback that parses as nothing at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from djaudit.live.checks import (
    SECTIONS,
    Message,
    Report,
    Unknown,
    parse_report,
    run_deployment_check,
)
from djaudit.live.sqlmigrate import Target
from djaudit.models import Severity

from .test_sqlmigrate import interpreter

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")

POSTGRES = "django.db.backends.postgresql"

REAL = """\
System check identified some issues:

WARNINGS:
?: (security.W001) You do not have 'django.middleware.security.SecurityMiddleware' in your \
MIDDLEWARE so the SECURE_HSTS_SECONDS setting will have no effect.
?: (security.W009) Your SECRET_KEY has less than 50 characters.
?: (security.W020) ALLOWED_HOSTS must not be empty in deployment.

System check identified 3 issues (0 silenced).
"""
"""Copied from a real run against Django 6.0, trimmed only in message length."""


class TestReadingTheReport:
    def test_each_warning_becomes_a_message(self) -> None:
        report = parse_report(REAL)
        assert report.available
        assert report.ids() == {"security.W001", "security.W009", "security.W020"}

    def test_a_warning_is_medium(self) -> None:
        message = parse_report(REAL).by_id("security.W009")
        assert message is not None
        assert message.severity is Severity.MEDIUM

    def test_the_message_text_survives_its_colons(self) -> None:
        """`obj` stops at the first `": "`, and the rest of the line is the
        message however many more it contains."""
        message = parse_report(REAL).by_id("security.W001")
        assert message is not None
        assert message.text.startswith("You do not have")
        assert "SECURE_HSTS_SECONDS setting will have no effect." in message.text

    def test_a_settings_check_names_no_object(self) -> None:
        message = parse_report(REAL).by_id("security.W020")
        assert message is not None
        assert message.about_nothing
        assert message.obj == "?"

    def test_nothing_is_silenced_here(self) -> None:
        assert parse_report(REAL).silenced == 0


class TestTheSectionsCarryTheSeverity:
    """Django sorts into five fixed sections and the section is the only thing
    that says how bad a message is -- the line itself does not repeat it."""

    @staticmethod
    def one(section: str) -> Message:
        text = (
            f"System check identified some issues:\n\n{section}:\n"
            "?: (x.001) something\n\nSystem check identified 1 issue (0 silenced).\n"
        )
        report = parse_report(text)
        assert report.available, report.explain()
        (message,) = report.messages
        return message

    @pytest.mark.parametrize(("section", "severity"), list(SECTIONS.items()))
    def test_each_section_maps(self, section: str, severity: Severity) -> None:
        assert self.one(section).severity is severity

    def test_the_section_names_are_djangos(self) -> None:
        """The parametrized test above builds its input out of `SECTIONS`, so
        it cannot see a wrong name -- it would ask for `GRUMBLES:` and be told
        `GRUMBLES` maps. These are the five literal headings written by
        `BaseCommand.check`, in `django/core/management/base.py`."""
        assert set(SECTIONS) == {"CRITICALS", "ERRORS", "WARNINGS", "INFOS", "DEBUGS"}

    def test_they_are_not_all_the_same(self) -> None:
        """The control for the table above: a mapping that collapsed every
        section onto one severity would satisfy each row of it."""
        assert len(set(SECTIONS.values())) == len(SECTIONS)

    def test_an_error_outranks_a_warning(self) -> None:
        assert self.one("ERRORS").severity.rank > self.one("WARNINGS").severity.rank

    def test_a_line_before_any_section_is_not_a_message(self) -> None:
        """The header is `obj: msg` shaped and would parse as one."""
        report = parse_report(
            "System check identified some issues:\n\nWARNINGS:\n"
            "?: (x.001) real\n\nSystem check identified 1 issue (0 silenced).\n"
        )
        assert len(report.messages) == 1

    def test_an_unknown_section_name_is_not_a_section(self) -> None:
        text = (
            "System check identified some issues:\n\nGRUMBLES:\n"
            "?: (x.001) something\n\nSystem check identified 1 issue (0 silenced).\n"
        )
        assert parse_report(text).messages == ()


class TestDescribingAMessage:
    """What a finding will quote when it cites Django."""

    def test_it_names_the_check_and_what_it_said(self) -> None:
        message = parse_report(REAL).by_id("security.W009")
        assert message is not None
        assert message.describe() == ("security.W009: Your SECRET_KEY has less than 50 characters.")

    def test_a_check_with_no_id_says_so(self) -> None:
        """Rather than `None: ...`, which reads as a bug in us."""
        (message,) = parse_report(TestAnIdIsOptional.IDLESS).messages
        assert message.describe() == ("no id: something is wrong (and parenthesised, at that)")


class TestHints:
    HINTED = (
        "System check identified some issues:\n\nERRORS:\n"
        "blog.Post.a: (fields.E304) Reverse accessor clashes.\n"
        "\tHINT: Add or change a related_name argument.\n"
        "\nSystem check identified 1 issue (0 silenced).\n"
    )

    def test_a_hint_attaches_to_its_message(self) -> None:
        (message,) = parse_report(self.HINTED).messages
        assert message.hint == "Add or change a related_name argument."

    def test_and_is_not_a_message_of_its_own(self) -> None:
        """A tab-indented continuation parses as `obj: msg` if nothing stops
        it, which would double every hinted finding."""
        assert len(parse_report(self.HINTED).messages) == 1

    def test_a_message_without_one_has_none(self) -> None:
        assert parse_report(REAL).messages[0].hint is None

    def test_the_object_is_the_model_field(self) -> None:
        (message,) = parse_report(self.HINTED).messages
        assert message.obj == "blog.Post.a"
        assert not message.about_nothing

    def test_a_hint_before_any_message_is_dropped(self) -> None:
        """Django never emits this, so the only thing to get right is not
        raising `IndexError` if it ever does."""
        text = (
            "System check identified some issues:\n\nERRORS:\n"
            "\tHINT: orphaned.\n\nSystem check identified 1 issue (0 silenced).\n"
        )
        assert parse_report(text).messages == ()


class TestAnIdIsOptional:
    """`CheckMessage.__str__` writes `"(%s) " % self.id if self.id else ""`, so
    a third-party check with no id produces a line with no parenthesis."""

    IDLESS = (
        "System check identified some issues:\n\nWARNINGS:\n"
        "?: something is wrong (and parenthesised, at that)\n"
        "\nSystem check identified 1 issue (0 silenced).\n"
    )

    def test_it_still_becomes_a_message(self) -> None:
        (message,) = parse_report(self.IDLESS).messages
        assert message.id is None

    def test_and_keeps_its_whole_text(self) -> None:
        (message,) = parse_report(self.IDLESS).messages
        assert message.text == "something is wrong (and parenthesised, at that)"

    def test_an_idless_message_is_not_matched_by_id(self) -> None:
        assert parse_report(self.IDLESS).by_id("security.W009") is None

    def test_and_is_absent_from_the_id_set(self) -> None:
        assert parse_report(self.IDLESS).ids() == frozenset()


class TestSilencing:
    """`SILENCED_SYSTEM_CHECKS` removes the message and leaves only a count, so
    a project can quiet its deployment check without quieting the risk. Our own
    `DJS` rules read the settings source and still see it."""

    def test_the_count_is_read(self) -> None:
        text = (
            "System check identified some issues:\n\nWARNINGS:\n"
            "?: (security.W020) ALLOWED_HOSTS must not be empty in deployment.\n"
            "\nSystem check identified 1 issue (2 silenced).\n"
        )
        report = parse_report(text)
        assert report.silenced == 2
        assert report.ids() == {"security.W020"}

    def test_a_silenced_check_leaves_no_trace_but_the_number(self) -> None:
        """Which is the whole limitation: there is no id to report."""
        text = "System check identified no issues (3 silenced).\n"
        report = parse_report(text)
        assert report.available
        assert report.messages == ()
        assert report.silenced == 3


class TestNothingIsMistakenForSuccess:
    def test_a_traceback_is_not_a_clean_bill_of_health(self) -> None:
        """The failure that motivates trusting the footer. A project that
        cannot be imported prints no sections and no summary, and scoring that
        as zero findings would report a broken project as a safe one."""
        text = (
            "Traceback (most recent call last):\n"
            '  File "/p/manage.py", line 4, in <module>\n'
            "django.core.exceptions.ImproperlyConfigured: settings are not configured\n"
        )
        report = parse_report(text)
        assert not report.available
        assert report.messages == ()

    def test_and_says_so(self) -> None:
        assert parse_report("boom\n").explain() == (
            "the deployment check did not report: no summary line, "
            "so the command did not finish a check"
        )

    def test_the_control_is_that_a_summary_makes_it_a_report(self) -> None:
        """Same absence of findings, one line different, opposite meaning."""
        report = parse_report("System check identified no issues (0 silenced).\n")
        assert report.available
        assert report.messages == ()
        assert report.explain() == "0 checks reported, 0 silenced"

    def test_a_command_that_said_nothing_at_all_still_explains_itself(self) -> None:
        """`_refusal` has nothing to quote, so it reports the only fact it
        has. An empty string here would produce `did not report: ` and leave
        the reader with a sentence that stops."""
        from djaudit.live.checks import _refusal
        from djaudit.live.runner import Outcome

        assert _refusal(Outcome(("python",), 3, "", "   \n", 0.1, False)) == (
            "exit code 3 and no output"
        )

    def test_and_quotes_the_last_line_when_there_is_one(self) -> None:
        from djaudit.live.checks import _refusal
        from djaudit.live.runner import Outcome

        outcome = Outcome(("python",), 1, "", "Traceback\nImproperlyConfigured: no\n", 0.1, False)
        assert _refusal(outcome) == "ImproperlyConfigured: no"

    def test_it_falls_back_to_stdout_when_stderr_is_empty(self) -> None:
        from djaudit.live.checks import _refusal
        from djaudit.live.runner import Outcome

        assert _refusal(Outcome(("python",), 1, "said on stdout\n", "", 0.1, False)) == (
            "said on stdout"
        )

    def test_an_unreadable_run_answers_nothing_to_every_question(self) -> None:
        unknown = Unknown("no interpreter")
        assert unknown.by_id("security.W009") is None
        assert unknown.ids() == frozenset()
        assert unknown.messages == ()
        assert unknown.silenced == 0

    def test_a_report_that_did_run_answers_for_real(self) -> None:
        report = parse_report(REAL)
        assert isinstance(report, Report)
        assert report.by_id("security.W009") is not None
        assert report.explain() == "3 checks reported, 0 silenced"

    def test_a_single_issue_is_counted_in_the_singular(self) -> None:
        """Django writes `1 issue`, not `1 issues`, and a pattern that only
        knows the plural reads a one-finding project as a crashed one."""
        assert parse_report("System check identified 1 issue (0 silenced).\n").available

    def test_the_plural_is_read_too(self) -> None:
        assert parse_report("System check identified 4 issues (0 silenced).\n").available

    def test_as_is_the_empty_case(self) -> None:
        assert parse_report("System check identified no issues (0 silenced).\n").available


@postgres
class TestAgainstRealDjango:
    """The format is Django's, so the parser is checked against Django."""

    @staticmethod
    def target(project: Path) -> Target:
        return Target(
            interpreter(project / ".venv" / "bin" / "python"),
            project / "manage.py",
            POSTGRES,
        )

    def test_it_reads_a_real_deployment_check(self, live_project: Path) -> None:
        report = run_deployment_check(self.target(live_project))
        assert report.available, report.explain()
        assert "security.W009" in report.ids(), report.ids()

    def test_the_findings_arrive_on_stderr_and_are_still_read(self, live_project: Path) -> None:
        """Django writes the report to stderr when there are issues and to
        stdout when there are none. Both streams are joined before parsing
        precisely because neither one alone is the whole answer."""
        done = subprocess.run(
            [
                str(live_project / ".venv" / "bin" / "python"),
                "manage.py",
                "check",
                "--deploy",
                "--no-color",
            ],
            cwd=live_project,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert done.stdout == ""
        assert "security.W009" in done.stderr
        assert done.returncode == 0, "warnings exit 0, which is why we do not read the code"

    def test_a_silenced_check_disappears_from_a_real_run(self, live_project: Path) -> None:
        """Measured rather than asserted: the id is gone and the count moves."""
        before = run_deployment_check(self.target(live_project))
        assert "security.W009" in before.ids()

        settings = live_project / "proj" / "settings.py"
        settings.write_text(settings.read_text() + "SILENCED_SYSTEM_CHECKS = ['security.W009']\n")
        after = run_deployment_check(self.target(live_project))

        assert "security.W009" not in after.ids()
        assert after.silenced == 1
        assert before.silenced == 0

    def test_an_error_level_check_exits_nonzero_and_is_still_read(self, live_project: Path) -> None:
        """The exit code answers a different question than ours. A model error
        makes `check` exit 1 while printing a complete report -- and that is
        precisely the run whose report matters most. Treating a non-zero exit
        as "no report" would discard it."""
        (live_project / "blog" / "models.py").write_text(
            "from django.db import models\n\n\n"
            "class Author(models.Model):\n"
            "    name = models.CharField(max_length=10)\n\n\n"
            "class Post(models.Model):\n"
            "    a = models.ForeignKey(Author, models.CASCADE, related_name='x')\n"
            "    b = models.ForeignKey(Author, models.CASCADE, related_name='x')\n"
        )
        done = subprocess.run(
            [
                str(live_project / ".venv" / "bin" / "python"),
                "manage.py",
                "check",
                "--deploy",
                "--no-color",
            ],
            cwd=live_project,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert done.returncode == 1, "the premise: an error-level check fails the command"

        report = run_deployment_check(self.target(live_project))
        assert report.available, report.explain()

        clash = report.by_id("fields.E304")
        assert clash is not None
        assert clash.severity is Severity.HIGH
        assert clash.hint is not None, "a real hint, tab-indented by real Django"
        assert "security.W009" in report.ids(), "and the warnings are still there too"

    def test_a_project_that_cannot_be_imported_is_unknown(self, live_project: Path) -> None:
        """A real traceback from real Django, not a handwritten one."""
        (live_project / "proj" / "settings.py").write_text("import nonexistent_module\n")
        report = run_deployment_check(self.target(live_project))
        assert not report.available
        assert "nonexistent_module" in report.explain()
