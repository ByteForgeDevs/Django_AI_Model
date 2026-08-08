"""Whether `--live` says what it is about to do, before it does it.

The live tier executes the audited project's code. Every test here is about the
gap between that fact and the reader's knowledge of it: is it off by default, is
it disclosed, is the disclosure early enough to act on, and does asking for it
and not getting it leave the audit in a state that pretends otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from djaudit import engine
from djaudit.discovery import build_context
from djaudit.live.consent import Consent, notice, resolve
from djaudit.live.interpreter import Interpreter, Rejection, Search
from tests.live.test_context import MANAGE, SETTINGS, make_project

SENTINEL = "djaudit-executed-this-project"


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real project with a real virtualenv of its own.

    A genuine `pyvenv.cfg` under the target, because `find_interpreter` reads
    one and a directory merely shaped like a virtualenv would not exercise it.
    The interpreter is still the target's own, which is the property the live
    tier turns on.

    Django is installed rather than inherited. It used to be inherited through
    `system_site_packages`, which is free and works on a machine whose system
    Python happens to have Django -- and silently produces a target that cannot
    start Django at all on one that does not. Every failure it caused named a
    missing module rather than the missing install, and CI stayed red for five
    commits while the same tests passed locally.
    """
    import venv

    root = tmp_path_factory.mktemp("consent")
    make_project(root, settings=SETTINGS + f"\nMARKER = {SENTINEL!r}\n", manage=MANAGE)
    venv.EnvBuilder(with_pip=False, symlinks=True, system_site_packages=True).create(root / ".venv")

    uv = shutil.which("uv")
    assert uv is not None, "uv is required to give the target its Django"
    subprocess.run(
        [uv, "pip", "install", "-q", "--python", str(root / ".venv" / "bin" / "python"), "django"],
        check=True,
        timeout=900,
    )
    return root / "manage.py"


class TestItIsOffUntilAsked:
    def test_no_consent_resolves_to_nothing(self, tmp_path: Path) -> None:
        assert resolve(tmp_path, tmp_path / "manage.py", granted=False) == Consent(granted=False)

    def test_it_does_not_even_look_for_an_interpreter(self, tmp_path: Path) -> None:
        """The control for the test above: with consent it would have searched
        and reported a problem, so the empty result means it stopped early."""
        without = resolve(tmp_path, tmp_path / "manage.py", granted=False)
        with_consent = resolve(tmp_path, tmp_path / "manage.py", granted=True)
        assert without.problem is None
        assert with_consent.problem is not None

    def test_nothing_is_executed_without_consent(self, project: Path) -> None:
        executed: list[str] = []
        resolve(project.parent, project, granted=False, announce=executed.append)
        assert executed == []


class TestTheDisclosure:
    def test_it_names_both_paths(self) -> None:
        """Asserted whole rather than by substring: two paths are both "in" a
        string that has run them together into `/venv/bin/python/proj/manage.py`,
        and a reader cannot check a path that does not exist."""
        assert notice(Path("/venv/bin/python"), Path("/proj/manage.py")) == (
            "live tier enabled: djaudit will execute the target's own code.\n"
            "  interpreter  /venv/bin/python\n"
            "  entry point  /proj/manage.py\n"
            "  Its settings module and everything that module imports will be "
            "imported and run.\n"
            "  Run --no-live if this project is not one you trust."
        )

    def test_it_says_code_will_be_executed(self) -> None:
        assert "execute the target's own code" in notice(Path("a"), Path("b"))

    def test_it_names_the_way_out(self) -> None:
        assert "--no-live" in notice(Path("a"), Path("b"))

    def test_it_is_printed_when_consent_is_given(self, project: Path) -> None:
        announced: list[str] = []
        resolve(project.parent, project, granted=True, announce=announced.append)
        assert len(announced) == 1
        assert "will execute the target's own code" in announced[0]

    def test_it_arrives_before_the_execution_it_discloses(self, project: Path) -> None:
        """A disclosure printed after the code has run is a changelog. The
        marker file is written by the target on import, so its absence at
        announce time is the ordering."""
        order: list[str] = []
        marker = project.parent / "disclosed.marker"
        marker.unlink(missing_ok=True)

        def announce(_: str) -> None:
            order.append("ran" if marker.exists() else "announced")

        settings = project.parent / "proj" / "settings.py"
        original = settings.read_text()
        settings.write_text(original + f"\nopen({str(marker)!r}, 'w').write('x')\n")
        try:
            outcome = resolve(project.parent, project, granted=True, announce=announce)
        finally:
            settings.write_text(original)

        assert order == ["announced"]
        assert marker.exists(), "the control: the target really did run afterwards"
        assert outcome.available


