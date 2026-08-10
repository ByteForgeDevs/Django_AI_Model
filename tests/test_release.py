"""The release gate, checked against the defects it must not miss.

Publishing is the one thing this repository does that cannot be undone. A
version on PyPI is permanent, its contents are immutable, and `__version__` is
stamped into every JSON and SARIF report that release will ever emit. So the
gate guarding it is worth more scrutiny than the thing it guards, and the
tests below are the defects it was written to catch rather than a description
of what it does.

Two of them are not hypothetical. YAML 1.1 resolves the bare word `on` to the
boolean `true`, so a workflow's trigger block is `workflow[True]` and a reader
that asks for `workflow["on"]` raises `KeyError` on a file that plainly
contains one -- and a `.get("on", {})` would return empty and make every
trigger check below pass while comparing nothing. And a `needs:` key holding a
single string rather than a list is legal YAML that would silently break the
dependency walk.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

from djaudit import __version__

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_release  # noqa: E402

RELEASE = ROOT / ".github/workflows/release.yml"
CI = ROOT / ".github/workflows/ci.yml"
PYPROJECT = ROOT / "pyproject.toml"
MODULE = "src/djaudit/__init__.py"
# The assignment, not the path: `pyproject.toml` names that file in a comment
# first, so an un-fix anchored on the path alone rewrites the prose and leaves
# the build reading exactly what it read before.
DECLARED = f'path = "{MODULE}"'


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of everything the gate reads, so a defect can be injected."""
    root = tmp_path / "repo"
    (root / ".github/workflows").mkdir(parents=True)
    (root / "src/djaudit").mkdir(parents=True)
    shutil.copy(PYPROJECT, root / "pyproject.toml")
    shutil.copy(RELEASE, root / ".github/workflows/release.yml")
    shutil.copy(CI, root / ".github/workflows/ci.yml")
    shutil.copy(ROOT / MODULE, root / MODULE)

    monkeypatch.setattr(check_release, "ROOT", root)
    monkeypatch.setattr(check_release, "PYPROJECT", root / "pyproject.toml")
    monkeypatch.setattr(check_release, "RELEASE", root / ".github/workflows/release.yml")
    monkeypatch.setattr(check_release, "CI", root / ".github/workflows/ci.yml")
    return root


def edit(path: Path, old: str, new: str) -> None:
    """Replace once, refusing an edit that changes nothing.

    Every test here is an un-fix, and an un-fix that alters no text is
    indistinguishable from a gate that passes.
    """
    text = path.read_text()
    found = text.count(old)
    # Uniqueness, not presence. `pyproject.toml` names the version module in a
    # comment before it names it in `[tool.hatch.version]`, so an anchor that
    # merely exists somewhere rewrote the prose and left the build reading what
    # it read before -- an un-fix that ran, changed a file, and tested nothing.
    assert found == 1, f"{path.name} contains {found} of {old!r}; this test needs exactly one"
    path.write_text(text.replace(old, new, 1))


def complaint(capsys: pytest.CaptureFixture[str], *, tag: str | None = None) -> str:
    """Run the gate, require failure, and return what it said."""
    code = check_release.main(["--tag", tag] if tag else [])
    said = capsys.readouterr().out
    assert code == 1, f"the gate passed, saying: {said.strip()}"
    return said


class TestItPassesOnTheRealTree:
    def test_the_release_path_is_consistent_right_now(self) -> None:
        assert check_release.main([]) == 0

    def test_the_current_version_could_be_released(self) -> None:
        assert check_release.main(["--tag", f"v{__version__}"]) == 0


