"""Tests for the verification loop.

Each check is shown failing on its own defect: a patch that does not parse, one
that does not clear its finding, one that introduces another, and one whose
suite fails. A verifier that accepted everything would otherwise pass a suite
made only of happy paths.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import djaudit.rules  # noqa: F401  -- registers every rule
from djaudit import engine
from djaudit.llm.edit import Edit
from djaudit.llm.fix import Fix, fixes
from djaudit.llm.verify import (
    IGNORED,
    Level,
    Report,
    SuiteResult,
    Verification,
    explain_rejection,
    run_suite,
    scratch_copy,
    verified,
    verify,
)
from djaudit.models import Confidence, Family, Finding, Location, Severity, Tier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# `djaudit.llm` re-exports a function named `verify`, which shadows the
# submodule of the same name, so `import djaudit.llm.verify` yields the
# function. Reaching the module for monkeypatching has to go around that.
verify_module = importlib.import_module("djaudit.llm.verify")


def make_finding(
    rule_id: str = "DJS-001",
    file: str = "settings.py",
    line: int = 1,
    snippet: str = "DEBUG = True",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        title="a title",
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        family=Family.DJS,
        tier=Tier.STATIC,
        location=Location(file=file, line=line, snippet=snippet),
        message="a message",
        rationale="a rationale",
        remediation="a remediation",
    )


def a_project(tmp_path: Path) -> Path:
    """A copy of the fixture, so the original is never at risk."""
    root = tmp_path / "vulnerable_project"
    shutil.copytree(FIXTURES / "vulnerable_project", root)
    return root


def a_real_fix(root: Path) -> tuple[list[Fix], list[Finding]]:
    result = engine.run(root, min_confidence=Confidence.TENTATIVE)
    proposed, _ = fixes(result, root)
    return proposed, result.findings


class TestItAcceptsAGoodPatch:
    def test_a_real_fix_verifies(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        outcome = verify(proposed, root, before, Confidence.TENTATIVE)

        assert outcome.accepted
        assert outcome.cleared
        assert not outcome.still_present
        assert not outcome.introduced

    def test_the_original_is_never_written_to(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        untouched = {p: p.read_bytes() for p in root.rglob("*.py")}

        verify(proposed, root, before, Confidence.TENTATIVE)

        assert {p: p.read_bytes() for p in root.rglob("*.py")} == untouched

    def test_it_clears_every_finding_it_targeted(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        outcome = verify(proposed, root, before, Confidence.TENTATIVE)

        assert set(outcome.cleared) == {f.finding.fingerprint for f in proposed}

    def test_an_empty_batch_is_trivially_fine(self, tmp_path: Path) -> None:
        outcome = verify([], a_project(tmp_path), [])

        assert outcome.accepted
        assert outcome.fixes == ()


class TestEachCheckIsLoadBearing:
    def test_a_patch_that_does_not_parse_is_rejected(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        broken = replace(proposed[0], edits=(Edit(0, 0, "def (:\n", "sabotage"),))

        outcome = verify([broken], root, before, Confidence.TENTATIVE)

        assert not outcome.accepted
        assert not outcome.parses
        assert "no longer parses" in outcome.problem

    def test_a_patch_that_changes_nothing_leaves_its_finding_present(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        useless = replace(proposed[0], edits=())

        outcome = verify([useless], root, before, Confidence.TENTATIVE)

        assert not outcome.accepted
        assert outcome.still_present
        assert "still reported" in explain_rejection(outcome)

    def test_a_patch_that_introduces_a_finding_is_rejected(self, tmp_path: Path) -> None:
        """Turn one thing off while turning another on."""
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        fix = next(f for f in proposed if f.finding.rule_id == "DJS-001")
        sabotaged = replace(
            fix,
            edits=(
                *fix.edits,
                Edit(
                    len(fix.before),
                    len(fix.before),
                    "\nCORS_ALLOW_ALL_ORIGINS = True\n",
                    "sabotage",
                ),
            ),
        )

        outcome = verify([sabotaged], root, before, Confidence.TENTATIVE)

        assert not outcome.accepted
        assert outcome.introduced
        assert "introduced" in explain_rejection(outcome)

    def test_a_failing_suite_rejects_an_otherwise_perfect_patch(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        outcome = verify(proposed, root, before, Confidence.TENTATIVE, command="exit 3")

        assert not outcome.accepted
        assert outcome.suite is not None
        assert outcome.suite.code == 3
        assert "own tests failed" in explain_rejection(outcome)

    def test_a_passing_suite_keeps_it(self, tmp_path: Path) -> None:
        """The contrast: a verifier that always rejected would pass the above."""
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        outcome = verify(proposed, root, before, Confidence.TENTATIVE, command="true")

        assert outcome.accepted
        assert outcome.suite is not None
        assert outcome.suite.passed


class TestWhatVerifiedMeans:
    def test_without_a_suite_it_only_claims_static(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        outcome = verify(proposed, root, before, Confidence.TENTATIVE)

        assert outcome.confidence is Level.STATIC

    def test_with_a_passing_suite_it_claims_tested(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        outcome = verify(proposed, root, before, Confidence.TENTATIVE, command="true")

        assert outcome.confidence is Level.TESTED

    def test_a_rejected_patch_claims_nothing(self) -> None:
        outcome = Verification(fixes=(), parses=False, problem="broken")

        assert outcome.confidence is Level.FAILED

    def test_the_three_levels_are_distinct(self) -> None:
        assert len({Level.STATIC, Level.TESTED, Level.FAILED}) == 3


class TestTheSuiteRunner:
    def test_it_runs_in_the_copy_and_not_the_original(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("here")

        result = run_suite(tmp_path, "test -f marker.txt")

        assert result.passed

    def test_a_failure_carries_its_exit_code(self, tmp_path: Path) -> None:
        result = run_suite(tmp_path, "exit 42")

        assert not result.passed
        assert result.code == 42

    def test_output_is_captured(self, tmp_path: Path) -> None:
        result = run_suite(tmp_path, "echo something-distinctive")

        assert "something-distinctive" in result.output

    def test_stderr_is_captured_too(self, tmp_path: Path) -> None:
        result = run_suite(tmp_path, "echo on-stderr >&2; exit 1")

        assert "on-stderr" in result.output

    def test_output_is_bounded(self, tmp_path: Path) -> None:
        """A suite that prints a hundred megabytes must not be held in full."""
        result = run_suite(tmp_path, "python -c \"print('x' * 100000)\"")

        assert len(result.output) <= 4000

    def test_no_command_means_no_execution(self, tmp_path: Path) -> None:
        """The static-tier property: nothing runs unless asked."""
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        outcome = verify(proposed, root, before, Confidence.TENTATIVE)

        assert outcome.suite is None


class TestBisecting:
    def test_one_bad_patch_does_not_cost_the_good_ones(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        assert len(proposed) >= 2
        bad = replace(proposed[0], edits=())
        batch = [bad, *proposed[1:]]

        report = verified(batch, root, before, Confidence.TENTATIVE)

        assert len(report.accepted) == len(proposed) - 1
        assert bad not in report.accepted
        assert len(report.rejected) == 1

    def test_a_clean_batch_is_verified_once(self, tmp_path: Path) -> None:
        """Bisecting always would make this N engine runs instead of one."""
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        runs = 0
        original = engine.run

        def counted(*args: object, **kwargs: object) -> object:
            nonlocal runs
            runs += 1
            return original(*args, **kwargs)  # type: ignore[arg-type]

        engine.run = counted  # type: ignore[assignment]
        try:
            report = verified(proposed, root, before, Confidence.TENTATIVE)
        finally:
            engine.run = original

        assert report.accepted == list(proposed)
        assert runs == 1

    def test_every_patch_failing_accepts_none(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        useless = [replace(f, edits=()) for f in proposed]

        report = verified(useless, root, before, Confidence.TENTATIVE)

        assert report.accepted == []
        assert len(report.rejected) == len(proposed)
        assert report.confidence is Level.FAILED

    def test_an_empty_batch_reports_nothing(self, tmp_path: Path) -> None:
        report = verified([], a_project(tmp_path), [])

        assert report.accepted == []
        assert report.rejected == []

    def test_the_surviving_set_is_re_verified_together(self, tmp_path: Path) -> None:
        """Patches that pass alone must still pass as a group.

        Dropping one can change what the others mean, so the kept set is run
        once more rather than assembled from individual passes.
        """
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        bad = replace(proposed[0], edits=())

        report = verified([bad, *proposed[1:]], root, before, Confidence.TENTATIVE)

        assert report.batch is not None
        assert [f.finding.fingerprint for f in report.batch.fixes] == [
            f.finding.fingerprint for f in report.accepted
        ]
        assert report.confidence is Level.STATIC


class TestTheScratchCopy:
    def test_it_copies_the_source(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        into = tmp_path / "scratch"
        into.mkdir()

        copy = scratch_copy(root, into)

        assert (copy / "manage.py").exists()
        assert copy != root

    def test_it_leaves_out_what_is_never_worth_carrying(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        (root / ".git").mkdir(exist_ok=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (root / "node_modules").mkdir(exist_ok=True)
        (root / "node_modules" / "big.js").write_text("x")
        into = tmp_path / "scratch"
        into.mkdir()

        copy = scratch_copy(root, into)

        assert not (copy / ".git").exists()
        assert not (copy / "node_modules").exists()

    def test_the_ignore_list_covers_the_expensive_directories(self) -> None:
        assert IGNORED(".", [".git", ".venv", "node_modules", "src"]) == {
            ".git",
            ".venv",
            "node_modules",
        }


class TestExplainRejection:
    def test_a_parse_failure_reports_itself(self) -> None:
        outcome = Verification(fixes=(), parses=False, problem="settings.py no longer parses")

        assert "no longer parses" in explain_rejection(outcome)

    def test_a_suite_failure_names_the_code(self) -> None:
        outcome = Verification(
            fixes=(),
            parses=True,
            suite=SuiteResult("pytest", passed=False, code=1, output=""),
        )

        assert "exit 1" in explain_rejection(outcome)

    def test_an_introduced_finding_names_its_rule(self) -> None:
        outcome = Verification(fixes=(), parses=True, introduced=(make_finding(rule_id="DJS-013"),))

        assert "DJS-013" in explain_rejection(outcome)

    def test_there_is_always_a_reason(self) -> None:
        assert explain_rejection(Verification(fixes=(), parses=True))


class TestAgainstTheFixture:
    def test_the_fixture_loses_findings_and_gains_none(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        report = verified(proposed, root, before, Confidence.TENTATIVE)

        assert report.accepted
        assert report.confidence is Level.STATIC
        assert report.batch is not None
        assert report.batch.introduced == ()

    def test_a_real_command_runs_against_the_copy(self, tmp_path: Path) -> None:
        """End to end with an actual subprocess, not a stand-in."""
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        report = verified(
            proposed,
            root,
            before,
            Confidence.TENTATIVE,
            command='python -c "import ast,pathlib;'
            "[ast.parse(p.read_text()) for p in pathlib.Path('.').rglob('*.py')]\"",
        )

        assert report.accepted == list(proposed)
        assert report.confidence is Level.TESTED

    def test_a_suite_that_rejects_one_change_drops_only_that_one(self, tmp_path: Path) -> None:
        """A project whose own tests dislike exactly one of the fixes.

        This is the case the whole loop exists for: `SESSION_COOKIE_HTTPONLY`
        breaks a script that reads the cookie, so the project's suite fails on
        it and passes on everything else. The other three survive.
        """
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)
        assert any(f.finding.rule_id == "DJS-011" for f in proposed)

        report = verified(
            proposed,
            root,
            before,
            Confidence.TENTATIVE,
            command="! grep -rq 'SESSION_COOKIE_HTTPONLY = True' config/",
        )

        assert {f.finding.rule_id for f in report.accepted} == {"DJS-001", "DJS-008", "DJS-018"}
        assert [v.fixes[0].finding.rule_id for v in report.rejected] == ["DJS-011"]

    def test_a_suite_that_rejects_everything_accepts_nothing(self, tmp_path: Path) -> None:
        root = a_project(tmp_path)
        proposed, before = a_real_fix(root)

        report = verified(proposed, root, before, Confidence.TENTATIVE, command="exit 1")

        assert report.accepted == []


class TestEachRejectionReasonStandsAlone:
    """Every reason to reject must reject on its own.

    In real runs the reasons arrive together -- a patch that does not parse
    also carries a `problem` -- so each field silently backstops the others,
    and deleting any single one of them from `accepted` changes no outcome.
    These build each defect in isolation so that cannot happen.
    """

    def test_a_parse_failure_alone_rejects(self) -> None:
        assert not Verification(fixes=(), parses=False).accepted

    def test_a_copy_problem_alone_rejects(self) -> None:
        assert Verification(fixes=(), parses=True).accepted
        assert not Verification(fixes=(), parses=True, problem="the copy failed").accepted

    def test_a_finding_left_behind_alone_rejects(self) -> None:
        assert not Verification(fixes=(), parses=True, still_present=("DJS-001",)).accepted

    def test_a_new_finding_alone_rejects(self) -> None:
        assert not Verification(fixes=(), parses=True, introduced=(make_finding(),)).accepted

    def test_a_failing_suite_alone_rejects(self) -> None:
        failed = SuiteResult(command="pytest", passed=False, code=1, output="")
        assert not Verification(fixes=(), parses=True, suite=failed).accepted

    def test_a_rejected_verification_reports_no_level(self) -> None:
        """A failure must not be describable as either kind of pass."""
        passing = SuiteResult(command="pytest", passed=True, code=0, output="")
        rejected = Verification(fixes=(), parses=False, suite=passing)
        assert rejected.confidence is Level.FAILED

    def test_a_report_with_nothing_accepted_reports_no_level(self) -> None:
        assert Report(accepted=[], batch=Verification(fixes=(), parses=True)).confidence is (
            Level.FAILED
        )


class TestATimeoutIsAFailure:
    """A suite that never finishes has not passed.

    The timeout path builds its own result rather than reading one back from
    `subprocess`, so nothing else in the file constrains what it says.
    """

    def test_a_suite_that_outruns_the_timeout_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def timeout(*args: object, **kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="sleep", timeout=1)

        monkeypatch.setattr(subprocess, "run", timeout)
        result = run_suite(tmp_path, "sleep 10000")
        assert not result.passed
        assert "timed out" in result.output

    def test_a_timed_out_suite_rejects_the_patch(self, tmp_path: Path) -> None:
        timed_out = SuiteResult(command="sleep 10000", passed=False, code=-1, output="timed out")
        assert not Verification(fixes=(), parses=True, suite=timed_out).accepted


class TestTheSurvivingSetIsReVerified:
    """Patches that each hold up alone can still be wrong together.

    Bisecting establishes that every survivor is individually fine, which is
    not the same claim as the set being fine. Whatever ships is the set, so
    the set is what has to be checked -- and if it fails, nothing ships.
    """

    def _fake_verify(
        self, monkeypatch: pytest.MonkeyPatch, verdicts: list[bool]
    ) -> list[tuple[int, ...]]:
        """Answer each call from `verdicts`, recording the batch sizes asked."""
        seen: list[tuple[int, ...]] = []

        def fake(
            proposed: object,
            root: object,
            before: object,
            min_confidence: object = None,
            command: object = None,
        ) -> Verification:
            assert isinstance(proposed, list)
            seen.append(tuple(id(f) for f in proposed))
            return Verification(fixes=(), parses=verdicts.pop(0))

        monkeypatch.setattr(verify_module, "verify", fake)
        return seen

    def test_a_set_that_fails_together_ships_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        one, two, three = (object(), object(), object())
        # batch fails, one and two pass alone, three fails, the pair fails.
        seen = self._fake_verify(monkeypatch, [False, True, True, False, False])

        report = verify_module.verified([one, two, three], tmp_path, [])

        assert len(seen) == 5, "the surviving pair must be verified together"
        assert seen[-1] == (id(one), id(two))
        assert report.accepted == [], "a set that fails together must ship nothing"

    def test_a_set_that_holds_together_ships(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        one, two, three = (object(), object(), object())
        seen = self._fake_verify(monkeypatch, [False, True, True, False, True])

        report = verify_module.verified([one, two, three], tmp_path, [])

        assert len(seen) == 5
        assert report.accepted == [one, two]

    def test_a_batch_that_passes_whole_is_not_re_verified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        one, two = (object(), object())
        seen = self._fake_verify(monkeypatch, [True])

        report = verify_module.verified([one, two], tmp_path, [])

        assert len(seen) == 1, "a clean batch costs exactly one engine run"
        assert report.accepted == [one, two]
