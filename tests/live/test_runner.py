"""Tests for the subprocess runner.

Every test here runs a real process. The module's whole purpose is what happens
when a subprocess misbehaves, and a mocked `Popen` would only prove we can
mock `Popen`.

The interpreter used is djaudit's own, via `self_check`. It is refused as a
*target* by design, but the runner has to be exercised against something that
really exists, and this is the one interpreter we know does.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from djaudit.live.interpreter import Creator
from djaudit.live.runner import (
    DEFAULT_TIMEOUT,
    OUTPUT_LIMIT,
    PASSTHROUGH,
    REFUSED,
    Outcome,
    _in_its_own_group,
    _terminate,
    build_environment,
    probe,
    run_command,
    run_python,
    self_check,
)


def _alive(pid: int) -> bool:
    """Whether `pid` still exists, asked of the kernel rather than of `ps`.

    Signal 0 performs the permission and existence checks and delivers nothing.
    A zombie answers yes, which is the honest answer: it has not been reaped.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestItRunsThings:
    def test_a_command_that_succeeds(self, tmp_path: Path) -> None:
        outcome = run_python(self_check(), ["-c", "print(21 * 2)"], cwd=tmp_path)
        assert outcome.ok
        assert outcome.stdout.strip() == "42"

    def test_a_command_that_fails_is_reported_rather_than_raised(self, tmp_path: Path) -> None:
        """A live rule reports what it observed; the target's failure is data."""
        outcome = run_python(self_check(), ["-c", "raise SystemExit(3)"], cwd=tmp_path)
        assert not outcome.ok
        assert outcome.returncode == 3

    def test_stderr_is_captured_separately(self, tmp_path: Path) -> None:
        outcome = run_python(
            self_check(),
            ["-c", "import sys; sys.stdout.write('out'); sys.stderr.write('err')"],
            cwd=tmp_path,
        )
        assert outcome.stdout == "out"
        assert outcome.stderr == "err"

    def test_it_runs_in_the_directory_it_was_given(self, tmp_path: Path) -> None:
        outcome = probe(self_check(), "__import__('os').getcwd()", cwd=tmp_path)
        assert Path(outcome.stdout).resolve() == tmp_path.resolve()

    def test_a_missing_executable_is_an_outcome_not_an_exception(self, tmp_path: Path) -> None:
        outcome = run_command([str(tmp_path / "nothing")], cwd=tmp_path)
        assert not outcome.started
        assert not outcome.ok
        assert outcome.error is not None
        assert "FileNotFoundError" in outcome.error

    def test_a_missing_working_directory_is_an_outcome_too(self, tmp_path: Path) -> None:
        outcome = run_python(self_check(), ["-c", ""], cwd=tmp_path / "absent")
        assert not outcome.started

    def test_a_command_that_never_started_captured_nothing(self, tmp_path: Path) -> None:
        """Nothing ran, so there is no output and nothing was cut short."""
        outcome = run_command([str(tmp_path / "nothing")], cwd=tmp_path)
        assert (outcome.stdout, outcome.stderr, outcome.truncated) == ("", "", False)

    def test_the_duration_is_measured(self, tmp_path: Path) -> None:
        outcome = run_python(self_check(), ["-c", "import time; time.sleep(0.2)"], cwd=tmp_path)
        assert outcome.duration >= 0.2


