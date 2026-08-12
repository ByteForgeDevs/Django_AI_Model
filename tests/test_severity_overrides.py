"""Tests for per-rule severity overrides.

`TestItIsNotDecoration` is the substep's actual claim. An override applied
after the threshold filter could only relabel findings the threshold had
already decided about -- the output would contain the same findings wearing
different words. Applied before, it changes what is reported and what fails the
build, in both directions. Those are the numbers that tell the two designs
apart, so they are asserted rather than described.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from djaudit.cli import app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
runner = CliRunner()


@pytest.fixture
def orm(tmp_path: Path) -> Path:
    target = tmp_path / "orm_project"
    shutil.copytree(FIXTURES / "orm_project", target)
    return target


def report(project: Path, *args: str) -> dict[str, Any]:
    result = runner.invoke(app, ["run", str(project), "--format", "json", *args])
    assert result.exit_code in (0, 1), result.output
    data: dict[str, Any] = json.loads(result.stdout)
    return data


def configure(project: Path, body: str) -> None:
    (project / "pyproject.toml").write_text(body)


def severity_of(data: dict[str, Any], rule_id: str) -> list[str]:
    return [f["severity"] for f in data["findings"] if f["rule_id"] == rule_id]


class TestItRelabels:
    def test_the_fixture_ranks_these_rules_as_expected(self, orm: Path) -> None:
        """The premise every other test here rests on, asserted not assumed."""
        data = report(orm, "--min-severity", "info", "--min-confidence", "tentative")
        assert severity_of(data, "DJP-003") == ["high"]
        assert severity_of(data, "DJP-006") == ["low"]

    def test_an_override_changes_the_reported_severity(self, orm: Path) -> None:
        data = report(orm, "--min-severity", "info", "--severity", "DJP-003=low")
        assert severity_of(data, "DJP-003") == ["low"]

    def test_other_rules_are_untouched(self, orm: Path) -> None:
        data = report(orm, "--min-severity", "info", "--severity", "DJP-003=low")
        assert severity_of(data, "DJP-004") == ["high"]

    def test_the_flag_is_repeatable(self, orm: Path) -> None:
        data = report(
            orm,
            "--min-severity",
            "info",
            "--severity",
            "DJP-003=low",
            "--severity",
            "DJP-004=info",
        )
        assert severity_of(data, "DJP-003") == ["low"]
        assert severity_of(data, "DJP-004") == ["info"]

    def test_a_lowercase_rule_id_still_works(self, orm: Path) -> None:
        """Otherwise `djp-003=low` is a setting that silently does nothing."""
        data = report(orm, "--min-severity", "info", "--severity", "djp-003=low")
        assert severity_of(data, "DJP-003") == ["low"]


class TestItIsNotDecoration:
    """Applied before the threshold, or it cannot change any of these."""

    def test_raising_a_rule_brings_it_above_the_threshold(self, orm: Path) -> None:
        before = report(orm, "--min-severity", "high")
        assert not severity_of(before, "DJP-006"), "DJP-006 is low; it should be filtered out"
        after = report(orm, "--min-severity", "high", "--severity", "DJP-006=critical")
        assert severity_of(after, "DJP-006") == ["critical"]
        assert len(after["findings"]) == len(before["findings"]) + 1

    def test_lowering_a_rule_drops_it_below_the_threshold(self, orm: Path) -> None:
        before = report(orm, "--min-severity", "high")
        assert severity_of(before, "DJP-003") == ["high"]
        after = report(orm, "--min-severity", "high", "--severity", "DJP-003=low")
        assert not severity_of(after, "DJP-003")
        assert len(after["findings"]) == len(before["findings"]) - 1

    def test_an_override_can_fail_a_build_that_passed(self, orm: Path) -> None:
        plain = runner.invoke(app, ["run", str(orm), "--fail-on", "critical"])
        assert plain.exit_code == 0, "nothing here is critical without an override"
        raised = runner.invoke(
            app, ["run", str(orm), "--fail-on", "critical", "--severity", "DJP-003=critical"]
        )
        assert raised.exit_code == 1

    def test_an_override_can_pass_a_build_that_failed(self, orm: Path) -> None:
        plain = runner.invoke(app, ["run", str(orm), "--fail-on", "high"])
        assert plain.exit_code == 1
        lowered = runner.invoke(
            app,
            [
                "run",
                str(orm),
                "--fail-on",
                "high",
                "--severity",
                "DJP-003=low",
                "--severity",
                "DJP-004=low",
            ],
        )
        assert lowered.exit_code == 0


class TestBaselinesSurvive:
    """Severity is not in the fingerprint, and adopters depend on that."""

    def test_the_fingerprints_do_not_move(self, orm: Path) -> None:
        before = report(orm, "--min-severity", "info")
        after = report(orm, "--min-severity", "info", "--severity", "DJP-003=info")
        assert {f["fingerprint"] for f in before["findings"]} == {
            f["fingerprint"] for f in after["findings"]
        }

    def test_a_baseline_written_without_the_override_still_matches(self, orm: Path) -> None:
        path = orm / "baseline.json"
        written = runner.invoke(
            app, ["run", str(orm), "--min-severity", "info", "--write-baseline", str(path)]
        )
        assert written.exit_code in (0, 1), written.output
        after = report(
            orm,
            "--min-severity",
            "info",
            "--baseline",
            str(path),
            "--severity",
            "DJP-003=critical",
        )
        assert after["findings"] == [], "the override should not have invalidated the baseline"


class TestItIsVisible:
    def test_the_json_summary_counts_them(self, orm: Path) -> None:
        data = report(orm, "--min-severity", "info", "--severity", "DJP-003=low")
        assert data["summary"]["severity_overridden"] == 1

    def test_nothing_overridden_counts_zero(self, orm: Path) -> None:
        assert report(orm, "--min-severity", "info")["summary"]["severity_overridden"] == 0

    def test_the_terminal_says_so(self, orm: Path) -> None:
        result = runner.invoke(
            app, ["run", str(orm), "--min-severity", "info", "--severity", "DJP-003=low"]
        )
        assert "severity overridden" in result.output


class TestItRefusesNonsense:
    def test_an_unknown_rule_is_refused(self, orm: Path) -> None:
        """A typo'd id is a setting that silently does nothing, forever."""
        result = runner.invoke(app, ["run", str(orm), "--severity", "DJP-999=low"])
        assert result.exit_code == 2
        assert "unknown rule" in result.output

    def test_a_near_miss_is_suggested(self, orm: Path) -> None:
        result = runner.invoke(app, ["run", str(orm), "--severity", "DJP-00=low"])
        assert result.exit_code == 2
        assert "did you mean" in result.output

    def test_an_unknown_severity_is_refused(self, orm: Path) -> None:
        result = runner.invoke(app, ["run", str(orm), "--severity", "DJP-003=urgent"])
        assert result.exit_code == 2
        assert "unknown severity" in result.output

    @pytest.mark.parametrize("bad", ["DJP-003", "DJP-003=", "=low", ""])
    def test_a_malformed_pair_is_refused(self, orm: Path, bad: str) -> None:
        result = runner.invoke(app, ["run", str(orm), "--severity", bad])
        assert result.exit_code == 2
        assert "RULE=LEVEL" in result.output


