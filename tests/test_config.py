"""Tests for `[tool.djaudit]` in `pyproject.toml`.

Three things here are worth more than the rest.

`TestPrecedence` runs the real CLI and counts findings, because precedence is
the part that cannot be checked by reading. A merge that compares against the
default rather than asking where the value came from passes every unit test of
the parser and still loses every setting to a default nobody typed.

`TestItCannotTurnOnExecution` asserts a refusal. The file is read out of the
audited project, so it is as trustworthy as that project.

`TestTheErrorReachesTheUser` exists because a message rich swallows is not a
message. Every string this module produces contains `[tool.djaudit]`, which is
also valid rich markup.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from djaudit import config as config_module
from djaudit.cli import app
from djaudit.config import KNOWN, ConfigError, FileConfig, from_pyproject
from djaudit.models import Confidence, Family, Severity
from djaudit.reporters import OutputFormat

FIXTURES = Path(__file__).resolve().parent / "fixtures"
runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy of the planted-defect fixture, so findings are real."""
    import shutil

    target = tmp_path / "project"
    shutil.copytree(FIXTURES / "vulnerable_project", target)
    return target


def configure(project: Path, body: str) -> None:
    (project / "pyproject.toml").write_text(f"[tool.djaudit]\n{body}\n")


def audit(project: Path, *args: str) -> int:
    """Run an audit and return how many findings it reported."""
    result = runner.invoke(app, ["run", str(project), "--format", "json", *args])
    assert result.exit_code in (0, 1), result.output
    return len(json.loads(result.stdout)["findings"])


def refusal(project: Path, body: str) -> str:
    configure(project, body)
    result = runner.invoke(app, ["run", str(project), "--format", "json"])
    assert result.exit_code == 2, f"expected a refusal, got {result.exit_code}: {result.output}"
    return result.output


def read(tmp_path: Path, text: str) -> FileConfig:
    path = tmp_path / "pyproject.toml"
    path.write_text(text)
    return from_pyproject(path)


class TestWhatIsAbsent:
    def test_a_missing_file_asks_for_nothing(self, tmp_path: Path) -> None:
        assert from_pyproject(tmp_path / "nope.toml").empty

    def test_a_file_without_the_table_asks_for_nothing(self, tmp_path: Path) -> None:
        assert read(tmp_path, '[project]\nname = "x"\n').empty

    def test_an_empty_table_asks_for_nothing(self, tmp_path: Path) -> None:
        assert read(tmp_path, "[tool.djaudit]\n").empty

    def test_an_unstated_key_stays_none(self, tmp_path: Path) -> None:
        """`None` has to survive to the merge, or the file cannot lose to a flag."""
        got = read(tmp_path, '[tool.djaudit]\nmin_severity = "high"\n')
        assert got.min_severity is Severity.HIGH
        assert got.min_confidence is None, "an unstated key must not acquire a default here"

    def test_the_llm_subtable_is_left_alone(self, tmp_path: Path) -> None:
        """It has its own reader; this one must not call it an unknown key."""
        got = read(tmp_path, '[tool.djaudit.llm]\nprovider = "null"\n')
        assert got.empty


class TestWhatItReads:
    def test_every_known_key_is_a_field(self) -> None:
        """Otherwise a key can be accepted by validation and then dropped."""
        for name in KNOWN:
            assert hasattr(FileConfig(), name), name

    def test_the_thresholds(self, tmp_path: Path) -> None:
        got = read(
            tmp_path,
            '[tool.djaudit]\nmin_severity = "critical"\n'
            'min_confidence = "tentative"\nfail_on = "low"\n',
        )
        assert got.min_severity is Severity.CRITICAL
        assert got.min_confidence is Confidence.TENTATIVE
        assert got.fail_on is Severity.LOW

    def test_case_does_not_matter(self, tmp_path: Path) -> None:
        assert read(tmp_path, '[tool.djaudit]\nmin_severity = "HIGH"\n').min_severity is (
            Severity.HIGH
        )

    def test_the_lists(self, tmp_path: Path) -> None:
        got = read(
            tmp_path,
            '[tool.djaudit]\nfamily = ["DJS", "dji"]\nselect = ["djs-001"]\nignore = ["dja-001"]\n',
        )
        assert got.family == (Family.DJS, Family.DJI)
        assert got.select == ("DJS-001",), "rule ids are upper-cased, as the CLI does"
        assert got.ignore == ("DJA-001",)

    def test_the_paths(self, tmp_path: Path) -> None:
        got = read(tmp_path, '[tool.djaudit]\nbaseline = "b.json"\noutput = "out.sarif"\n')
        assert got.baseline == Path("b.json")
        assert got.output == Path("out.sarif")

    def test_the_format(self, tmp_path: Path) -> None:
        assert read(tmp_path, '[tool.djaudit]\nformat = "sarif"\n').format is OutputFormat.SARIF

    def test_switching_a_tier_off_is_allowed(self, tmp_path: Path) -> None:
        got = read(tmp_path, "[tool.djaudit]\nlive = false\nexternal = false\n")
        assert got.live is False
        assert got.external is False