class TestWhenTheLiveTierCannotStart:
    def test_a_missing_manage_py_is_named(self, tmp_path: Path) -> None:
        outcome = resolve(tmp_path, None, granted=True)
        assert outcome.problem == "no manage.py was found in the target"

    def test_no_virtualenv_is_named(self, tmp_path: Path) -> None:
        (tmp_path / "manage.py").write_text(MANAGE)
        outcome = resolve(tmp_path, tmp_path / "manage.py", granted=True)
        assert outcome.problem is not None
        assert "virtualenv" in outcome.problem
        # Still a request that was made. Reporting it as never made would tell
        # the reader to pass the flag they just passed.
        assert outcome.reason().startswith("the live tier was requested but")

    def test_every_refused_candidate_is_listed(self) -> None:
        """Two, because a separator between one item is never rendered."""
        from djaudit.live.consent import _no_interpreter

        search = Search(
            None,
            (
                Rejection(Path("/t/.venv"), "it is djaudit's own"),
                Rejection(Path("/t/env"), "no pyvenv.cfg"),
            ),
        )
        assert _no_interpreter(search) == (
            "no usable virtualenv was found in the target "
            "(/t/.venv: it is djaudit's own; /t/env: no pyvenv.cfg)"
        )

    def test_a_refused_candidate_is_quoted_not_swallowed(self) -> None:
        """`find_interpreter` declines djaudit's own environment on purpose, so
        "no virtualenv" while the reader is looking straight at one is the least
        useful true thing available."""
        from djaudit.live.consent import _no_interpreter

        search = Search(None, (Rejection(Path("/t/.venv"), "it is djaudit's own"),))
        assert _no_interpreter(search) == (
            "no usable virtualenv was found in the target (/t/.venv: it is djaudit's own)"
        )

    def test_nothing_found_and_nothing_refused_reads_plainly(self) -> None:
        from djaudit.live.consent import _no_interpreter

        assert _no_interpreter(Search(None)) == "no virtualenv was found in the target"

    def test_a_target_whose_django_will_not_start_is_quoted(self, project: Path) -> None:
        settings = project.parent / "proj" / "settings.py"
        original = settings.read_text()
        settings.write_text(original + "\nimport nonexistent_module_xyz\n")
        try:
            outcome = resolve(project.parent, project, granted=True)
        finally:
            settings.write_text(original)
        assert outcome.problem is not None
        assert "nonexistent_module_xyz" in outcome.problem
        assert outcome.reason().startswith("the live tier was requested but")

    def test_it_is_still_a_granted_consent(self, tmp_path: Path) -> None:
        """Granted and unavailable are different facts: the reader asked, so
        telling them to pass the flag they passed is the wrong instruction."""
        outcome = resolve(tmp_path, None, granted=True)
        assert outcome.granted
        assert not outcome.available


class TestTheReasonItReports:
    def test_not_requested(self) -> None:
        assert Consent(granted=False).reason() == "the live tier was not requested"

    def test_requested_and_failed_says_which(self) -> None:
        assert Consent(granted=True, problem="no manage.py").reason() == (
            "the live tier was requested but is unavailable: no manage.py"
        )

    def test_it_ran(self, project: Path) -> None:
        assert resolve(project.parent, project, granted=True).reason() == "the live tier ran"


