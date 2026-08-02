"""Tests for the benchmark precision gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from djaudit.benchmark import compare
from djaudit.context import ProjectContext
from djaudit.engine import RunResult
from djaudit.models import Finding
from djaudit.triage import Triage, TriageEntry, Verdict
from tests.conftest import make_finding, make_findings


def make_result(
    findings: list[Finding],
    tmp_path: Path,
    rule_errors: dict[str, str] | None = None,
) -> RunResult:
    return RunResult(
        context=ProjectContext(root=tmp_path),
        findings=findings,
        rule_errors=rule_errors or {},
    )


def entry(finding: Finding, verdict: Verdict) -> TriageEntry:
    return TriageEntry.from_finding(finding, verdict)


class TestUntriaged:
    def test_untriaged_finding_fails_the_gate(self, tmp_path: Path) -> None:
        finding = make_finding("DJS-001")
        report = compare(make_result([finding], tmp_path), Triage(target="t"))

        assert report.untriaged == [finding]
        assert report.ok is False

    def test_fully_triaged_run_passes(self, tmp_path: Path) -> None:
        finding = make_finding("DJS-001")
        triage = Triage(target="t", entries=(entry(finding, Verdict.TRUE_POSITIVE),))

        report = compare(make_result([finding], tmp_path), triage)

        assert report.untriaged == []
        assert report.ok is True
        assert report.precision == 1.0

    def test_clean_run_against_empty_triage_passes(self, tmp_path: Path) -> None:
        report = compare(make_result([], tmp_path), Triage(target="t"))

        assert report.ok is True
        assert report.precision == 1.0
        assert "0 untriaged" in report.summary()


class TestRegression:
    def test_disappearing_true_positive_is_a_regression(self, tmp_path: Path) -> None:
        gone = make_finding("DJS-001")
        triage = Triage(target="t", entries=(entry(gone, Verdict.TRUE_POSITIVE),))

        report = compare(make_result([], tmp_path), triage)

        assert [e.fingerprint for e in report.regressed] == [gone.fingerprint]
        assert report.ok is False

    def test_disappearing_accepted_risk_is_also_a_regression(self, tmp_path: Path) -> None:
        gone = make_finding("DJS-001")
        triage = Triage(target="t", entries=(entry(gone, Verdict.ACCEPTED_RISK),))

        report = compare(make_result([], tmp_path), triage)

        assert len(report.regressed) == 1

    def test_disappearing_false_positive_is_resolved_not_regressed(self, tmp_path: Path) -> None:
        # Fixing a false positive must make the build greener, not redder.
        gone = make_finding("DJS-001")
        triage = Triage(target="t", entries=(entry(gone, Verdict.FALSE_POSITIVE),))

        report = compare(make_result([], tmp_path), triage)

        assert report.regressed == []
        assert [e.fingerprint for e in report.resolved] == [gone.fingerprint]
        assert report.ok is True


class TestFalsePositiveRate:
    def test_rate_is_measured_per_family(self, tmp_path: Path) -> None:
        # DJS is clean; DJA is 100% noise. A pooled rate would read 50% and
        # hide which family is actually broken.
        clean = make_findings("DJS-001", 5)
        noisy = make_findings("DJA-001", 5)
        entries = [entry(f, Verdict.TRUE_POSITIVE) for f in clean]
        entries += [entry(f, Verdict.FALSE_POSITIVE) for f in noisy]
        triage = Triage(target="t", entries=tuple(entries))

        report = compare(make_result(clean + noisy, tmp_path), triage)

        rates = {s.family: s.false_positive_rate for s in report.scores}
        assert rates == {"DJS": 0.0, "DJA": 1.0}
        assert [s.family for s in report.over_budget] == ["DJA"]
        assert report.ok is False

    def test_family_within_budget_passes(self, tmp_path: Path) -> None:
        findings = make_findings("DJS-001", 20)
        entries = [entry(f, Verdict.TRUE_POSITIVE) for f in findings[:19]]
        entries.append(entry(findings[19], Verdict.FALSE_POSITIVE))
        triage = Triage(target="t", entries=tuple(entries))

        report = compare(make_result(findings, tmp_path), triage)

        assert report.scores[0].false_positive_rate == pytest.approx(0.05)
        assert report.over_budget == []
        assert report.ok is True

    def test_budget_is_configurable_per_target(self, tmp_path: Path) -> None:
        findings = make_findings("DJS-001", 20)
        entries = [entry(f, Verdict.TRUE_POSITIVE) for f in findings[:19]]
        entries.append(entry(findings[19], Verdict.FALSE_POSITIVE))
        triage = Triage(target="t", entries=tuple(entries), max_false_positive_rate=0.01)

        report = compare(make_result(findings, tmp_path), triage)

        assert [s.family for s in report.over_budget] == ["DJS"]
        assert report.ok is False

    def test_accepted_risk_counts_toward_precision(self, tmp_path: Path) -> None:
        findings = make_findings("DJS-001", 2)
        triage = Triage(
            target="t",
            entries=(
                entry(findings[0], Verdict.TRUE_POSITIVE),
                entry(findings[1], Verdict.ACCEPTED_RISK),
            ),
        )

        report = compare(make_result(findings, tmp_path), triage)

        assert report.precision == 1.0
        assert report.scores[0].false_positive_rate == 0.0


class TestCrashes:
    def test_rule_crash_fails_the_gate(self, tmp_path: Path) -> None:
        # A rule that throws on a real repository would throw on a user's too.
        result = make_result([], tmp_path, rule_errors={"DJS-001": "boom"})

        report = compare(result, Triage(target="t"))

        assert report.rule_errors == {"DJS-001": "boom"}
        assert report.ok is False


class TestPrecisionDisplay:
    def test_precision_is_not_claimed_when_nothing_is_scored(self, tmp_path) -> None:
        # A wholly untriaged run must not advertise 100% precision.
        report = compare(make_result([make_finding("DJS-001")], tmp_path), Triage(target="t"))

        assert report.scored == 0
        assert report.precision_display == "not measured"
        assert "precision not measured" in report.summary()

    def test_precision_is_shown_once_findings_are_judged(self, tmp_path) -> None:
        findings = make_findings("DJS-001", 2)
        triage = Triage(
            target="t",
            entries=(
                entry(findings[0], Verdict.TRUE_POSITIVE),
                entry(findings[1], Verdict.FALSE_POSITIVE),
            ),
        )

        report = compare(make_result(findings, tmp_path), triage)

        assert report.scored == 2
        assert report.precision_display == "50.0%"