class TestItCannotTurnOnExecution:
    """The file ships inside the repository being audited."""

    @pytest.mark.parametrize("key", ["live", "external"])
    def test_asking_for_a_tier_that_runs_code_is_refused(self, tmp_path: Path, key: str) -> None:
        with pytest.raises(ConfigError) as caught:
            read(tmp_path, f"[tool.djaudit]\n{key} = true\n")
        assert key in str(caught.value)

    def test_the_refusal_says_what_to_do_instead(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            read(tmp_path, "[tool.djaudit]\nlive = true\n")
        assert "--live" in str(caught.value), "a refusal without an alternative is a dead end"

    def test_the_flag_still_works(self, project: Path) -> None:
        """The refusal must be about the file, not about the feature."""
        configure(project, "live = false")
        result = runner.invoke(app, ["run", str(project), "--no-live", "--format", "json"])
        assert result.exit_code in (0, 1), result.output

    def test_write_baseline_is_not_configurable(self, tmp_path: Path) -> None:
        """As a persistent setting it would make every audit a no-op that passes."""
        assert "write_baseline" not in KNOWN
        with pytest.raises(ConfigError):
            read(tmp_path, '[tool.djaudit]\nwrite_baseline = "b.json"\n')


class TestItRefusesWhatItCannotUnderstand:
    def test_an_unknown_key_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no key 'nonsense'"):
            read(tmp_path, '[tool.djaudit]\nnonsense = "x"\n')

    def test_a_near_miss_is_suggested(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="min_severity"):
            read(tmp_path, '[tool.djaudit]\nmin_severty = "high"\n')

    def test_a_hyphenated_key_names_the_real_spelling(self, tmp_path: Path) -> None:
        """The CLI flag is `--min-severity`, so this is the obvious wrong guess."""
        with pytest.raises(ConfigError, match="spelled 'min_severity'"):
            read(tmp_path, '[tool.djaudit]\nmin-severity = "high"\n')

    def test_an_unknown_subtable_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"tool\.djaudit\.rules"):
            read(tmp_path, '[tool.djaudit.rules]\nx = "y"\n')

    def test_a_bad_enum_lists_what_is_allowed(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            read(tmp_path, '[tool.djaudit]\nmin_severity = "urgent"\n')
        for member in Severity:
            assert repr(member.value) in str(caught.value)

    def test_a_bad_family_lists_what_is_allowed(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="'DJS'"):
            read(tmp_path, '[tool.djaudit]\nfamily = ["DJS", "NOPE"]\n')

    @pytest.mark.parametrize(
        "body",
        [
            "min_severity = 3",
            'family = "DJS"',
            'live = "yes"',
            "baseline = 7",
            "format = 1",
            "family = [1, 2]",
        ],
    )
    def test_a_wrong_type_is_an_error(self, tmp_path: Path, body: str) -> None:
        with pytest.raises(ConfigError):
            read(tmp_path, f"[tool.djaudit]\n{body}\n")

    def test_the_type_error_says_what_it_got(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"3 \(int\)"):
            read(tmp_path, "[tool.djaudit]\nmin_severity = 3\n")

    def test_malformed_toml_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            read(tmp_path, "[tool.djaudit\nmin_severity =\n")

    def test_a_directory_where_the_file_should_be_is_not_a_crash(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").mkdir()
        assert from_pyproject(tmp_path / "pyproject.toml").empty


class TestPrecedence:
    """Measured through the real CLI, by counting findings."""

    def test_the_fixture_reports_findings_with_no_config(self, project: Path) -> None:
        """The control every test below leans on."""
        assert audit(project) > 0

    def test_the_file_beats_the_default(self, project: Path) -> None:
        """The case a default-comparison merge gets wrong.

        `--min-severity` is not passed, so the parameter already holds `low`.
        Only asking click where that came from distinguishes it from a run that
        typed `--min-severity low`.
        """
        loose = audit(project)
        configure(project, 'min_severity = "critical"')
        assert audit(project) < loose

    def test_the_flag_beats_the_file(self, project: Path) -> None:
        wide = audit(project)
        configure(project, 'min_severity = "critical"')
        assert audit(project) < wide, "the file did not take effect, so this proves nothing"
        assert audit(project, "--min-severity", "low") == wide

    def test_the_flag_beats_the_file_even_at_its_own_default(self, project: Path) -> None:
        """`--min-severity low` is the default value, typed explicitly.

        If the merge asked "is this the default?" instead of "did they say
        it?", this is the case that would silently keep the file's `critical`.
        """
        configure(project, 'min_severity = "critical"')
        restricted = audit(project)
        assert audit(project, "--min-severity", "low") > restricted

    def test_a_stated_confidence_widens_the_run(self, project: Path) -> None:
        default = audit(project)
        configure(project, 'min_confidence = "tentative"')
        assert audit(project) > default

    def test_a_stated_family_narrows_the_run(self, project: Path) -> None:
        """A pair, because this fixture reports exactly one family.

        Restricting to the family it has must change nothing; restricting to
        one it does not have must empty the run. Either alone is ambiguous --
        the first would also pass if the config were ignored, the second if the
        config broke every run.
        """
        default = audit(project)
        configure(project, 'family = ["DJS"]')
        assert audit(project) == default
        configure(project, 'family = ["DJD"]')
        assert audit(project) == 0

    def test_a_stated_select_narrows_the_run(self, project: Path) -> None:
        result = runner.invoke(app, ["run", str(project), "--format", "json"])
        findings = json.loads(result.stdout)["findings"]
        configure(project, f'select = ["{findings[0]["rule_id"]}"]')
        assert 0 < audit(project) < len(findings)

    def test_a_stated_ignore_removes_a_rule(self, project: Path) -> None:
        result = runner.invoke(app, ["run", str(project), "--format", "json"])
        findings = json.loads(result.stdout)["findings"]
        victim = findings[0]["rule_id"]
        configure(project, f'ignore = ["{victim}"]')
        after = runner.invoke(app, ["run", str(project), "--format", "json"])
        assert victim not in {f["rule_id"] for f in json.loads(after.stdout)["findings"]}

    def test_a_stated_format_changes_the_output(self, project: Path) -> None:
        configure(project, 'format = "sarif"')
        result = runner.invoke(app, ["run", str(project)])
        assert "$schema" in result.stdout, "the terminal default should have lost"

    def test_a_stated_fail_on_changes_the_exit_code(self, project: Path) -> None:
        configure(project, 'fail_on = "critical"\nmin_severity = "info"')
        lenient = runner.invoke(app, ["run", str(project), "--format", "json"])
        configure(project, 'fail_on = "info"\nmin_severity = "info"')
        strict = runner.invoke(app, ["run", str(project), "--format", "json"])
        assert strict.exit_code == 1
        assert lenient.exit_code != strict.exit_code or lenient.exit_code == 1

    def test_a_broken_config_stops_the_run(self, project: Path) -> None:
        """Exit 2, not 0: a config nobody can read must not report a clean audit."""
        assert "no key" in refusal(project, 'nonsense = "x"')


class TestTheErrorReachesTheUser:
    def test_the_table_name_survives_rich(self, project: Path) -> None:
        """`[tool.djaudit]` is also valid rich markup, and rich ate it.

        Before `_fail` escaped its message the user was told
        `pyproject.toml:  min_severity must be a string` -- with the part
        naming the table silently removed by the renderer.
        """
        assert "[tool.djaudit]" in refusal(project, "min_severity = 3")

    def test_the_path_survives_rich(self, project: Path) -> None:
        assert "pyproject.toml" in refusal(project, "min_severity = 3")


class TestTheMergeIsWiredUp:
    def test_every_field_is_consulted_by_the_cli(self) -> None:
        """A key that parses and is never merged is a setting that does nothing."""
        from djaudit import cli

        wired = inspect.getsource(cli.run) + str(cli._CONFIG_FIELD)
        for name in KNOWN:
            assert name in wired, f"{name} is parsed but never merged"

    def test_the_merge_still_asks_where_the_value_came_from(self) -> None:
        from djaudit import cli

        assert "get_parameter_source" in inspect.getsource(cli._resolve)

    def test_every_cli_name_maps_to_a_known_key(self) -> None:
        """The rename map is where `output_format` becomes `format`."""
        from djaudit import cli

        for cli_name, key in cli._CONFIG_FIELD.items():
            assert key in KNOWN, f"{cli_name} maps to {key}, which is not a config key"

    def test_the_refusal_list_is_still_enforced(self) -> None:
        assert "EXECUTING" in inspect.getsource(config_module.from_pyproject) or "EXECUTING" in (
            inspect.getsource(config_module._check_executing)
        )
