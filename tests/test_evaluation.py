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


class TestIncompleteRuns:
    """A fixture the analyser could not read must not score as a clean one.

    ``must_not_report`` entries are satisfied by silence, and a run where
    discovery failed is nothing but silence. So a manifest made entirely of
    control cases -- which is what a false-positive fixture is -- scored 100%
    precision, 100% recall and passed, on a project no rule ever looked at.
    ``djaudit run`` had refused to exit 0 on exactly this since Phase 1; the
    two scoring commands did not, so whether CI noticed depended on which
    command it called.

    The shape used here is django-configurations: settings assigned in a class
    body, which djaudit detects but cannot yet read. That is not hypothetical
    -- it is how a readthedocs-shaped project looks to us today, and Substep
    1.10.2 is the deferred work to support it.
    """

    def class_configured(self, tmp_path, manifest):
        (tmp_path / "manage.py").write_text("import os\n")
        (tmp_path / "settings.py").write_text(
            "from configurations import Configuration\n\n\n"
            "class Dev(Configuration):\n"
            "    DEBUG = True\n"
            "    SECRET_KEY = 'hunter2'\n"
            "    ALLOWED_HOSTS = ['*']\n"
        )
        (tmp_path / "expected.json").write_text(json.dumps(manifest))
        return tmp_path

    def test_a_control_only_manifest_no_longer_passes_by_silence(self, tmp_path):
        project = self.class_configured(
            tmp_path,
            {
                "description": "settings live in a class body, so nothing is read",
                "must_not_report": [{"rule_id": "DJS-001", "file": "settings.py"}],
            },
        )

        report = evaluate(project)

        assert report.passed is False
        assert [d.code for d in report.incomplete] == ["settings-in-class-body"]

    def test_the_scores_still_look_perfect_which_is_the_point(self, tmp_path):
        # Every number the harness computes says the fixture is clean. The
        # only thing that says otherwise is the diagnostic, so the diagnostic
        # has to be what decides.
        project = self.class_configured(
            tmp_path, {"must_not_report": [{"rule_id": "DJS-001", "file": "settings.py"}]}
        )

        report = evaluate(project)

        assert report.precision == 1.0
        assert report.recall == 1.0
        assert report.false_positives == 0
        assert report.false_negatives == 0
        assert report.passed is False

    def test_the_failure_is_reported_before_any_expectation(self, tmp_path):
        # An incomplete run invalidates every line below it, so it is read
        # first or it is read after the reader has already formed a view.
        project = self.class_configured(
            tmp_path,
            {
                "expected": [{"rule_id": "DJS-001", "file": "settings.py", "line": 5}],
                "must_not_report": [],
            },
        )

        failures = evaluate(project).failures()

        assert failures[0].startswith("INCOMPLETE settings-in-class-body")
        assert any(line.startswith("MISSED") for line in failures)

    def test_a_crashed_rule_also_fails_the_fixture(self, vulnerable_project, monkeypatch):
        # Same reasoning, different cause: a rule that threw reported nothing,
        # and nothing satisfies every control case in the manifest. Made to
        # crash for real rather than by assigning the field, so the wiring from
        # the engine through to the score is what gets tested.
        from djaudit import engine
        from djaudit.registry import all_rules
        from tests.test_engine import ExplodingRule

        assert evaluate(vulnerable_project).passed is True

        monkeypatch.setattr(engine, "select", lambda **kwargs: [ExplodingRule, *all_rules()])
        crashed = evaluate(vulnerable_project)

        assert crashed.rule_errors["DJX-999"].startswith("RuntimeError")
        assert crashed.passed is False
        assert crashed.failures()[0].startswith("RULE CRASHED DJX-999")

    def test_a_readable_project_reports_nothing_incomplete(self, vulnerable_project):
        report = evaluate(vulnerable_project)

        assert report.incomplete == []
        assert report.rule_errors == {}
        assert report.passed is True