class TestFromTheConfigFile:
    def test_the_file_can_state_overrides(self, orm: Path) -> None:
        configure(orm, '[tool.djaudit.severity]\n"DJP-003" = "low"\n')
        data = report(orm, "--min-severity", "info")
        assert severity_of(data, "DJP-003") == ["low"]
        assert data["summary"]["severity_overridden"] == 1

    def test_the_file_can_change_what_the_threshold_keeps(self, orm: Path) -> None:
        configure(orm, '[tool.djaudit.severity]\n"DJP-006" = "critical"\n')
        assert severity_of(report(orm, "--min-severity", "high"), "DJP-006") == ["critical"]

    def test_the_flag_beats_the_file(self, orm: Path) -> None:
        configure(orm, '[tool.djaudit.severity]\n"DJP-003" = "low"\n')
        data = report(orm, "--min-severity", "info", "--severity", "DJP-003=critical")
        assert severity_of(data, "DJP-003") == ["critical"]

    def test_a_lowercase_key_still_works(self, orm: Path) -> None:
        configure(orm, '[tool.djaudit.severity]\n"djp-003" = "low"\n')
        assert severity_of(report(orm, "--min-severity", "info"), "DJP-003") == ["low"]

    def test_an_unknown_rule_in_the_file_is_refused(self, orm: Path) -> None:
        configure(orm, '[tool.djaudit.severity]\n"DJP-999" = "low"\n')
        result = runner.invoke(app, ["run", str(orm)])
        assert result.exit_code == 2
        assert "unknown rule" in result.output

    def test_an_unknown_severity_in_the_file_is_refused(self, orm: Path) -> None:
        configure(orm, '[tool.djaudit.severity]\n"DJP-003" = "urgent"\n')
        result = runner.invoke(app, ["run", str(orm)])
        assert result.exit_code == 2
        assert "severity.DJP-003" in result.output

    def test_a_scalar_instead_of_a_table_is_refused(self, orm: Path) -> None:
        configure(orm, '[tool.djaudit]\nseverity = "low"\n')
        result = runner.invoke(app, ["run", str(orm)])
        assert result.exit_code == 2
        assert "table of rule id = severity" in result.output
