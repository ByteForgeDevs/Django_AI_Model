"""Engine behaviour: filtering, suppression, baselines and rule isolation."""

import shutil
from collections.abc import Iterator

import pytest

from djaudit import engine
from djaudit.baseline import Baseline
from djaudit.context import ProjectContext
from djaudit.models import Confidence, Family, Finding, Severity, Tier
from djaudit.registry import Rule, RuleMeta

ONLY_DEBUG = {"DJS-001"}
"""These tests are about engine mechanics -- thresholds, baselines, isolation --
and only borrow a fixture to have something to filter. Pinning them to one rule
keeps them from breaking every time the catalogue grows, which would train us to
update the number rather than read the failure."""


class TestRunAgainstFixtures:
    def test_reports_both_planted_defects(self, vulnerable_project):
        result = engine.run(
            vulnerable_project,
            include=ONLY_DEBUG,
            min_severity=Severity.INFO,
            min_confidence=Confidence.TENTATIVE,
        )
        assert [(f.location.file, f.severity, f.confidence) for f in result.findings] == [
            ("config/settings/production.py", Severity.CRITICAL, Confidence.CERTAIN),
            ("config/settings/base.py", Severity.HIGH, Confidence.FIRM),
        ]

    def test_development_settings_are_never_reported(self, vulnerable_project):
        result = engine.run(
            vulnerable_project,
            min_severity=Severity.INFO,
            min_confidence=Confidence.TENTATIVE,
        )
        assert not any("development" in f.location.file for f in result.findings)

    def test_findings_are_sorted_worst_first(self, vulnerable_project):
        result = engine.run(vulnerable_project, min_severity=Severity.INFO)
        assert result.worst_severity is Severity.CRITICAL
        assert result.findings[0].severity is Severity.CRITICAL

    def test_every_finding_carries_evidence_and_a_fingerprint(self, vulnerable_project):
        result = engine.run(vulnerable_project, min_severity=Severity.INFO)
        assert result.findings
        for finding in result.findings:
            assert finding.evidence, f"{finding.rule_id} shipped an opinion, not evidence"
            assert len(finding.fingerprint) == 16


class TestThresholds:
    def test_severity_threshold_hides_and_counts(self, vulnerable_project):
        result = engine.run(vulnerable_project, include=ONLY_DEBUG, min_severity=Severity.CRITICAL)
        assert len(result.findings) == 1
        assert result.filtered_threshold == 1

    def test_confidence_threshold_hides_and_counts(self, overridden_project):
        result = engine.run(
            overridden_project,
            include=ONLY_DEBUG,
            min_severity=Severity.INFO,
            min_confidence=Confidence.FIRM,
        )
        assert result.findings == []
        assert result.filtered_threshold == 1

    def test_totals_survive_filtering_so_silence_is_explainable(self, vulnerable_project):
        result = engine.run(vulnerable_project, include=ONLY_DEBUG, min_severity=Severity.CRITICAL)
        assert result.total_raw == 2


class TestSelection:
    def test_selecting_an_unrelated_family_runs_nothing(self, vulnerable_project):
        result = engine.run(vulnerable_project, families={Family.DJM})
        assert result.rules_run == 0
        assert result.findings == []

    def test_ignoring_a_rule_silences_it(self, vulnerable_project):
        result = engine.run(vulnerable_project, exclude={"DJS-001"}, min_severity=Severity.INFO)
        assert not any(f.rule_id == "DJS-001" for f in result.findings)

    def test_live_tier_rules_are_skipped_without_a_live_context(self, vulnerable_project):
        result = engine.run(vulnerable_project, tiers={Tier.LIVE})
        assert result.rules_run == 0


class TestSuppression:
    def test_inline_comment_suppresses_and_is_counted(self, vulnerable_project, tmp_path):
        project = tmp_path / "project"
        shutil.copytree(vulnerable_project, project)
        target = project / "config" / "settings" / "production.py"
        target.write_text(
            target.read_text().replace("DEBUG = True", "DEBUG = True  # noqa: DJS-001")
        )

        result = engine.run(project, min_severity=Severity.INFO)

        assert result.suppressed_inline == 1
        assert not any("production" in f.location.file for f in result.findings)

    def test_another_tools_noqa_does_not_suppress(self, vulnerable_project, tmp_path):
        project = tmp_path / "project"
        shutil.copytree(vulnerable_project, project)
        target = project / "config" / "settings" / "production.py"
        target.write_text(target.read_text().replace("DEBUG = True", "DEBUG = True  # noqa: E501"))

        result = engine.run(project, min_severity=Severity.INFO)

        assert result.suppressed_inline == 0
        assert any("production" in f.location.file for f in result.findings)


class TestBaselineIntegration:
    def test_baselined_findings_are_hidden_but_counted(self, vulnerable_project):
        first = engine.run(vulnerable_project, include=ONLY_DEBUG, min_severity=Severity.INFO)
        baseline = Baseline.from_findings(first.findings)

        second = engine.run(
            vulnerable_project,
            include=ONLY_DEBUG,
            min_severity=Severity.INFO,
            baseline=baseline,
        )

        assert second.findings == []
        assert second.suppressed_baseline == 2

    def test_new_findings_still_surface_through_a_baseline(self, vulnerable_project):
        first = engine.run(vulnerable_project, include=ONLY_DEBUG, min_severity=Severity.INFO)
        partial = Baseline.from_findings(first.findings[:1])

        second = engine.run(
            vulnerable_project,
            include=ONLY_DEBUG,
            min_severity=Severity.INFO,
            baseline=partial,
        )

        assert len(second.findings) == 1
        assert second.suppressed_baseline == 1


class ExplodingRule(Rule):
    meta = RuleMeta(
        id="DJX-999",
        title="deliberately broken rule",
        family=Family.DJX,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale="r",
        remediation="fix",
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        raise RuntimeError("boom")


class TestRuleIsolation:
    def test_a_crashing_rule_does_not_abort_the_run(self, vulnerable_project, monkeypatch):
        """One odd construct in a large codebase must not cost every other rule."""
        from djaudit.registry import all_rules

        monkeypatch.setattr(engine, "select", lambda **kwargs: [ExplodingRule, *all_rules()])

        result = engine.run(vulnerable_project, min_severity=Severity.INFO)

        assert "DJX-999" in result.rule_errors
        assert "boom" in result.rule_errors["DJX-999"]
        # The surviving rules still produced their findings, which is the point:
        # one bad rule must not cost the user every other rule's results.
        assert {"DJS-001", "DJS-002"} <= {f.rule_id for f in result.findings}


class TestEmptyProject:
    def test_a_non_django_directory_yields_nothing_and_does_not_crash(self, tmp_path):
        (tmp_path / "script.py").write_text("print('hello')\n")
        result = engine.run(tmp_path)
        assert result.findings == []
        assert result.context.settings_modules == ()


@pytest.mark.parametrize("severity", list(Severity))
def test_counts_by_severity_covers_every_level(vulnerable_project, severity):
    result = engine.run(vulnerable_project, min_severity=Severity.INFO)
    assert severity in result.counts_by_severity()
