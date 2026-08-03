"""The eval harness is the regression gate; it must itself be trustworthy."""

import json

import pytest

from djaudit.evaluation import Expectation, Manifest, ManifestError, evaluate
from djaudit.models import Confidence, Severity

ONLY_DEBUG = {"DJS-001"}
"""Scoring arithmetic is the subject here, so the run is pinned to one rule.
Otherwise every rule added to the catalogue changes these numbers and the test
teaches us to edit the expected value rather than read the failure."""


class TestRegressionGate:
    """These two assertions are the actual CI gate for rule quality."""

    def test_vulnerable_fixture_scores_perfectly(self, vulnerable_project):
        report = evaluate(vulnerable_project)
        assert report.passed, "\n".join(report.failures())
        assert report.precision == 1.0
        assert report.recall == 1.0

    def test_overridden_fixture_scores_perfectly(self, overridden_project):
        report = evaluate(overridden_project)
        assert report.passed, "\n".join(report.failures())
        assert report.precision == 1.0
        assert report.recall == 1.0


def line_of(project, relpath: str, needle: str) -> int:
    """The line a planted defect actually sits on, rather than a pinned number.

    These tests are about the evaluator, not about the fixture's layout, and
    hardcoded line numbers meant every planted defect added to the fixture
    broke three tests that had nothing to do with it.
    """
    for number, line in enumerate((project / relpath).read_text().splitlines(), 1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} is not in {relpath}")


PRODUCTION = "config/settings/production.py"
BASE = "config/settings/base.py"


class TestScoring:
    def test_a_missed_expectation_is_a_false_negative(self, tmp_path, vulnerable_project):
        debug_line = line_of(vulnerable_project, PRODUCTION, "DEBUG = True")
        base_debug_line = line_of(vulnerable_project, BASE, "DEBUG = True")
        manifest = tmp_path / "expected.json"
        manifest.write_text(
            json.dumps(
                {
                    "expected": [
                        {"rule_id": "DJS-001", "file": PRODUCTION, "line": debug_line},
                        {"rule_id": "DJS-001", "file": BASE, "line": base_debug_line},
                        {"rule_id": "DJP-001", "file": "app/models.py", "line": 1},
                    ]
                }
            )
        )
        report = evaluate(vulnerable_project, manifest, include=ONLY_DEBUG)

        assert not report.passed
        assert report.false_negatives == 1
        assert report.recall == pytest.approx(2 / 3)
        assert any("MISSED DJP-001" in line for line in report.failures())

    def test_an_unlisted_finding_is_a_false_positive(self, tmp_path, vulnerable_project):
        debug_line = line_of(vulnerable_project, PRODUCTION, "DEBUG = True")
        manifest = tmp_path / "expected.json"
        manifest.write_text(
            json.dumps(
                {"expected": [{"rule_id": "DJS-001", "file": PRODUCTION, "line": debug_line}]}
            )
        )
        report = evaluate(vulnerable_project, manifest, include=ONLY_DEBUG)

        assert not report.passed
        assert report.false_positives == 1
        assert report.precision == 0.5
        assert any("UNEXPECTED" in line for line in report.failures())

    def test_wrong_grading_is_caught_even_when_the_location_is_right(
        self, tmp_path, vulnerable_project
    ):
        """Grading drift is the failure mode a location-only check would miss."""
        debug_line = line_of(vulnerable_project, PRODUCTION, "DEBUG = True")
        base_debug_line = line_of(vulnerable_project, BASE, "DEBUG = True")
        manifest = tmp_path / "expected.json"
        manifest.write_text(
            json.dumps(
                {
                    "expected": [
                        {
                            "rule_id": "DJS-001",
                            "file": PRODUCTION,
                            "line": debug_line,
                            "severity": "low",
                        },
                        {"rule_id": "DJS-001", "file": BASE, "line": base_debug_line},
                    ]
                }
            )
        )
        report = evaluate(vulnerable_project, manifest)

        assert not report.passed
        assert len(report.misgraded) == 1
        assert any("MISGRADED" in line for line in report.failures())

    def test_control_case_hits_are_reported_as_false_positives(self, tmp_path, vulnerable_project):
        manifest = tmp_path / "expected.json"
        manifest.write_text(
            json.dumps(
                {
                    "expected": [],
                    "must_not_report": [
                        {"rule_id": "DJS-001", "file": "config/settings/production.py"}
                    ],
                }
            )
        )
        report = evaluate(vulnerable_project, manifest)

        assert not report.passed
        assert len(report.forbidden) == 1
        assert report.failures()[0].startswith("FALSE POSITIVE on a control case")


class TestExpectationMatching:
    def test_line_is_optional(self):
        assert Expectation.from_dict({"rule_id": "DJS-001", "file": "a.py"}).line is None

    def test_grades_wildcard_when_unspecified(self, vulnerable_project):
        from djaudit import engine

        finding = engine.run(vulnerable_project, min_severity=Severity.INFO).findings[0]
        assert Expectation(rule_id="DJS-001", file=finding.location.file).grades(finding)

    def test_grades_rejects_a_mismatch(self, vulnerable_project):
        from djaudit import engine

        finding = engine.run(vulnerable_project, min_severity=Severity.INFO).findings[0]
        expectation = Expectation(
            rule_id="DJS-001", file=finding.location.file, confidence=Confidence.TENTATIVE
        )
        assert not expectation.grades(finding)


class TestManifestErrors:
    def test_missing_manifest_is_reported_clearly(self, tmp_path):
        with pytest.raises(ManifestError, match="not found"):
            Manifest.load(tmp_path / "expected.json")

    def test_invalid_json_is_reported_clearly(self, tmp_path):
        path = tmp_path / "expected.json"
        path.write_text("{oops")
        with pytest.raises(ManifestError, match="not valid JSON"):
            Manifest.load(path)

    def test_malformed_expectation_is_reported_clearly(self, tmp_path):
        path = tmp_path / "expected.json"
        path.write_text(json.dumps({"expected": [{"file": "a.py"}]}))
        with pytest.raises(ManifestError, match="invalid expectation"):
            Manifest.load(path)