class TestTheVersionHasOneSource:
    def test_a_literal_version_returning_to_pyproject(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two literals is two answers to which version produced a report."""
        edit(tree / "pyproject.toml", 'dynamic = ["version"]', 'version = "0.1.0"')
        assert "as a literal" in complaint(capsys)

    def test_the_version_no_longer_dynamic(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(tree / "pyproject.toml", 'dynamic = ["version"]', "dynamic = []")
        assert "does not declare version as dynamic" in complaint(capsys)

    def test_the_build_given_nothing_to_read(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            tree / "pyproject.toml",
            f"[tool.hatch.version]\n{DECLARED}",
            "",
        )
        assert "no [tool.hatch.version] path" in complaint(capsys)

    def test_the_build_reading_a_file_that_is_not_there(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(tree / "pyproject.toml", DECLARED, 'path = "src/djaudit/gone.py"')
        assert "which does not exist" in complaint(capsys)

    def test_the_build_reading_a_module_with_no_version(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tree / "src/djaudit/models.py").write_text("# no version here\n")
        edit(tree / "pyproject.toml", DECLARED, 'path = "src/djaudit/models.py"')
        assert "defines no __version__" in complaint(capsys)

    def test_the_module_and_the_import_disagreeing(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The drift the single source exists to prevent, forced directly."""
        monkeypatch.setattr(check_release, "__version__", "9.9.9")
        assert "the imported package says 9.9.9" in complaint(capsys)

    def test_a_version_we_would_not_know_how_to_tag(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(tree / MODULE, '__version__ = "0.1.0"', '__version__ = "0.1.0+local"')
        monkeypatch.setattr(check_release, "__version__", "0.1.0+local")
        assert "is not a release version we know how to tag" in complaint(capsys)


class TestOnlyATagCanRelease:
    def test_a_release_that_can_be_started_by_hand(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The path by which an untested commit reaches PyPI."""
        edit(
            tree / ".github/workflows/release.yml",
            "on:\n  push:\n    tags: ['v*']",
            "on:\n  workflow_dispatch:\n  push:\n    tags: ['v*']",
        )
        assert "can be started by workflow_dispatch" in complaint(capsys)

    def test_a_release_that_fires_on_a_branch(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            tree / ".github/workflows/release.yml",
            "  push:\n    tags: ['v*']",
            "  push:\n    branches: [main]\n    tags: ['v*']",
        )
        assert "triggers on a branch push" in complaint(capsys)

    def test_a_release_that_no_longer_fires_on_tags(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(tree / ".github/workflows/release.yml", "    tags: ['v*']", "    branches: [main]")
        assert "does not trigger on tags" in complaint(capsys)

    def test_the_workflow_disappearing(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tree / ".github/workflows/release.yml").unlink()
        assert "there is no release workflow" in complaint(capsys)


class TestPublishingNeedsNoLongLivedCredential:
    def test_an_api_token_passed_to_the_publisher(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A token publishes from anywhere, forever, and its theft is invisible here."""
        edit(
            tree / ".github/workflows/release.yml",
            "      - uses: pypa/gh-action-pypi-publish@release/v1",
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
            "        with:\n          password: ${{ secrets.PYPI_API_TOKEN }}",
        )
        assert "passes a password" in complaint(capsys)

    def test_the_publisher_unable_to_mint_a_token(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            tree / ".github/workflows/release.yml",
            "    permissions:\n      id-token: write\n",
            "",
        )
        assert "cannot mint an OIDC token" in complaint(capsys)

    def test_no_environment_for_pypi_to_pin(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            tree / ".github/workflows/release.yml",
            "    environment:\n      name: pypi\n      url: https://pypi.org/p/djaudit\n",
            "",
        )
        assert "declares no environment" in complaint(capsys)

    def test_nothing_publishing_at_all(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            tree / ".github/workflows/release.yml",
            "      - uses: pypa/gh-action-pypi-publish@release/v1",
            "      - run: echo shipped",
        )
        assert "no job publishes anything" in complaint(capsys)


class TestPublishingIsGated:
    def test_publishing_that_waits_for_nothing(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            tree / ".github/workflows/release.yml",
            "    name: publish to PyPI\n    needs: build\n",
            "    name: publish to PyPI\n",
        )
        assert "depends on nothing" in complaint(capsys)

    def test_a_build_that_no_longer_waits_for_the_gate(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One link removed in the middle of the chain, not at its end."""
        edit(
            tree / ".github/workflows/release.yml",
            "    name: build sdist and wheel\n    needs: gate\n",
            "    name: build sdist and wheel\n",
        )
        assert "the gate is not on the tag" in complaint(capsys)

    def test_the_gate_job_running_something_else(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            tree / ".github/workflows/release.yml",
            "    uses: ./.github/workflows/ci.yml",
            "    uses: ./.github/workflows/nothing.yml",
        )
        assert "the gate is not on the tag" in complaint(capsys)

    def test_ci_no_longer_callable(self, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        edit(tree / ".github/workflows/ci.yml", "  workflow_call:\n", "")
        assert "is not callable" in complaint(capsys)

    def test_ci_missing_entirely(self, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tree / ".github/workflows/ci.yml").unlink()
        assert "does not exist to be called" in complaint(capsys)

    def test_a_needs_written_as_a_string_is_still_followed(self, tree: Path) -> None:
        """`needs: gate` and `needs: [gate]` are the same graph.

        A reader that iterates a string walks its characters, finds no job
        called `g`, and reports a broken chain -- or worse, finds the chain
        broken for a tree that is correct.
        """
        edit(
            tree / ".github/workflows/release.yml",
            "    needs: gate\n",
            "    needs:\n      - gate\n",
        )
        assert check_release.main([]) == 0


class TestTheTagIsTheVersion:
    def test_a_tag_that_does_not_match(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """It does not fail the build; it publishes the wrong number."""
        assert "does not match the package version" in complaint(capsys, tag="v9.9.9")

    def test_a_tag_without_the_v(self, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert "does not start with v" in complaint(capsys, tag="0.1.0")

    def test_the_matching_tag_passes(self, tree: Path) -> None:
        assert check_release.main(["--tag", f"v{__version__}"]) == 0


class TestTheTriggerReader:
    """`on:` is the boolean `True` after YAML 1.1 resolution.

    A reader that asks for the string key raises on a real workflow; one that
    asks with a default returns empty and makes every trigger check vacuous.
    Neither failure is visible in a passing run, which is why it is asserted
    directly.
    """

    def test_it_reads_the_block_yaml_actually_produces(self) -> None:
        parsed = yaml.safe_load(RELEASE.read_text())
        assert "on" not in parsed, "PyYAML stopped folding `on` to True; simplify the reader"
        assert True in parsed
        assert "push" in check_release.triggers(parsed)

    def test_it_reads_a_list_of_triggers(self) -> None:
        assert set(check_release.triggers({True: ["push", "pull_request"]})) == {
            "push",
            "pull_request",
        }

    def test_it_reads_a_workflow_with_no_triggers(self) -> None:
        assert check_release.triggers({"jobs": {}}) == {}

    def test_the_real_ci_workflow_is_callable(self) -> None:
        assert "workflow_call" in check_release.triggers(yaml.safe_load(CI.read_text()))


class TestTheWorkflowsAreValidYaml:
    """A workflow GitHub cannot parse is a release that silently never runs."""

    @pytest.mark.parametrize("workflow", [RELEASE, CI])
    def test_it_parses(self, workflow: Path) -> None:
        parsed = yaml.safe_load(workflow.read_text())
        assert parsed["jobs"], f"{workflow.name} declares no jobs"

    def test_every_job_the_release_needs_exists(self) -> None:
        jobs = yaml.safe_load(RELEASE.read_text())["jobs"]
        for name, job in jobs.items():
            declared = job.get("needs") or []
            for dependency in [declared] if isinstance(declared, str) else declared:
                assert dependency in jobs, f"{name} needs {dependency}, which is not a job"
