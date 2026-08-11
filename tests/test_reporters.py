"""SARIF is the CI integration; malformed output silently breaks code scanning."""

import json
from dataclasses import replace

from djaudit import engine, registry
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

    def test_descriptor_uses_rule_defaults_not_a_downgraded_instance(self, overridden_project):
        """A descriptor describes the rule, not one occurrence of it.

        The overridden project reports DJS-001 downgraded to low/tentative,
        because production disables DEBUG. Deriving the descriptor from that
        finding would publish DJS-001 to GitHub as a low-severity rule for
        every repository, so the descriptor must come from RuleMeta.
        """
        result = run(overridden_project)
        finding = next(f for f in result.findings if f.rule_id == "DJS-001")
        assert finding.severity is Severity.LOW

        rule = sarif.build(result)["runs"][0]["tool"]["driver"]["rules"][0]
        meta = registry.get("DJS-001").meta
        assert rule["defaultConfiguration"]["level"] == "error"
        assert float(rule["properties"]["security-severity"]) == meta.severity.security_severity

    def test_per_finding_severity_still_surfaces_on_the_result(self, overridden_project):
        """Grading is not lost by the change above -- it lives on the result."""
        results = sarif.build(run(overridden_project))["runs"][0]["results"]
        downgraded = next(r for r in results if r["ruleId"] == "DJS-001")
        assert downgraded["level"] == "note"

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
        assert [n for n in invocation["toolExecutionNotifications"] if n["level"] == "error"] == []

    def test_a_static_run_notes_what_it_could_not_check(self, vulnerable_project):
        """Fewer findings look exactly like a cleaner codebase, so say so."""
        invocation = sarif.build(run(vulnerable_project))["runs"][0]["invocations"][0]

        (notice,) = [
            n
            for n in invocation["toolExecutionNotifications"]
            if n.get("descriptor", {}).get("id") == "djaudit/degraded"
        ]
        assert notice["level"] == "note"
        assert "live-tier" in notice["message"]["text"]
        # The invocation is still a success: narrower is not broken.
        assert invocation["executionSuccessful"] is True

    def test_a_failed_live_request_warns_rather_than_notes(self, vulnerable_project):
        """The contrast. Asking for the live tier and not getting it is actionable.

        Without this, the level could be hardcoded to `note` and the test above
        would not notice -- an alarm that never rises is the failure the
        distinction exists to prevent.
        """
        result = run(vulnerable_project)
        result.context = replace(result.context, live_problem="no virtualenv was found")

        invocation = sarif.build(result)["runs"][0]["invocations"][0]

        (notice,) = [
            n
            for n in invocation["toolExecutionNotifications"]
            if n.get("descriptor", {}).get("id") == "djaudit/degraded"
        ]
        assert notice["level"] == "warning"

    def test_rule_failures_are_surfaced_in_the_sarif(self, vulnerable_project):
        result = run(vulnerable_project)
        result.rule_errors["DJP-001"] = "RuntimeError: boom"

        invocation = sarif.build(result)["runs"][0]["invocations"][0]

        assert invocation["executionSuccessful"] is False
        assert "boom" in invocation["toolExecutionNotifications"][0]["message"]["text"]

    def test_associated_rule_resolves_to_a_real_descriptor(self, vulnerable_project):
        """A dangling reportingDescriptorReference is invalid SARIF.

        A rule that crashes before emitting anything has no finding, so it
        would not appear in tool.driver.rules -- leaving associatedRule
        pointing at a descriptor that does not exist. Consumers may reject the
        whole file, which would take the real findings down with it.
        """
        result = run(vulnerable_project)
        result.rule_errors["DJP-001"] = "RuntimeError: boom"
        assert not any(f.rule_id == "DJP-001" for f in result.findings)

        run_data = sarif.build(result)["runs"][0]
        rules = run_data["tool"]["driver"]["rules"]
        notification = run_data["invocations"][0]["toolExecutionNotifications"][0]
        associated = notification["associatedRule"]

        assert associated["id"] in {r["id"] for r in rules}
        assert rules[associated["index"]]["id"] == associated["id"]

    def test_result_indices_survive_a_crashed_rule_being_added(self, vulnerable_project):
        """Adding crash descriptors must not shift results onto the wrong rule."""
        result = run(vulnerable_project)
        result.rule_errors["DJA-001"] = "RuntimeError: boom"

        run_data = sarif.build(result)["runs"][0]
        rules = run_data["tool"]["driver"]["rules"]
        for sarif_result in run_data["results"]:
            assert rules[sarif_result["ruleIndex"]]["id"] == sarif_result["ruleId"]

    def test_unregistered_crashed_rule_still_gets_a_descriptor(self, vulnerable_project):
        """External adapters in Phase 5 will report ids we never registered."""
        result = run(vulnerable_project)
        result.rule_errors["ZZZ-999"] = "RuntimeError: boom"

        run_data = sarif.build(result)["runs"][0]
        rules = run_data["tool"]["driver"]["rules"]
        notification = run_data["invocations"][0]["toolExecutionNotifications"][0]

        assert rules[notification["associatedRule"]["index"]]["id"] == "ZZZ-999"


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

    def test_the_report_says_what_the_run_could_not_check(self, vulnerable_project):
        """A consumer that never sees the terminal still has to learn this.

        The block is what lets a pipeline tell a clean project from an
        unreachable one; without it a static-only run and a fully-checked run
        are byte-identical apart from the findings they happen to contain.
        """
        degraded = json_reporter.build(run(vulnerable_project))["degraded"]

        assert degraded is not None
        assert "live tier" in degraded["reason"]
        assert degraded["skipped"], "live rules were skipped but none were named"
        for item in degraded["skipped"]:
            assert item["rule_id"] and item["fallback"], item

    def test_a_covering_fallback_is_named(self, vulnerable_project):
        """The fallback is the actionable half: what still covers the gap."""
        degraded = json_reporter.build(run(vulnerable_project))["degraded"]

        covered = {s["rule_id"]: s["covered_by"] for s in degraded["skipped"]}
        assert any(covered.values()), f"no skipped rule named a fallback that ran: {covered}"

    def test_a_complete_run_says_so_rather_than_going_quiet(self, vulnerable_project):
        """The contrast, and the reason the block is not omitted when empty.

        An absent key would be ambiguous -- old djaudit, or nothing skipped? --
        so a run with nothing to report still states the reason and an empty
        list. Without this, `_degraded` could return `None` whenever `skipped`
        was empty and every other test here would still pass.
        """
        result = run(vulnerable_project)
        result.degraded = result.degraded._replace(skipped=())

        degraded = json_reporter.build(result)["degraded"]

        assert degraded is not None, "a complete run must still say it was complete"
        assert degraded["skipped"] == []
        assert degraded["reason"]
