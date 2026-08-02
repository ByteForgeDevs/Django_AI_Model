"""CLI contract: exit codes and output routing are what CI depends on."""

import json

import pytest
from typer.testing import CliRunner

from djaudit.baseline import Baseline
from djaudit.cli import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, app

runner = CliRunner()


class TestExitCodes:
    def test_findings_at_or_above_fail_on_exit_one(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project)])
        assert result.exit_code == EXIT_FINDINGS

    def test_raising_fail_on_above_the_worst_finding_exits_zero(self, overridden_project):
        result = runner.invoke(app, ["run", str(overridden_project)])
        assert result.exit_code == EXIT_OK

    def test_findings_below_fail_on_still_exit_zero(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project), "--ignore", "DJS-001"])
        assert result.exit_code == EXIT_OK

    def test_a_bad_path_is_a_tool_error_not_a_finding(self, tmp_path):
        """CI must distinguish 'found problems' from 'installation is broken'."""
        result = runner.invoke(app, ["run", str(tmp_path / "nope")])
        assert result.exit_code == EXIT_ERROR

    def test_a_file_instead_of_a_directory_is_a_tool_error(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1")
        assert runner.invoke(app, ["run", str(target)]).exit_code == EXIT_ERROR

    def test_an_unreadable_baseline_is_a_tool_error(self, vulnerable_project, tmp_path):
        bad = tmp_path / "baseline.json"
        bad.write_text("{nope")
        result = runner.invoke(app, ["run", str(vulnerable_project), "--baseline", str(bad)])
        assert result.exit_code == EXIT_ERROR


class TestOutputFormats:
    def test_json_output_is_parseable(self, vulnerable_project, tmp_path):
        out = tmp_path / "out.json"
        runner.invoke(app, ["run", str(vulnerable_project), "-f", "json", "-o", str(out)])
        assert len(json.loads(out.read_text())["findings"]) == 2

    def test_sarif_output_is_parseable(self, vulnerable_project, tmp_path):
        out = tmp_path / "out.sarif"
        runner.invoke(app, ["run", str(vulnerable_project), "-f", "sarif", "-o", str(out)])
        assert json.loads(out.read_text())["version"] == "2.1.0"

    @pytest.mark.parametrize("fmt", ["terminal", "json", "sarif"])
    def test_output_parent_directories_are_created(self, vulnerable_project, tmp_path, fmt):
        """Every format must behave the same here.

        Terminal previously opened the file directly while JSON and SARIF
        created parents first, so `-o reports/out.txt` failed on a fresh
        checkout for one format out of three.
        """
        out = tmp_path / "reports" / "nested" / f"out.{fmt}"
        result = runner.invoke(app, ["run", str(vulnerable_project), "-f", fmt, "-o", str(out)])
        assert result.exit_code in {EXIT_OK, EXIT_FINDINGS}, result.output
        assert out.is_file()
        assert out.read_text().strip()

    def test_terminal_file_output_names_the_rule(self, vulnerable_project, tmp_path):
        out = tmp_path / "reports" / "out.txt"
        runner.invoke(app, ["run", str(vulnerable_project), "-f", "terminal", "-o", str(out)])
        assert "DJS-001" in out.read_text()

    def test_terminal_output_names_the_rule_and_location(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project)])
        assert "DJS-001" in result.output
        assert "production.py" in result.output


class TestBaselineWorkflow:
    def test_writing_a_baseline_then_running_reports_nothing_new(
        self, vulnerable_project, tmp_path
    ):
        baseline = tmp_path / "baseline.json"

        written = runner.invoke(
            app, ["run", str(vulnerable_project), "--write-baseline", str(baseline)]
        )
        assert written.exit_code == EXIT_OK

        second = runner.invoke(app, ["run", str(vulnerable_project), "--baseline", str(baseline)])
        assert second.exit_code == EXIT_OK
        assert "No findings" in second.output

    def test_baseline_captures_findings_hidden_by_default_thresholds(
        self, overridden_project, tmp_path
    ):
        """Otherwise lowering a threshold later resurfaces old findings as 'new'."""
        baseline = tmp_path / "baseline.json"
        runner.invoke(app, ["run", str(overridden_project), "--write-baseline", str(baseline)])
        assert len(Baseline.load(baseline)) == 1


class TestRuleSelection:
    def test_select_restricts_to_named_rules(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project), "--select", "DJS-001"])
        assert "DJS-001" in result.output

    def test_family_filter_can_silence_everything(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project), "--family", "DJM"])
        assert result.exit_code == EXIT_OK
        assert "No findings" in result.output


class TestOtherCommands:
    def test_rules_lists_the_catalogue(self):
        result = runner.invoke(app, ["rules"])
        assert result.exit_code == EXIT_OK
        assert "DJS-001" in result.output

    def test_version_prints_a_version(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == EXIT_OK
        assert result.output.strip()

    def test_eval_passes_on_the_fixtures(self, vulnerable_project):
        result = runner.invoke(app, ["eval", str(vulnerable_project)])
        assert result.exit_code == EXIT_OK
        assert "evaluation passed" in result.output

    def test_eval_fails_loudly_on_a_regression(self, vulnerable_project, tmp_path):
        manifest = tmp_path / "expected.json"
        manifest.write_text(json.dumps({"expected": []}))
        result = runner.invoke(app, ["eval", str(vulnerable_project), "--manifest", str(manifest)])
        assert result.exit_code == EXIT_FINDINGS
        assert "UNEXPECTED" in result.output
