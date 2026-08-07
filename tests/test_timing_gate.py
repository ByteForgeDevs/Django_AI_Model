"""The timing gate, checked against the ways a timing can lie.

``scripts/timing_gate.py`` is the step risk 8 described as existing for the
whole of Phase 2 while nothing in CI timed anything. A gate written to close
that hole should not itself be taken on trust, and a performance gate has an
unusually nasty failure mode: every way of making it pass by accident makes it
*look better*, not worse.

Three of those ways are tested here, because all three were reachable in the
version that was written first:

* a run that could not read the project finishes almost instantly and posts the
  best number on the board;
* a run whose rules all crashed does less work and looks like an optimisation;
* an unloaded budget nobody ever approaches passes forever and measures
  nothing.

The timings are supplied rather than measured. A test that ran the real
analyser would be timing this machine, so it would be slow, flaky, and would
assert nothing about the gate's own logic.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from timing_gate import TIGHTEN_BELOW, Timing, main, measure, report  # noqa: E402


def timing(
    *,
    runs: tuple[float, ...] = (5.0,),
    budget: float = 10.0,
    incomplete: tuple[str, ...] = (),
    rule_errors: tuple[str, ...] = (),
) -> Timing:
    return Timing(
        name="target",
        budget=budget,
        runs=runs,
        findings=7,
        incomplete=incomplete,
        rule_errors=rule_errors,
    )


@dataclass
class FakeDiagnostic:
    code: str
    blocking: bool = True


@dataclass
class FakeContext:
    diagnostics: tuple[FakeDiagnostic, ...] = ()


@dataclass
class FakeResult:
    context: FakeContext = field(default_factory=FakeContext)
    findings: list[object] = field(default_factory=list)
    rule_errors: dict[str, str] = field(default_factory=dict)


class TestTheVerdict:
    def test_the_fastest_run_is_the_estimate(self):
        """A shared runner can only make a run slower, never faster."""
        assert timing(runs=(9.0, 4.0, 30.0)).best == 4.0

    def test_a_run_inside_the_budget_passes(self):
        assert timing(runs=(4.0,), budget=10.0).ok

    def test_a_run_over_the_budget_fails(self):
        assert timing(runs=(11.0,), budget=10.0).over_budget

    def test_one_fast_run_does_not_rescue_a_slow_target(self):
        """The best of several is still the best -- but it must be real."""
        assert not timing(runs=(11.0, 12.0, 14.0), budget=10.0).ok

    def test_headroom_is_negative_when_over(self):
        assert timing(runs=(15.0,), budget=10.0).headroom < 0

    def test_headroom_is_the_unused_fraction(self):
        assert timing(runs=(4.0,), budget=10.0).headroom == 0.6


class TestARunThatDidNotHappen:
    """The failure this gate exists to not have.

    Every incomplete run is *fast*. A gate that only compared a number against
    a budget would rank a project it could not read above every project it
    could.
    """

    def test_an_unreadable_project_does_not_pass_by_being_quick(self):
        stalled = timing(runs=(0.1,), budget=10.0, incomplete=("settings-in-class-body",))
        assert stalled.best < stalled.budget
        assert not stalled.ok

    def test_crashed_rules_do_not_count_as_an_optimisation(self):
        broken = timing(runs=(0.2,), budget=10.0, rule_errors=("DJS-001",))
        assert broken.best < broken.budget
        assert not broken.ok

    def test_both_reasons_are_reported(self):
        both = timing(incomplete=("no-settings-module",), rule_errors=("DJA-002",))
        assert both.partial == ("no-settings-module", "DJA-002")

    def test_the_report_refuses_to_stand_behind_the_number(self):
        text = report(timing(runs=(0.1,), incomplete=("settings-in-class-body",)))
        assert "analysis incomplete" in text
        assert "its speed means nothing" in text

    def test_a_non_blocking_diagnostic_is_not_a_reason(self, tmp_path, monkeypatch):
        """Only a diagnostic that stopped the analysis invalidates the timing."""
        monkeypatch.setattr(
            "timing_gate.engine.run",
            lambda _root: FakeResult(
                context=FakeContext((FakeDiagnostic("advisory", blocking=False),))
            ),
        )
        assert measure(tmp_path, 10.0, "target", 1).partial == ()


class TestTighteningAnIdleBudget:
    """A budget nothing approaches has stopped measuring anything."""

    def test_a_wildly_generous_budget_asks_to_be_tightened(self):
        assert timing(runs=(1.0,), budget=100.0).should_tighten

    def test_a_budget_being_used_is_left_alone(self):
        assert not timing(runs=(9.0,), budget=10.0).should_tighten

    def test_the_threshold_is_where_it_claims_to_be(self):
        budget = 10.0
        assert timing(runs=(budget * TIGHTEN_BELOW - 0.01,), budget=budget).should_tighten
        assert not timing(runs=(budget * TIGHTEN_BELOW + 0.01,), budget=budget).should_tighten

    def test_being_fast_is_not_a_failure(self):
        """Failing CI for an improvement would punish the improvement."""
        fast = timing(runs=(1.0,), budget=100.0)
        assert fast.should_tighten
        assert fast.ok

    def test_the_suggestion_names_a_number_to_move_towards(self):
        text = report(timing(runs=(1.2,), budget=100.0))
        assert "Tighten it towards 1.2s" in text


class TestMeasuring:
    def test_it_runs_the_analyser_the_requested_number_of_times(self, tmp_path, monkeypatch):
        calls = []

        def run(root):
            calls.append(root)
            return FakeResult()

        monkeypatch.setattr("timing_gate.engine.run", run)
        result = measure(tmp_path, 10.0, "target", 3)
        assert len(calls) == 3
        assert len(result.runs) == 3

    def test_it_carries_the_blocking_diagnostics_out(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "timing_gate.engine.run",
            lambda _root: FakeResult(context=FakeContext((FakeDiagnostic("no-settings-module"),))),
        )
        assert measure(tmp_path, 10.0, "target", 1).incomplete == ("no-settings-module",)

    def test_it_carries_rule_errors_out(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "timing_gate.engine.run",
            lambda _root: FakeResult(rule_errors={"DJP-001": "boom"}),
        )
        assert measure(tmp_path, 10.0, "target", 1).rule_errors == ("DJP-001",)

    def test_it_collects_before_every_sample(self, tmp_path, monkeypatch):
        """Each sample must start from the same heap.

        Without this, sample N is measured while CPython is still holding
        sample N-1's garbage, and every sample after the first reads high. That
        is not hypothetical: the gate's first CI run reported netbox at 7.08s,
        11.07s, 12.99s -- monotonically rising across three identical runs,
        which was read as runner noise until the shape gave it away. Only
        best-of-N kept the gate green while it was measuring itself.
        """
        events = []

        def run(_root):
            events.append("run")
            return FakeResult()

        monkeypatch.setattr("timing_gate.gc.collect", lambda *a, **k: events.append("collect"))
        monkeypatch.setattr("timing_gate.engine.run", run)
        measure(tmp_path, 10.0, "target", 3)
        assert events == ["collect", "run", "collect", "run", "collect", "run"]

    def test_it_drops_the_result_before_the_next_sample(self, tmp_path, monkeypatch):
        """Holding the previous result alive would defeat the collect.

        The result owns the project context, which owns every parsed AST. If a
        reference outlives the loop iteration, the collect at the top of the
        next one has nothing it is allowed to free.

        The assertion has to be made *at collect time*. Asserting that the
        first result is dead by the end of the loop passes either way, because
        rebinding ``result`` on the next iteration drops it regardless -- just
        too late to be collected.
        """
        import weakref

        refs: list[weakref.ref[FakeResult]] = []
        alive_at_collect: list[bool] = []

        def run(_root):
            result = FakeResult()
            refs.append(weakref.ref(result))
            return result

        def collect(*_args, **_kwargs):
            alive_at_collect.append(any(ref() is not None for ref in refs))

        monkeypatch.setattr("timing_gate.engine.run", run)
        monkeypatch.setattr("timing_gate.gc.collect", collect)
        measure(tmp_path, 10.0, "target", 3)
        assert alive_at_collect == [False, False, False]


class TestTheCommand:
    def _stub(self, monkeypatch, result: FakeResult, delay: float = 0.0) -> None:
        def run(_root):
            if delay:
                time.sleep(delay)
            return result

        monkeypatch.setattr("timing_gate.engine.run", run)

    def test_a_missing_target_is_an_error_not_a_pass(self, tmp_path, capsys):
        assert main([str(tmp_path / "absent"), "--name", "x", "--budget", "10"]) == 2
        assert "not a directory" in capsys.readouterr().err

    def test_a_fast_run_exits_zero(self, tmp_path, monkeypatch):
        self._stub(monkeypatch, FakeResult())
        assert main([str(tmp_path), "--name", "x", "--budget", "1000", "--runs", "1"]) == 0

    def test_a_slow_run_exits_one(self, tmp_path, monkeypatch):
        """Slept rather than hoped for: a real clock needs a real lower bound."""
        self._stub(monkeypatch, FakeResult(), delay=0.05)
        assert main([str(tmp_path), "--name", "x", "--budget", "0.001", "--runs", "1"]) == 1

    def test_a_budget_of_zero_is_rejected_rather_than_dividing_by_it(self, tmp_path, capsys):
        """Found by these tests: headroom is a fraction of the budget."""
        assert main([str(tmp_path), "--name", "x", "--budget", "0"]) == 2
        assert "must be positive" in capsys.readouterr().err

    def test_a_negative_budget_is_rejected(self, tmp_path, capsys):
        assert main([str(tmp_path), "--name", "x", "--budget", "-5"]) == 2
        assert "must be positive" in capsys.readouterr().err

    def test_zero_runs_is_rejected_rather_than_taking_min_of_nothing(self, tmp_path, capsys):
        assert main([str(tmp_path), "--name", "x", "--budget", "10", "--runs", "0"]) == 2
        assert "at least one run" in capsys.readouterr().err

    def test_an_incomplete_run_exits_one_however_fast_it_was(self, tmp_path, monkeypatch, capsys):
        self._stub(
            monkeypatch,
            FakeResult(context=FakeContext((FakeDiagnostic("settings-in-class-body"),))),
        )
        code = main([str(tmp_path), "--name", "x", "--budget", "1000", "--runs", "1"])
        assert code == 1
        assert "::error::" in capsys.readouterr().out

    def test_it_writes_the_report_into_the_job_summary(self, tmp_path, monkeypatch):
        self._stub(monkeypatch, FakeResult())
        summary = tmp_path / "summary.md"
        main(
            [
                str(tmp_path),
                "--name",
                "netbox",
                "--budget",
                "1000",
                "--runs",
                "1",
                "--summary",
                str(summary),
            ]
        )
        assert "timing gate — netbox" in summary.read_text(encoding="utf-8")

    def test_the_summary_is_appended_not_overwritten(self, tmp_path, monkeypatch):
        """Each target in the matrix writes into the same file."""
        self._stub(monkeypatch, FakeResult())
        summary = tmp_path / "summary.md"
        summary.write_text("### an earlier step\n", encoding="utf-8")
        main(
            [
                str(tmp_path),
                "--name",
                "px",
                "--budget",
                "1000",
                "--runs",
                "1",
                "--summary",
                str(summary),
            ]
        )
        text = summary.read_text(encoding="utf-8")
        assert "an earlier step" in text
        assert "timing gate — px" in text
