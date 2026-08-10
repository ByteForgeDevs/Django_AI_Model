"""The changelog says what actually shipped.

Most of this exercises paths the real repository cannot reach yet: it has no
tags, so every released-version code path and the whole breaking-change gate
would otherwise go to PyPI never having run. Those tests build small git
repositories with real tags and real commits.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gen_changelog


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def commit(repo: Path, subject: str, body: str = "", touch: str | None = "src.py") -> None:
    """Commit a change. ``touch=None`` commits whatever the caller already wrote."""
    if touch is not None:
        path = repo / touch
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((path.read_text() if path.exists() else "") + subject + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", subject, "-m", body)


def ledger(repo: Path, schemas: list[str], prints: list[str]) -> None:
    path = repo / "schema" / "contract.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schemas": {s: {"tool_version": "0.1.0"} for s in schemas},
                "fingerprints": {f: {"tool_version": "0.1.0"} for f in prints},
            }
        )
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    ledger(tmp_path, ["1"], ["djaudit/v1"])
    commit(tmp_path, "feat(core): the first thing")
    monkeypatch.setattr(gen_changelog, "ROOT", tmp_path)
    monkeypatch.setattr(gen_changelog, "CHANGELOG", tmp_path / "CHANGELOG.md")
    monkeypatch.setattr(gen_changelog, "LEDGER", tmp_path / "schema" / "contract.json")
    return tmp_path


def complaint(capsys: pytest.CaptureFixture[str]) -> str:
    code = gen_changelog.check()
    said = capsys.readouterr().out
    assert code == 1, f"check passed when it should have failed; it said: {said}"
    return said


class TestTheRealRepository:
    def test_the_committed_changelog_is_current(self) -> None:
        assert gen_changelog.check() == 0

    def test_generation_is_idempotent(self) -> None:
        assert gen_changelog.build() == gen_changelog.CHANGELOG.read_text()

    def test_it_found_real_commits(self) -> None:
        """A changelog built from nothing would pass every other test here."""
        assert len(gen_changelog.commits(None, "HEAD")) > 100


class TestWhatGetsIncluded:
    def test_a_feature_is_listed(self, repo: Path) -> None:
        assert "the first thing" in gen_changelog.build()

    def test_the_scope_survives(self, repo: Path) -> None:
        assert "**core:**" in gen_changelog.build()

    def test_an_unknown_type_is_dropped(self, repo: Path) -> None:
        commit(repo, "style(x): reflow a docstring")
        assert "reflow a docstring" not in gen_changelog.build()

    def test_a_merge_commit_is_dropped(self, repo: Path) -> None:
        git(repo, "checkout", "-q", "-b", "side")
        commit(repo, "feat(side): work on a branch", touch="side.py")
        git(repo, "checkout", "-q", "-")
        commit(repo, "feat(main): work on main", touch="main.py")
        git(repo, "merge", "-q", "--no-ff", "side", "-m", "Merge pull request #1 from side")
        built = gen_changelog.build()
        assert "Merge pull request" not in built
        assert "work on a branch" in built, "the branch's own commit should survive"
        subjects = [c.subject for c in gen_changelog.commits(None, "HEAD")]
        assert not [s for s in subjects if s.startswith("Merge")], (
            "the merge must be excluded from the commit list, not merely unprintable: "
            "the subject regex would drop it either way, so only this sees --no-merges"
        )

    def test_a_changelog_only_commit_is_dropped(self, repo: Path) -> None:
        """Otherwise writing the changelog is a change the changelog must describe."""
        commit(repo, "docs(changelog): regenerate", touch="CHANGELOG.md")
        assert "regenerate" not in gen_changelog.build()

    def test_a_commit_touching_the_changelog_and_code_survives(self, repo: Path) -> None:
        """The exclusion is for changelog-*only* commits, not any commit near one."""
        (repo / "CHANGELOG.md").write_text("x")
        (repo / "src.py").write_text("y")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat(core): real work plus a changelog touch")
        assert "real work plus a changelog touch" in gen_changelog.build()


class TestBreakingChangesAreMarked:
    def test_a_bang_marks_it(self, repo: Path) -> None:
        commit(repo, "feat(api)!: drop the old flag")
        assert "**BREAKING** **api:** drop the old flag" in gen_changelog.build()

    def test_a_footer_marks_it(self, repo: Path) -> None:
        commit(repo, "feat(api): drop the old flag", body="BREAKING CHANGE: the flag is gone")
        assert "**BREAKING**" in gen_changelog.build()

    def test_an_ordinary_commit_is_not_marked(self, repo: Path) -> None:
        """The contrast: without this, marking everything would pass both tests."""
        assert "**BREAKING**" not in gen_changelog.build()


class TestReleasedSections:
    def test_a_tag_opens_a_released_section(self, repo: Path) -> None:
        git(repo, "tag", "v0.1.0")
        built = gen_changelog.build()
        assert "## 0.1.0" in built
        assert "the first thing" in built

    def test_work_after_a_tag_is_unreleased(self, repo: Path) -> None:
        git(repo, "tag", "v0.1.0")
        commit(repo, "feat(core): came later")
        built = gen_changelog.build()
        released = built.index("## 0.1.0")
        assert built.index("came later") < released, "later work belongs above the release"
        assert built.index("the first thing") > released

    def test_each_release_holds_only_its_own_commits(self, repo: Path) -> None:
        git(repo, "tag", "v0.1.0")
        commit(repo, "feat(core): second release work")
        git(repo, "tag", "v0.2.0")
        built = gen_changelog.build()
        first = built.index("## 0.1.0")
        second = built.index("## 0.2.0")
        assert second < first, "newest release first"
        assert first > built.index("second release work") > second

    def test_a_non_release_tag_is_ignored(self, repo: Path) -> None:
        """Tags are not all releases, and a stray one must not invent a section."""
        git(repo, "tag", "vendor-sync")
        git(repo, "tag", "v1.2")
        assert gen_changelog.tags() == []

    def test_an_empty_release_says_so(self, repo: Path) -> None:
        git(repo, "tag", "v0.1.0")
        commit(repo, "style(x): nothing worth listing")
        git(repo, "tag", "v0.2.0")
        assert "_Nothing recorded._" in gen_changelog.build()


class TestTheContractGate:
    """A new fingerprint version invalidates baselines the user has committed."""

    def test_a_new_fingerprint_version_demands_a_note(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        git(repo, "tag", "v0.1.0")
        ledger(repo, ["1"], ["djaudit/v1", "djaudit/v2"])
        commit(repo, "feat(core): new fingerprints", touch=None)
        gen_changelog.CHANGELOG.write_text("# Changelog\n\nnothing to see\n")
        said = complaint(capsys)
        assert "djaudit/v2" in said

    def test_the_generated_note_satisfies_the_gate(self, repo: Path) -> None:
        """The contrast: the gate must accept what the generator produces."""
        git(repo, "tag", "v0.1.0")
        ledger(repo, ["1"], ["djaudit/v1", "djaudit/v2"])
        commit(repo, "feat(core): new fingerprints", touch=None)
        gen_changelog.CHANGELOG.write_text(gen_changelog.build())
        assert gen_changelog.check() == 0

    def test_the_note_tells_the_user_what_to_do(self, repo: Path) -> None:
        git(repo, "tag", "v0.1.0")
        ledger(repo, ["1"], ["djaudit/v1", "djaudit/v2"])
        commit(repo, "feat(core): new fingerprints", touch=None)
        built = gen_changelog.build()
        assert "no longer match" in built
        assert "--write-baseline" in built, "a warning without a remedy is just alarming"

    def test_a_new_schema_version_demands_a_note(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        git(repo, "tag", "v0.1.0")
        ledger(repo, ["1", "2"], ["djaudit/v1"])
        commit(repo, "feat(core): new schema", touch=None)
        gen_changelog.CHANGELOG.write_text("# Changelog\n\nnothing to see\n")
        assert "Report schema 2" in complaint(capsys)

    def test_an_unchanged_contract_demands_nothing(self, repo: Path) -> None:
        git(repo, "tag", "v0.1.0")
        commit(repo, "feat(core): ordinary work")
        assert gen_changelog.contract_changes("v0.1.0") == []

    def test_changes_are_read_from_the_ledger_not_the_subject(self, repo: Path) -> None:
        """A commit subject is what someone remembered; the ledger is what shipped."""
        git(repo, "tag", "v0.1.0")
        ledger(repo, ["1"], ["djaudit/v1", "djaudit/v2"])
        commit(repo, "chore(misc): tidy up", touch=None)
        assert any("djaudit/v2" in n for n in gen_changelog.contract_changes("v0.1.0"))


class TestStaleness:
    def test_an_out_of_date_changelog_is_caught(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gen_changelog.CHANGELOG.write_text(gen_changelog.build())
        commit(repo, "feat(core): added after the changelog was written")
        assert "out of date" in complaint(capsys)

    def test_a_missing_changelog_is_caught(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert "CHANGELOG.md is missing" in complaint(capsys)

    def test_a_current_changelog_passes(self, repo: Path) -> None:
        gen_changelog.CHANGELOG.write_text(gen_changelog.build())
        assert gen_changelog.check() == 0


class TestShallowClones:
    """A truncated history rebuilds a wrong changelog, so say which it is.

    GitHub's checkout action clones to depth 1 by default. Without this guard
    the gate reports "out of date" on a correct changelog, which sends the
    reader looking for a content problem that is not there.
    """

    def test_a_shallow_clone_is_named_as_the_problem(
        self, repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        gen_changelog.CHANGELOG.write_text(gen_changelog.build())
        assert gen_changelog.check() == 0, "the deep clone must pass first"

        shallow = tmp_path / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)],
            check=True,
            capture_output=True,
        )
        gen_changelog.ROOT = shallow
        gen_changelog.CHANGELOG = shallow / "CHANGELOG.md"
        assert gen_changelog._shallow(), "the clone must actually be shallow"

        out = complaint(capsys)
        assert "shallow clone" in out
        assert "fetch-depth: 0" in out, "the message must say how to fix it"
        assert "out of date" not in out, (
            "the misleading content complaint must not be what the reader sees"
        )
