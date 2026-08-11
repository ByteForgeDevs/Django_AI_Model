"""Tests for the `pre-commit` hooks.

`TestTheGate` checks the manifest against the CLI without running anything.
`TestRealPreCommit` runs the actual `pre-commit` binary against throwaway
repositories, which is the only way to learn that the manifest is one the tool
accepts, that the hook installs, and that it fails a dirty tree while passing a
clean one.

Each of those builds a virtualenv and installs djaudit into it, so they are
opt-in through `DJAUDIT_TEST_PRE_COMMIT=1`, following the same convention as
the Postgres tests. Asking for them when `pre-commit` is not installed is an
error rather than a skip: an opt-in that quietly does nothing is the failure
mode the opt-in exists to prevent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_pre_commit

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / ".pre-commit-hooks.yaml"
FIXTURES = ROOT / "tests/fixtures"

pre_commit = shutil.which("pre-commit")
REQUESTED = os.environ.get("DJAUDIT_TEST_PRE_COMMIT") == "1"

if REQUESTED and pre_commit is None:
    raise RuntimeError(
        "DJAUDIT_TEST_PRE_COMMIT=1 was set but pre-commit is not on PATH, so the "
        "tests it asks for would silently skip"
    )


def hooks() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = yaml.safe_load(MANIFEST.read_text())
    return loaded


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def consumer(tmp_path: Path, fixture: str, hook_id: str, rev: str) -> Path:
    """Build a git repository that consumes our hooks from this checkout."""
    repo = tmp_path / "consumer"
    repo.mkdir()
    shutil.copytree(FIXTURES / fixture, repo, dirs_exist_ok=True)
    (repo / ".pre-commit-config.yaml").write_text(
        yaml.safe_dump(
            {"repos": [{"repo": str(ROOT), "rev": rev, "hooks": [{"id": hook_id}]}]},
            sort_keys=False,
        )
    )
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.mark.skipif(not REQUESTED, reason="set DJAUDIT_TEST_PRE_COMMIT=1 to run these")
class TestRealPreCommit:
    """Run the tool, not a model of it."""

    @staticmethod
    @pytest.fixture(scope="class")
    def rev() -> str:
        """The commit pre-commit should clone.

        pre-commit clones this repository at a revision, so the manifest has to
        be committed for it to be found. When it is not yet committed -- which
        is the normal case while the substep is being built -- these tests
        cannot run and say so rather than passing vacuously.
        """
        committed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-tree", "HEAD", "--name-only", ".pre-commit-hooks.yaml"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not committed:
            pytest.skip(".pre-commit-hooks.yaml is not committed yet, so pre-commit cannot see it")
        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return head.stdout.strip()

    def _run(self, repo: Path) -> subprocess.CompletedProcess[str]:
        assert pre_commit is not None
        return subprocess.run(
            [pre_commit, "run", "--all-files"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            env=dict(os.environ, PRE_COMMIT_HOME=str(repo.parent / "pre-commit-cache")),
        )

    def test_a_project_with_findings_fails_the_commit(self, tmp_path: Path, rev: str) -> None:
        repo = consumer(tmp_path, "vulnerable_project", "djaudit", rev)
        done = self._run(repo)
        assert done.returncode != 0, done.stdout + done.stderr
        assert "Failed" in done.stdout
        assert "DJS-" in done.stdout, "the findings themselves should reach the terminal"

    def test_a_clean_project_passes(self, tmp_path: Path, rev: str) -> None:
        repo = consumer(tmp_path, "near_miss_project", "djaudit", rev)
        done = self._run(repo)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Passed" in done.stdout

    def test_the_security_hook_runs_only_its_families(self, tmp_path: Path, rev: str) -> None:
        repo = consumer(tmp_path, "orm_project", "djaudit-security", rev)
        done = self._run(repo)
        assert "DJP-" not in done.stdout, (
            "the security hook restricts to DJS/DJI/DJA, so ORM findings must not appear"
        )


class TestTheGate:
    @pytest.fixture
    def tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        copy = tmp_path / "hooks.yaml"
        copy.write_text(MANIFEST.read_text())
        doc = tmp_path / "pre-commit.md"
        doc.write_text((ROOT / "docs/pre-commit.md").read_text())
        monkeypatch.setattr(check_pre_commit, "MANIFEST", copy)
        monkeypatch.setattr(check_pre_commit, "DOC", doc)
        monkeypatch.setattr(check_pre_commit, "ROOT", tmp_path)
        return copy

    def complaint(self, capsys: pytest.CaptureFixture[str]) -> str:
        assert check_pre_commit.check() == 1
        return capsys.readouterr().out

    def rewrite(self, tree: Path, old: str, new: str) -> None:
        text = tree.read_text()
        assert text.count(old) == 1, f"anchor appears {text.count(old)} times"
        tree.write_text(text.replace(old, new, 1))

    def test_the_real_manifest_passes(self) -> None:
        assert check_pre_commit.check() == 0

    def test_passing_filenames_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, "  pass_filenames: false\n  require_serial: true\n\n#", "#")
        assert "does not set `pass_filenames: false`" in self.complaint(capsys)

    def test_a_flag_the_cli_rejects_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, "args: [--fail-on, high]", "args: [--fail-upon, high]")
        assert "--fail-upon, which `djaudit run` rejects" in self.complaint(capsys)

    def test_a_value_outside_the_choices_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, "args: [--fail-on, high]", "args: [--fail-on, severe]")
        assert "but --fail-on accepts" in self.complaint(capsys)

    def test_a_renamed_family_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, "--family, DJI,", "--family, DJZ,")
        assert "but --family accepts" in self.complaint(capsys)

    def test_a_command_that_does_not_exist_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, "entry: djaudit run\n  args: [--fail-on, high]", "entry: djaudit audit")
        assert "which is not a command" in self.complaint(capsys)

    def test_an_undocumented_hook_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        self.rewrite(tree, "- id: djaudit-security", "- id: djaudit-fast")
        assert "is not documented" in self.complaint(capsys)

    def test_a_documented_hook_that_does_not_exist_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        doc = tmp_path / "pre-commit.md"
        doc.write_text(doc.read_text().replace("- id: djaudit-security", "- id: djaudit-ghost"))
        assert "which does not exist" in self.complaint(capsys)

    def test_an_unrestricted_hook_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(
            tree, "  types: [python]\n  pass_filenames: false\n  require_serial: true\n\n#", "#"
        )
        assert "does not restrict `types`" in self.complaint(capsys)

    def test_a_missing_manifest_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tree.unlink()
        assert ".pre-commit-hooks.yaml is missing" in self.complaint(capsys)


class TestWhatTheManifestPromises:
    def test_no_hook_passes_filenames(self) -> None:
        """The claim the manifest's comment rests on, asserted rather than trusted."""
        for hook in hooks():
            assert hook["pass_filenames"] is False, hook["id"]

    def test_a_subdirectory_really_does_find_nothing(self) -> None:
        """The measurement the manifest cites, re-measured.

        This is why the hooks audit the repository root. Auditing an app package
        is not a smaller audit -- it is an empty one, and it succeeds.
        """
        from typer.testing import CliRunner

        from djaudit.cli import app as cli

        runner = CliRunner()
        whole = runner.invoke(
            cli, ["run", str(FIXTURES / "vulnerable_project"), "--format", "json"]
        )
        part = runner.invoke(
            cli, ["run", str(FIXTURES / "vulnerable_project" / "app"), "--format", "json"]
        )
        assert whole.exit_code == 1, "the fixture is meant to fail an audit"
        assert part.exit_code == 0, "and auditing a subtree of it is meant to succeed, quietly"

    def test_every_hook_is_documented_with_its_families(self) -> None:
        text = (ROOT / "docs/pre-commit.md").read_text()
        security = next(h for h in hooks() if h["id"] == "djaudit-security")
        args = [str(a) for a in security["args"]]
        for family in [args[i + 1] for i, a in enumerate(args) if a == "--family"]:
            assert family in text, f"{family} is restricted to but never mentioned"
