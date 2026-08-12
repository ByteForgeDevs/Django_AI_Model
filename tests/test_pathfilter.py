"""Tests for per-path exclusions.

The measurement that decided the design is `TestWhyItFiltersFindings`. It is
not a unit test of a matcher: it deletes a file from a copy of the ORM fixture
and shows that a run without that file reports **nothing at all**, then shows
that excluding the same path by pattern reports everything except that file's
own findings. Those two numbers are the whole argument for filtering by
location rather than dropping files before parsing.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from djaudit.cli import app
from djaudit.pathfilter import excluded

FIXTURES = Path(__file__).resolve().parent / "fixtures"
runner = CliRunner()


@pytest.fixture
def orm(tmp_path: Path) -> Path:
    target = tmp_path / "orm_project"
    shutil.copytree(FIXTURES / "orm_project", target)
    return target


def report(project: Path, *args: str) -> dict[str, Any]:
    result = runner.invoke(
        app,
        [
            "run",
            str(project),
            "--format",
            "json",
            "--min-severity",
            "info",
            "--min-confidence",
            "tentative",
            *args,
        ],
    )
    assert result.exit_code in (0, 1), result.output
    data: dict[str, Any] = json.loads(result.stdout)
    return data


def files_in(data: dict[str, Any]) -> set[str]:
    return {f["location"]["file"] for f in data["findings"]}


class TestTheMatcher:
    @pytest.mark.parametrize(
        ("path", "pattern"),
        [
            ("app/models.py", "app/models.py"),
            ("app/models.py", "app"),
            ("app/models.py", "app/"),
            ("app/models.py", "*/models.py"),
            ("app/models.py", "app/*"),
            ("app/sub/deep.py", "app"),
            ("migrations/0001_initial.py", "migrations"),
        ],
    )
    def test_it_matches(self, path: str, pattern: str) -> None:
        assert excluded(path, (pattern,))

    @pytest.mark.parametrize(
        ("path", "pattern"),
        [
            ("app/models.py", "app/views.py"),
            ("app/models.py", "other"),
            ("apples/models.py", "app"),
            ("app/models.py", "models.py"),
        ],
    )
    def test_it_does_not_match(self, path: str, pattern: str) -> None:
        assert not excluded(path, (pattern,))

    def test_a_bare_name_does_not_match_a_longer_directory(self) -> None:
        """`app` must not swallow `apples/`, which a naive prefix test would."""
        assert not excluded("apples/models.py", ("app",))

    def test_no_patterns_excludes_nothing(self) -> None:
        assert not excluded("anything.py", ())

    def test_an_empty_pattern_matches_nothing_here(self) -> None:
        """Not a guard -- an observation. It is why the CLI rejects it instead.

        A guard in `excluded` could not fire: against a relative path "" and
        "/*" both match nothing. Silently doing nothing is the wrong answer for
        a pattern the user clearly meant something by, so the rejection lives
        at the boundary (see TestAnEmptyPatternIsRefused).
        """
        assert not excluded("anything.py", ("",))
        assert not excluded("anything.py", ("/",))


class TestWhyItFiltersFindings:
    """The measurement behind the design, re-run rather than remembered."""

    def test_the_fixture_reports_across_several_files(self, orm: Path) -> None:
        data = report(orm)
        assert len(data["findings"]) > 10
        assert len(files_in(data)) > 2, "the point needs findings outside models.py"

    def test_removing_the_models_file_empties_the_whole_run(self, orm: Path) -> None:
        """What dropping a file before parsing would do.

        models.py holds 2 of this fixture's findings. Take it away and the run
        reports zero -- every DJP finding in the views, the serializers and the
        management command is derived from the model graph it builds. An
        exclusion implemented that way would not hide one file; it would empty
        the audit and exit clean.
        """
        before = len(report(orm)["findings"])
        own = len(
            [f for f in report(orm)["findings"] if f["location"]["file"].endswith("models.py")]
        )
        (orm / "inventory" / "models.py").unlink()
        after = len(report(orm)["findings"])
        assert 0 < own < before
        assert after == 0, "if this stops being catastrophic, revisit the design"

    def test_excluding_the_models_file_hides_only_its_findings(self, orm: Path) -> None:
        """The contrast: same path, excluded rather than removed."""
        before = report(orm)
        own = [f for f in before["findings"] if f["location"]["file"].endswith("models.py")]
        after = report(orm, "--exclude-path", "inventory/models.py")
        assert len(after["findings"]) == len(before["findings"]) - len(own)
        assert after["summary"]["suppressed_path"] == len(own)
        assert not any(f["location"]["file"].endswith("models.py") for f in after["findings"])

    def test_the_other_files_keep_their_findings(self, orm: Path) -> None:
        """The rules that read the model graph still ran, and still reported."""
        after = report(orm, "--exclude-path", "inventory/models.py")
        assert any(f["location"]["file"].endswith("views.py") for f in after["findings"])


class TestThroughTheCli:
    def test_a_directory_pattern_hides_a_subtree(self, orm: Path) -> None:
        data = report(orm, "--exclude-path", "inventory")
        assert data["findings"] == []
        assert data["summary"]["suppressed_path"] > 0

    def test_a_glob_hides_matching_files(self, orm: Path) -> None:
        before = files_in(report(orm))
        assert any(f.endswith("views.py") for f in before)
        after = report(orm, "--exclude-path", "*/views.py")
        assert not any(f.endswith("views.py") for f in files_in(after))
        assert after["findings"], "the glob should not have emptied the run"

    def test_the_flag_is_repeatable(self, orm: Path) -> None:
        one = report(orm, "--exclude-path", "*/views.py")
        two = report(orm, "--exclude-path", "*/views.py", "--exclude-path", "*/serializers.py")
        assert len(two["findings"]) < len(one["findings"])

    def test_nothing_excluded_reports_zero_suppressed(self, orm: Path) -> None:
        assert report(orm)["summary"]["suppressed_path"] == 0

    def test_a_pattern_matching_nothing_changes_nothing(self, orm: Path) -> None:
        before = report(orm)
        after = report(orm, "--exclude-path", "does/not/exist")
        assert len(after["findings"]) == len(before["findings"])
        assert after["summary"]["suppressed_path"] == 0


class TestItIsVisible:
    """A tool that hides findings silently is the thing djaudit criticises."""

    def test_the_json_summary_reports_the_count(self, orm: Path) -> None:
        assert report(orm, "--exclude-path", "inventory")["summary"]["suppressed_path"] > 0

    def test_the_terminal_says_so(self, orm: Path) -> None:
        result = runner.invoke(
            app, ["run", str(orm), "--min-severity", "info", "--exclude-path", "inventory"]
        )
        assert "excluded paths" in result.output, (
            "an empty report and an emptied report must not look the same"
        )


class TestFromTheConfigFile:
    def configure(self, project: Path, body: str) -> None:
        (project / "pyproject.toml").write_text(f"[tool.djaudit]\n{body}\n")

    def test_the_file_can_state_exclusions(self, orm: Path) -> None:
        before = report(orm)
        self.configure(orm, 'exclude_paths = ["inventory/models.py"]')
        after = report(orm)
        assert after["summary"]["suppressed_path"] > 0
        assert len(after["findings"]) < len(before["findings"])

    def test_the_flag_beats_the_file(self, orm: Path) -> None:
        self.configure(orm, 'exclude_paths = ["inventory"]')
        assert report(orm)["findings"] == []
        loosened = report(orm, "--exclude-path", "does/not/exist")
        assert loosened["findings"], "the command line did not win"

    def test_a_wrong_type_is_refused(self, orm: Path) -> None:
        self.configure(orm, 'exclude_paths = "inventory"')
        result = runner.invoke(app, ["run", str(orm), "--format", "json"])
        assert result.exit_code == 2
        assert "list of strings" in result.output


class TestAnEmptyPatternIsRefused:
    """Doing nothing quietly is the failure mode this whole substep is about."""

    @pytest.mark.parametrize("pattern", ["", "   ", "/", "//"])
    def test_the_flag_refuses_it(self, orm: Path, pattern: str) -> None:
        result = runner.invoke(app, ["run", str(orm), "--exclude-path", pattern])
        assert result.exit_code == 2
        assert "exclude path pattern is empty" in result.output

    def test_the_config_file_refuses_it(self, orm: Path) -> None:
        (orm / "pyproject.toml").write_text('[tool.djaudit]\nexclude_paths = [""]\n')
        result = runner.invoke(app, ["run", str(orm)])
        assert result.exit_code == 2
        assert "exclude path pattern is empty" in result.output

    def test_a_real_pattern_alongside_it_does_not_rescue_it(self, orm: Path) -> None:
        result = runner.invoke(
            app, ["run", str(orm), "--exclude-path", "inventory", "--exclude-path", ""]
        )
        assert result.exit_code == 2