class TestItAsksTheTargetAndUsesTheAnswer:
    def test_the_context_comes_back(self, project: Path) -> None:
        outcome = resolve(project.parent, project, granted=True)
        assert outcome.context is not None
        assert outcome.context.settings_module == "proj.settings"

    def test_it_is_the_targets_interpreter_not_ours(self, project: Path) -> None:
        import sys

        outcome = resolve(project.parent, project, granted=True)
        assert outcome.context is not None
        interpreter: Interpreter = outcome.context.interpreter
        assert interpreter.executable != Path(sys.executable)
        assert str(interpreter.executable).startswith(str(project.parent))

    def test_its_django_comes_from_its_own_virtualenv(self, project: Path) -> None:
        """The fixture inherits system site-packages, so on a machine whose
        system Python has Django this passes without anything being installed
        -- and the same fixture cannot start Django at all on a runner that
        does not. Asserting the *path* is what tells those two apart; asserting
        that Django merely imports cannot.
        """
        found = subprocess.run(
            [
                str(project.parent / ".venv" / "bin" / "python"),
                "-c",
                "import django; print(django.__file__)",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        assert found.stdout.strip().startswith(str(project.parent))


class TestTheAuditDoesNotPretend:
    """Asking for the live tier and not getting one must not leave live rules
    selected with nothing live behind them."""

    def test_a_failed_request_still_reports_as_requested(self, tmp_path: Path) -> None:
        from dataclasses import replace

        ctx = replace(build_context(tmp_path), live_problem="no virtualenv was found")
        result = engine.run(tmp_path, context=ctx)
        assert result.degraded is not None
        assert result.degraded.reason == ("the live tier was requested but no virtualenv was found")

    def test_a_request_never_made_reads_differently(self, tmp_path: Path) -> None:
        result = engine.run(tmp_path, context=build_context(tmp_path))
        assert result.degraded is not None
        assert result.degraded.reason == "the live tier was not requested"

    def test_a_failed_request_does_not_select_live_rules(self, tmp_path: Path) -> None:
        """The defect this guards: `--live` used to set the tier set from the
        flag, so a project with no virtualenv still selected every live rule."""
        from dataclasses import replace

        from djaudit.models import Tier
        from djaudit.registry import all_rules

        ctx = replace(build_context(tmp_path), live_problem="no virtualenv")
        result = engine.run(tmp_path, context=ctx)
        static_only = sum(1 for r in all_rules() if r.meta.tier is Tier.STATIC)
        assert result.rules_run == static_only


class TestThroughTheCommandLine:
    @staticmethod
    def invoke(target: Path, *flags: str) -> tuple[int, str, str]:
        from typer.testing import CliRunner

        from djaudit.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["run", str(target), *flags], catch_exceptions=False)
        return result.exit_code, result.stdout, result.stderr

    def test_live_is_off_by_default(self, project: Path) -> None:
        _, _, err = self.invoke(project.parent)
        assert "will execute the target's own code" not in err

    def test_the_flag_discloses(self, project: Path) -> None:
        _, _, err = self.invoke(project.parent, "--live")
        assert "will execute the target's own code" in err

    def test_no_live_is_accepted_explicitly(self, project: Path) -> None:
        code, _, err = self.invoke(project.parent, "--no-live")
        assert "will execute the target's own code" not in err
        assert code in (0, 1)

    def test_the_notice_does_not_corrupt_json(self, project: Path) -> None:
        """A security notice that breaks the document it warns you about would
        be its own small joke. It goes to stderr."""
        _, out, err = self.invoke(project.parent, "--live", "--format", "json")
        assert "will execute the target's own code" in err
        assert json.loads(out)["findings"]

    def test_an_unavailable_live_tier_warns_and_continues(self, tmp_path: Path) -> None:
        """A complete project with no virtualenv. The static tier still has 76
        rules to run, so a live tier that will not start is a warning, not an
        abandoned audit -- and specifically not the exit 2 that means djaudit
        could not read the project at all."""
        make_project(tmp_path)
        code, out, err = self.invoke(tmp_path, "--live")
        assert "live tier unavailable" in err
        assert code != 2, "a live tier that will not start is not a reason to abandon the audit"
        assert "DJS-" in out, "the static rules still ran and still reported"

    def test_the_control_a_project_it_cannot_read_does_exit_two(self, tmp_path: Path) -> None:
        """Distinguishes the two: exit 2 is still reachable, just not for this."""
        (tmp_path / "manage.py").write_text(MANAGE)
        code, _, _ = self.invoke(tmp_path, "--live")
        assert code == 2

    def test_the_live_tier_answers_what_the_static_tier_could_not(self, project: Path) -> None:
        """The point of the whole tier, in one observable difference: the static
        tier cannot resolve the Django version here, and the live tier asks."""
        _, without, _ = self.invoke(project.parent)
        _, with_live, _ = self.invoke(project.parent, "--live")
        assert "Django version unknown" in without
        assert "Django version unknown" not in with_live
