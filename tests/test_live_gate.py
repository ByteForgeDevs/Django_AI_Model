"""The gate that decides whether the live CI job checked anything.

Every test in `tests/live` skips itself when `DJAUDIT_TEST_POSTGRES` is unset.
That is right on a laptop and dangerous in CI, where a typo in the env block,
a renamed variable or a service container that never started all produce a job
that runs nothing and reports success in the same words as one that ran
everything.

So the gate has to fail on silence, and these are the shapes silence takes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from live_gate import FLOOR, main  # noqa: E402

PASSED = (
    '<testsuites><testsuite name="pytest" tests="{tests}" failures="0" '
    'errors="0" skipped="{skipped}">{cases}</testsuite></testsuites>'
)

CASE = '<testcase classname="tests.live.test_x" name="test_{n}"/>'

SKIP = (
    '<testcase classname="tests.live.test_x" name="test_{n}">'
    '<skipped message="set DJAUDIT_TEST_POSTGRES to a libpq DSN"/></testcase>'
)


def report(tmp_path: Path, *, tests: int, skipped: int = 0) -> Path:
    cases = "".join(SKIP.format(n=i) for i in range(skipped))
    cases += "".join(CASE.format(n=i) for i in range(skipped, tests))
    path = tmp_path / "live.xml"
    path.write_text(PASSED.format(tests=tests, skipped=skipped, cases=cases))
    return path


class TestItPasses:
    def test_a_full_run_of_real_tests(self, tmp_path: Path) -> None:
        assert main(["live_gate.py", str(report(tmp_path, tests=FLOOR))]) == 0

    def test_and_it_is_not_simply_always_happy(self, tmp_path: Path) -> None:
        """The control for the assertion above."""
        assert main(["live_gate.py", str(report(tmp_path, tests=FLOOR, skipped=1))]) == 1


class TestTheSilencesItCatches:
    def test_a_run_where_everything_skipped(self, tmp_path: Path) -> None:
        """The failure the job exists to prevent: `DJAUDIT_TEST_POSTGRES`
        misspelt, every live test skipped, pytest exiting 0."""
        assert main(["live_gate.py", str(report(tmp_path, tests=FLOOR, skipped=FLOOR))]) == 1

    def test_a_single_skip_is_enough(self, tmp_path: Path) -> None:
        """A tolerance here would be a place for skips to accumulate, and each
        one is a claim about PostgreSQL that nothing checked."""
        assert main(["live_gate.py", str(report(tmp_path, tests=FLOOR, skipped=1))]) == 1

    def test_a_run_that_collected_nothing(self, tmp_path: Path) -> None:
        """Zero skipped is also true of a suite that never ran. A gate reading
        only the skip count would call this a pass."""
        assert main(["live_gate.py", str(report(tmp_path, tests=0))]) == 1

    def test_a_run_that_collected_almost_nothing(self, tmp_path: Path) -> None:
        """A path typo that matches one file collects a handful of tests and
        skips none of them."""
        assert main(["live_gate.py", str(report(tmp_path, tests=FLOOR - 1))]) == 1

    def test_a_report_that_was_never_written(self, tmp_path: Path) -> None:
        """pytest crashed before writing one. `if: always()` means the gate
        still runs, and a missing file must not read as nothing to complain
        about."""
        assert main(["live_gate.py", str(tmp_path / "absent.xml")]) == 1


class TestItReadsTheReportRatherThanTheSummary:
    def test_a_bare_testsuite_root_is_understood(self, tmp_path: Path) -> None:
        """pytest writes `<testsuites>` wrapping one `<testsuite>`, but the
        bare form exists in the wild and losing the counts would silently
        become 'nothing ran'."""
        path = tmp_path / "bare.xml"
        path.write_text(f'<testsuite tests="{FLOOR}" failures="0" errors="0" skipped="0"/>')
        assert main(["live_gate.py", str(path)]) == 0

    def test_it_names_the_tests_that_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A gate that says only 'something skipped' sends the reader to the
        log it was meant to replace."""
        main(["live_gate.py", str(report(tmp_path, tests=FLOOR, skipped=2))])
        printed = capsys.readouterr().out
        assert "test_0" in printed
        assert "DJAUDIT_TEST_POSTGRES" in printed


class TestItsOwnArguments:
    def test_no_report_named(self) -> None:
        assert main(["live_gate.py"]) == 2