class TestTheAllowlistIsWrittenDown:
    """Asserted literally, because every other test here builds its expectation
    from the constant and so cannot notice the constant itself being wrong."""

    def test_passthrough_is_exactly_these(self) -> None:
        assert {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TZ",
            "TMPDIR",
            "SYSTEMROOT",
            "COMSPEC",
            "PATHEXT",
        } == PASSTHROUGH

    def test_refused_is_exactly_these(self) -> None:
        assert {
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "DJANGO_SETTINGS_MODULE",
        } == REFUSED

    def test_nothing_is_both_allowed_and_refused(self) -> None:
        assert not PASSTHROUGH & REFUSED

    def test_the_settings_module_is_not_inherited(self) -> None:
        """Named on its own: inheriting it would mean measuring our own answer
        instead of the target's."""
        assert "DJANGO_SETTINGS_MODULE" not in PASSTHROUGH

    def test_unbuffered_is_on_so_a_killed_process_keeps_what_it_wrote(self) -> None:
        assert build_environment()["PYTHONUNBUFFERED"] == "1"

    def test_bytecode_writing_is_off_in_the_environment_too(self) -> None:
        assert build_environment()["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_the_environment_route_still_works_for_a_raw_command(self, tmp_path: Path) -> None:
        """Why both variables stay, now that `run_python` restates them as flags.

        `run_command` passes no flags -- it runs what it is given, which may be
        `manage.py` under some wrapper of the target's own -- so for those
        callers the environment is the only channel there is.
        """
        (tmp_path / "leftover.py").write_text("value = 1\n")
        (tmp_path / "entry.py").write_text("import leftover\n")
        outcome = run_command([sys.executable, "entry.py"], cwd=tmp_path)
        assert outcome.ok, outcome.stderr
        assert not (tmp_path / "__pycache__").exists()


class TestItInheritsNoSecrets:
    """A CI job's environment holds deployment tokens and cloud keys.

    Handing them to a subprocess that runs arbitrary code out of the repository
    under audit would make djaudit a credential exfiltration path -- a
    supply-chain vulnerability introduced by a security tool.
    """

    def test_a_secret_in_our_environment_does_not_reach_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI")
        outcome = probe(self_check(), "sorted(__import__('os').environ)", cwd=tmp_path)
        assert "AWS_SECRET_ACCESS_KEY" not in outcome.stdout

    def test_the_child_sees_only_what_was_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SENTRY_DSN", "https://x@y/1")
        monkeypatch.setenv("NPM_TOKEN", "npm_x")
        outcome = probe(self_check(), "sorted(__import__('os').environ)", cwd=tmp_path)
        seen = set(eval(outcome.stdout))
        assert seen <= PASSTHROUGH | {"PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}

    def test_the_environment_is_built_up_rather_than_filtered_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A denylist is only as good as its last update."""
        monkeypatch.setenv("SOME_FUTURE_CREDENTIAL_FORMAT", "x")
        assert "SOME_FUTURE_CREDENTIAL_FORMAT" not in build_environment()

    def test_path_does_cross_because_nothing_starts_without_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        assert build_environment()["PATH"] == "/usr/bin"

    def test_a_caller_can_add_what_it_needs_explicitly(self) -> None:
        assert build_environment({"DATABASE_URL": "postgres:///x"})["DATABASE_URL"] == (
            "postgres:///x"
        )

    def test_an_added_variable_reaches_the_child(self, tmp_path: Path) -> None:
        outcome = probe(
            self_check(),
            "__import__('os').environ['DJAUDIT_MARKER']",
            cwd=tmp_path,
        )
        assert not outcome.ok
        outcome = run_python(
            self_check(),
            ["-c", "import os; print(os.environ['DJAUDIT_MARKER'])"],
            cwd=tmp_path,
            extra_environment={"DJAUDIT_MARKER": "here"},
        )
        assert outcome.stdout.strip() == "here"

    def test_bytecode_writing_is_off_so_the_audit_leaves_no_traces(self, tmp_path: Path) -> None:
        """A read-only audit that leaves `__pycache__` behind is not read-only.

        This test passed for the wrong reason while `run_python` used `-I`: the
        import could not resolve at all, so nothing was compiled and nothing was
        written. Making the project importable revealed that `-E` was discarding
        our own `PYTHONDONTWRITEBYTECODE`, which is why `-B` now says it again
        in the only form the interpreter will listen to.
        """
        module = tmp_path / "leftover.py"
        module.write_text("value = 1\n")
        outcome = run_python(self_check(), ["-c", "import leftover"], cwd=tmp_path)
        assert outcome.ok, outcome.stderr
        assert not (tmp_path / "__pycache__").exists()

    def test_the_child_really_is_unbuffered(self, tmp_path: Path) -> None:
        """`-E` discards `PYTHONUNBUFFERED` as readily as it discards the rest."""
        outcome = probe(self_check(), "__import__('sys').stdout.write_through", cwd=tmp_path)
        assert outcome.stdout.strip() == "True"

    def test_a_killed_process_keeps_what_it_had_already_written(self, tmp_path: Path) -> None:
        """The point of being unbuffered, stated as behaviour.

        A block-buffered pipe loses everything the target wrote before the
        timeout, which is exactly the evidence a live rule needs most when a
        command hangs partway through.
        """
        outcome = run_python(
            self_check(),
            ["-c", "print('said this first'); import time; time.sleep(60)"],
            cwd=tmp_path,
            timeout=2,
        )
        assert outcome.timed_out
        assert outcome.stdout.strip() == "said this first"

    @pytest.mark.parametrize("name", sorted(REFUSED))
    def test_a_caller_may_not_redirect_the_interpreter(self, name: str) -> None:
        with pytest.raises(ValueError, match="redirect the interpreter"):
            build_environment({name: "x"})

    def test_our_own_pythonpath_does_not_reach_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Our packages shadowing theirs defeats finding their interpreter."""
        planted = tmp_path / "shadow"
        planted.mkdir()
        (planted / "json.py").write_text("raise SystemExit(9)\n")
        monkeypatch.setenv("PYTHONPATH", str(planted))
        outcome = run_python(self_check(), ["-c", "import json; print('clean')"], cwd=tmp_path)
        assert outcome.stdout.strip() == "clean"

    def test_the_interpreter_ignores_python_variables(self, tmp_path: Path) -> None:
        """Belt and braces: `-E` means it would ignore them even if one got in."""
        outcome = probe(self_check(), "__import__('sys').flags.ignore_environment", cwd=tmp_path)
        assert outcome.stdout.strip() == "1"

    def test_the_user_site_directory_is_not_consulted(self, tmp_path: Path) -> None:
        """A package in the invoking user's home may not answer an import on
        the target's behalf."""
        outcome = probe(self_check(), "__import__('sys').flags.no_user_site", cwd=tmp_path)
        assert outcome.stdout.strip() == "1"

    def test_the_script_directory_is_still_importable(self, tmp_path: Path) -> None:
        """The reason this is `-E -s` and not `-I`.

        `-I` also implies `-P`, which stops Python prepending the script's own
        directory to `sys.path`. Every Django project imports its settings
        package that way, so `-I` made `manage.py` unrunnable -- the one thing
        the live tier exists to do. This is that defect, pinned.
        """
        (tmp_path / "sibling.py").write_text("VALUE = 'imported'\n")
        (tmp_path / "entry.py").write_text("import sibling; print(sibling.VALUE)\n")
        outcome = run_python(self_check(), ["entry.py"], cwd=tmp_path)
        assert outcome.stdout.strip() == "imported"

    def test_a_real_manage_py_can_import_its_own_settings(self, tmp_path: Path) -> None:
        """The same defect at full size, against Django itself."""
        (tmp_path / "proj").mkdir()
        (tmp_path / "proj" / "__init__.py").write_text("")
        (tmp_path / "proj" / "settings.py").write_text(
            "SECRET_KEY = 'x'\n"
            "INSTALLED_APPS = ['django.contrib.contenttypes']\n"
            "DATABASES = {}\n"
            "USE_TZ = True\n"
        )
        (tmp_path / "manage.py").write_text(
            "import os, sys\n"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proj.settings')\n"
            "from django.core.management import execute_from_command_line\n"
            "execute_from_command_line(sys.argv)\n"
        )
        outcome = run_python(self_check(), ["manage.py", "check"], cwd=tmp_path, timeout=60)
        assert outcome.ok, outcome.stderr

    def test_a_raw_command_is_not_forced_isolated(self, tmp_path: Path) -> None:
        """`run_command` runs what it is given; the flags belong to
        `run_python`."""
        outcome = run_command(
            [sys.executable, "-c", "import sys; print(sys.flags.ignore_environment)"], cwd=tmp_path
        )
        assert outcome.stdout.strip() == "0"


class TestTheTimeoutIsAHardTimeout:
    """`subprocess.run`'s timeout kills only the process it started.

    Django's `manage.py` spawns children, and a child that outlives its parent
    keeps our pipes open, so the read after the kill blocks on a pipe nobody
    will ever close. That is the failure this class exists for.
    """

    def test_a_sleeping_process_is_killed(self, tmp_path: Path) -> None:
        outcome = run_python(
            self_check(), ["-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout=1
        )
        assert outcome.timed_out

    def test_the_timeout_is_honoured_to_the_second(self, tmp_path: Path) -> None:
        started = time.monotonic()
        run_python(self_check(), ["-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout=1)
        assert time.monotonic() - started < 15

    def test_a_grandchild_holding_our_pipes_does_not_hang_us(self, tmp_path: Path) -> None:
        """The one that matters. Without the process group this never returns."""
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "time.sleep(120)\n"
        )
        started = time.monotonic()
        outcome = run_python(self_check(), ["-c", script], cwd=tmp_path, timeout=2)
        assert outcome.timed_out
        assert time.monotonic() - started < 30

    @pytest.mark.skipif(os.name != "posix", reason="process groups are posix")
    def test_the_grandchild_is_actually_dead_afterwards(self, tmp_path: Path) -> None:
        """Returning promptly is not the same as having killed anything.

        The grandchild records its own pid and we ask the kernel about it.
        An earlier version of this test scanned `ps` output for a marker in the
        command line and passed against a deliberately broken runner, because
        `ps` truncates that column to the terminal width and the marker sat past
        the cut. A liveness check that cannot observe a live process is not a
        liveness check.
        """
        recorded = tmp_path / "grandchild.pid"
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])\n"
            f"open({str(recorded)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(90)\n"
        )
        run_python(self_check(), ["-c", script], cwd=tmp_path, timeout=3)
        time.sleep(1)

        assert recorded.exists(), "the grandchild never started, so this proves nothing"
        pid = int(recorded.read_text())
        assert not _alive(pid)

    @pytest.mark.skipif(os.name != "posix", reason="process groups are posix")
    def test_the_liveness_check_can_see_a_live_process(self, tmp_path: Path) -> None:
        """The control for the test above, which asserts an absence."""
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            start_new_session=True,
        )
        try:
            assert _alive(process.pid)
        finally:
            process.kill()
            process.wait(timeout=30)
        assert not _alive(process.pid)

    def test_a_timed_out_command_has_no_returncode(self, tmp_path: Path) -> None:
        """`None` rather than a signal number: it did not choose to exit."""
        outcome = run_python(
            self_check(), ["-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout=1
        )
        assert outcome.returncode is None
        assert not outcome.ok

    def test_the_reason_says_what_was_exceeded(self, tmp_path: Path) -> None:
        """`1.0` rather than `1`: an int formats identically with and without
        the `g`, so an int here would not notice the format being dropped."""
        outcome = run_python(
            self_check(), ["-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout=1.0
        )
        assert outcome.error == "exceeded the 1s timeout and its process group was killed"

    def test_the_default_is_stated_rather_than_hidden(self) -> None:
        assert DEFAULT_TIMEOUT == 30.0


class TestItNeverSignalsItsOwnProcessGroup:
    """The guard that stops a timeout from killing djaudit and your shell.

    `_terminate` sends `SIGKILL` to a process *group*. If the child is not in a
    group of its own, that group is ours -- the test runner, its parent, and the
    terminal that started them. Removing `start_new_session` from `run_command`
    and running the timeout tests really does kill the shell, with no output,
    which is how this guard came to be written.
    """

    @pytest.mark.skipif(os.name != "posix", reason="process groups are posix")
    def test_a_child_of_ours_is_recognised_as_ours(self, tmp_path: Path) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            start_new_session=False,
        )
        try:
            assert not _in_its_own_group(process)
        finally:
            process.kill()
            process.wait(timeout=30)

    @pytest.mark.skipif(os.name != "posix", reason="process groups are posix")
    def test_a_child_in_a_new_session_is_recognised_as_separate(self, tmp_path: Path) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            start_new_session=True,
        )
        try:
            assert _in_its_own_group(process)
        finally:
            process.kill()
            process.wait(timeout=30)

    @pytest.mark.skipif(os.name != "posix", reason="process groups are posix")
    def test_terminating_a_shared_group_child_does_not_signal_the_group(
        self, tmp_path: Path
    ) -> None:
        """The proof: `_terminate` on such a child kills it and spares us.

        If the guard is removed this test does not fail -- it takes the whole
        session down with it. That is precisely why the guard is not optional.
        """
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            start_new_session=False,
        )
        _terminate(process)
        assert process.wait(timeout=30) != 0

    def test_a_process_that_already_exited_is_not_its_own_group(self, tmp_path: Path) -> None:
        """`getpgid` raises for a reaped pid; refusing is the safe answer."""
        process = subprocess.Popen([sys.executable, "-c", ""], cwd=tmp_path, start_new_session=True)
        process.wait(timeout=30)
        assert not _in_its_own_group(process)

    def test_terminating_a_process_that_already_exited_is_quiet(self, tmp_path: Path) -> None:
        process = subprocess.Popen([sys.executable, "-c", ""], cwd=tmp_path, start_new_session=True)
        process.wait(timeout=30)
        _terminate(process)

    def test_a_platform_without_process_groups_never_tries_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows `os.getpgid` does not exist, so asking would raise
        `AttributeError` rather than return a wrong answer. The guard is
        unreachable on posix, which is why it is reached here on purpose."""
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            start_new_session=True,
        )
        try:
            assert _in_its_own_group(process)
            monkeypatch.setattr(os, "name", "nt")
            assert not _in_its_own_group(process)
        finally:
            monkeypatch.undo()
            process.kill()
            process.wait(timeout=30)

    @pytest.mark.parametrize("function", [run_command, run_python])
    def test_the_default_is_the_one_that_is_documented(
        self, function: Callable[..., Outcome]
    ) -> None:
        """Cheaper than waiting 30s for it, and it fails on the same defect."""
        assert inspect.signature(function).parameters["timeout"].default == DEFAULT_TIMEOUT


class TestStdinIsClosed:
    """The failure that looks like a hang, because it is one."""

    def test_a_command_that_asks_a_question_gets_eof(self, tmp_path: Path) -> None:
        started = time.monotonic()
        outcome = run_python(self_check(), ["-c", "input()"], cwd=tmp_path, timeout=10)
        assert not outcome.timed_out
        assert "EOFError" in outcome.stderr
        assert time.monotonic() - started < 10


class TestOutputIsBounded:
    def test_a_flood_is_truncated_rather_than_buffered(self, tmp_path: Path) -> None:
        outcome = run_python(
            self_check(), ["-c", f"print('x' * {OUTPUT_LIMIT * 3})"], cwd=tmp_path, timeout=60
        )
        assert outcome.truncated
        assert len(outcome.stdout) == OUTPUT_LIMIT

    def test_truncation_is_recorded_rather_than_hidden(self, tmp_path: Path) -> None:
        outcome = run_python(self_check(), ["-c", "print('small')"], cwd=tmp_path)
        assert not outcome.truncated

    def test_a_flood_on_stderr_is_also_recorded(self, tmp_path: Path) -> None:
        outcome = run_python(
            self_check(),
            ["-c", f"import sys; sys.stderr.write('e' * {OUTPUT_LIMIT * 2})"],
            cwd=tmp_path,
            timeout=60,
        )
        assert outcome.truncated

    def test_undecodable_output_does_not_become_our_crash(self, tmp_path: Path) -> None:
        """The target chooses this encoding, not us."""
        outcome = run_python(
            self_check(),
            ["-c", "import sys; sys.stdout.buffer.write(b'caf\\xe9')"],
            cwd=tmp_path,
        )
        assert outcome.ok
        assert "caf" in outcome.stdout


class TestDescribingWhatHappened:
    def test_a_success_names_the_code_and_the_time(self, tmp_path: Path) -> None:
        outcome = run_python(self_check(), ["-c", "pass"], cwd=tmp_path)
        executable = self_check().executable
        assert outcome.describe() == (
            f"`{executable} -E -s -B -u -c pass` exited 0 in {outcome.duration:.1f}s"
        )

    def test_a_failure_is_described_whole(self, tmp_path: Path) -> None:
        """Asserted entire: a substring check leaves the punctuation unchecked,
        and the backticks are what make this readable in a report."""
        outcome = run_python(self_check(), ["-c", "raise SystemExit(2)"], cwd=tmp_path)
        executable = self_check().executable
        assert outcome.describe() == (
            f"`{executable} -E -s -B -u -c raise SystemExit(2)` exited 2 in {outcome.duration:.1f}s"
        )

    def test_a_failure_to_start_says_so(self, tmp_path: Path) -> None:
        missing = tmp_path / "nothing"
        outcome = run_command([str(missing)], cwd=tmp_path)
        assert outcome.describe() == (
            f"could not run `{missing}`: FileNotFoundError: "
            f"[Errno 2] No such file or directory: '{missing}'"
        )

    def test_a_timeout_says_it_was_killed(self, tmp_path: Path) -> None:
        outcome = run_python(
            self_check(), ["-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout=1
        )
        executable = self_check().executable
        assert outcome.describe() == (
            f"`{executable} -E -s -B -u -c import time; time.sleep(60)` was killed "
            f"after {outcome.duration:.1f}s"
        )

    def test_a_script_argument_is_collapsed_to_one_line(self, tmp_path: Path) -> None:
        """`-c` carries a whole script, and a one-line summary must be one."""
        outcome = run_python(self_check(), ["-c", "x = 1\ny = 2\n"], cwd=tmp_path)
        assert "\n" not in outcome.describe()
        assert "-c x = 1 y = 2" in outcome.describe()

    def test_the_command_is_kept_verbatim(self, tmp_path: Path) -> None:
        """`describe` collapses for reading; the record itself does not."""
        outcome = run_python(self_check(), ["-c", "x = 1\ny = 2\n"], cwd=tmp_path)
        assert outcome.command[-1] == "x = 1\ny = 2\n"


class TestSelfCheck:
    """It exists so the runner can be exercised against a real interpreter."""

    def test_it_describes_the_interpreter_we_are_running_on(self) -> None:
        assert self_check().executable == Path(sys.executable)
        assert self_check().prefix == Path(sys.prefix)

    def test_the_version_is_a_dotted_string(self) -> None:
        version = self_check().version
        assert version == ".".join(str(part) for part in sys.version_info[:3])
        assert version is not None
        assert version.count(".") == 2

    def test_it_says_where_it_came_from(self) -> None:
        """It is reported to users, so it has to read as an explanation."""
        assert self_check().found_by == "djaudit's own interpreter"

    def test_it_is_not_presented_as_a_discovered_environment(self) -> None:
        assert self_check().creator is Creator.UNKNOWN


class TestOutcomeAsAValue:
    def test_ok_requires_both_started_and_zero(self) -> None:
        assert not Outcome(("x",), 0, "", "", 0.0, False, started=False).ok
        assert Outcome(("x",), 0, "", "", 0.0, False).ok
        assert not Outcome(("x",), 1, "", "", 0.0, False).ok

    def test_a_command_that_never_started_did_not_time_out(self) -> None:
        """Both are `returncode is None`, and they are not the same thing."""
        assert not Outcome(("x",), None, "", "", 0.0, False, started=False).timed_out
        assert Outcome(("x",), None, "", "", 0.0, False).timed_out
