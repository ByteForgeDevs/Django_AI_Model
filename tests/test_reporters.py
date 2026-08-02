"""SARIF is the CI integration; malformed output silently breaks code scanning."""

import json

from djaudit import engine
from djaudit.fingerprint import FINGERPRINT_VERSION
from djaudit.models import Confidence, Severity
from djaudit.reporters import json_reporter, sarif


def run(project):
    return engine.run(project, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE)


class TestEnvelope:
    def test_declares_sarif_210(self, vulnerable_project):
        doc = sarif.build(run(vulnerable_project))
        assert doc["version"] == "2.1.0"
        assert doc["$schema"].endswith(".json")
        assert len(doc["runs"]) == 1

    def test_output_is_valid_json(self, vulnerable_project):
        assert json.loads(sarif.render(run(vulnerable_project)))

    def test_driver_identifies_the_tool(self, vulnerable_project):
        driver = sarif.build(run(vulnerable_project))["runs"][0]["tool"]["driver"]
        assert driver["name"] == "djaudit"
        assert driver["version"]
        assert driver["informationUri"]


class TestRuleDescriptors:
    def test_one_descriptor_per_distinct_rule(self, vulnerable_project):
        run_data = sarif.build(run(vulnerable_project))["runs"][0]
        ids = [r["id"] for r in run_data["tool"]["driver"]["rules"]]
        assert ids == sorted(set(ids))

    def test_result_indices_point_at_the_right_descriptor(self, vulnerable_project):
        run_data = sarif.build(run(vulnerable_project))["runs"][0]
        rules = run_data["tool"]["driver"]["rules"]
        for result in run_data["results"]:
            assert rules[result["ruleIndex"]]["id"] == result["ruleId"]

    def test_carries_security_severity_for_github_ranking(self, vulnerable_project):
        rules = sarif.build(run(vulnerable_project))["runs"][0]["tool"]["driver"]["rules"]
        for rule in rules:
            assert float(rule["properties"]["security-severity"]) > 0

    def test_confidence_maps_to_sarif_precision(self, vulnerable_project):
        rules = sarif.build(run(vulnerable_project))["runs"][0]["tool"]["driver"]["rules"]
        assert all(
            r["properties"]["precision"] in {"very-high", "high", "medium", "low"} for r in rules
        )

    def test_help_markdown_includes_remediation(self, vulnerable_project):
        rules = sarif.build(run(vulnerable_project))["runs"][0]["tool"]["driver"]["rules"]
        assert "Remediation" in rules[0]["help"]["markdown"]


class TestResults:
    def test_severity_maps_to_sarif_levels(self, vulnerable_project):
        results = sarif.build(run(vulnerable_project))["runs"][0]["results"]
        assert {r["level"] for r in results} <= {"error", "warning", "note"}

    def test_locations_are_repo_relative_with_a_base_id(self, vulnerable_project):
        results = sarif.build(run(vulnerable_project))["runs"][0]["results"]
        for result in results:
            artifact = result["locations"][0]["physicalLocation"]["artifactLocation"]
            assert not artifact["uri"].startswith("/")
            assert artifact["uriBaseId"] == sarif.SRCROOT

    def test_regions_are_one_based_and_well_formed(self, vulnerable_project):
        results = sarif.build(run(vulnerable_project))["runs"][0]["results"]
        for result in results:
            region = result["locations"][0]["physicalLocation"]["region"]
            assert region["startLine"] >= 1
            assert region["startColumn"] >= 1
            assert region.get("endLine", region["startLine"]) >= region["startLine"]

    def test_partial_fingerprints_let_github_track_alerts(self, vulnerable_project):
        results = sarif.build(run(vulnerable_project))["runs"][0]["results"]
        for result in results:
            assert result["partialFingerprints"][FINGERPRINT_VERSION]

    def test_base_id_is_a_file_uri(self, vulnerable_project):
        run_data = sarif.build(run(vulnerable_project))["runs"][0]
        assert run_data["originalUriBaseIds"][sarif.SRCROOT]["uri"].startswith("file://")


class TestInvocations:
    def test_clean_run_reports_success(self, vulnerable_project):
        invocation = sarif.build(run(vulnerable_project))["runs"][0]["invocations"][0]
        assert invocation["executionSuccessful"] is True
        assert invocation["toolExecutionNotifications"] == []

    def test_rule_failures_are_surfaced_in_the_sarif(self, vulnerable_project):
        result = run(vulnerable_project)
        result.rule_errors["DJP-001"] = "RuntimeError: boom"

        invocation = sarif.build(result)["runs"][0]["invocations"][0]

        assert invocation["executionSuccessful"] is False
        assert "boom" in invocation["toolExecutionNotifications"][0]["message"]["text"]


class TestJsonReporter:
    def test_includes_schema_version_and_summary(self, vulnerable_project):
        payload = json_reporter.build(run(vulnerable_project))
        assert payload["schema_version"] >= 1
        assert payload["summary"]["reported"] == len(payload["findings"])
        assert payload["project"]["django_version"] == "6.0.7"

    def test_settings_modules_are_reported_with_roles(self, vulnerable_project):
        payload = json_reporter.build(run(vulnerable_project))
        roles = {m["role"] for m in payload["project"]["settings_modules"]}
        assert roles == {"base", "production", "development"}

    def test_output_is_valid_json(self, vulnerable_project):
        assert json.loads(json_reporter.render(run(vulnerable_project)))
