"""The rule documentation has to be true, and it has to stay true.

Every page under ``docs/rules/`` is generated from the registry, so the only
way one can be wrong is if somebody edits the page instead of the rule, or
edits the rule and forgets to regenerate. Both are caught here rather than in
review.

The rest of these tests are about the ``limitations`` field itself. It is the
one piece of rule metadata whose absence is invisible -- a rule with no stated
boundary still runs, still reports, and still looks finished -- so it is worth
asserting that every rule has one and that each one says something.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from djaudit import registry
from djaudit.registry import Rule

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "gen_rule_docs.py"
DOCS = ROOT / "docs" / "rules"


@pytest.fixture(scope="module")
def rules() -> list[type[Rule]]:
    registry._load_builtin_rules()
    return sorted(registry.all_rules(), key=lambda r: r.meta.id)


class TestLimitations:
    def test_every_rule_states_one(self, rules: list[type[Rule]]) -> None:
        missing = [r.meta.id for r in rules if not r.meta.limitations]
        assert not missing, f"rules with no stated limitation: {missing}"

    def test_each_is_a_written_sentence(self, rules: list[type[Rule]]) -> None:
        # A placeholder like "none" would satisfy the field and tell a reader
        # nothing, so require enough text to have made a claim.
        for rule in rules:
            for item in rule.meta.limitations:
                assert len(item) >= 60, f"{rule.meta.id}: limitation too short: {item!r}"
                assert item.endswith("."), f"{rule.meta.id}: not a sentence: {item!r}"
                assert item[0].isupper(), f"{rule.meta.id}: not capitalised: {item!r}"

    def test_it_is_not_the_rationale_again(self, rules: list[type[Rule]]) -> None:
        for rule in rules:
            for item in rule.meta.limitations:
                assert item not in rule.meta.rationale, (
                    f"{rule.meta.id}: limitation is copied out of the rationale"
                )

    def test_a_tentative_rule_explains_why(self, rules: list[type[Rule]]) -> None:
        # Confidence below firm is a claim that something outside the source
        # decides the answer. Whatever that something is belongs in writing.
        for rule in rules:
            if rule.meta.confidence.value != "tentative":
                continue
            assert rule.meta.limitations, (
                f"{rule.meta.id} ships tentative with no explanation of what it cannot see"
            )


class TestGeneratedDoc:
    def test_committed_copies_are_current(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"docs/rules/ is stale; run scripts/gen_rule_docs.py\n{result.stderr}"
        )

    def test_every_rule_has_a_section_on_its_family_page(self, rules: list[type[Rule]]) -> None:
        # The generator used to be hardcoded to DJS, so DJA and DJD rules were
        # documented nowhere while the check still passed. Reading each rule's
        # own family means a new family cannot ship undocumented.
        pages = {path.stem: path.read_text(encoding="utf-8") for path in DOCS.glob("*.md")}
        for rule in rules:
            family = rule.meta.family.value
            assert family in pages, f"{rule.meta.id} has no {family} page"
            assert f"### {rule.meta.id} — {rule.meta.title}" in pages[family]

    def test_a_page_exists_for_every_family_that_has_rules(self, rules: list[type[Rule]]) -> None:
        families = {r.meta.family.value for r in rules}
        assert families <= {p.stem for p in DOCS.glob("*.md")}

    def test_no_page_exists_for_a_family_with_no_rules(self, rules: list[type[Rule]]) -> None:
        # An empty family's page would describe rules that no longer run, which
        # is the failure mode generation exists to prevent.
        families = {r.meta.family.value for r in rules}
        assert {p.stem for p in DOCS.glob("*.md")} <= families

    def test_an_orphaned_page_fails_the_check(self, tmp_path: Path) -> None:
        # Deliberately not a real family prefix. This was `DJX.md` while DJX
        # had no rules, which made the test stop testing orphaning on the day
        # DJX gained one -- the page was no longer an orphan.
        orphan = DOCS / "DJZ.md"
        assert not orphan.exists()
        orphan.write_text("# DJX\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, str(GENERATOR), "--check"],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            orphan.unlink()
        assert result.returncode == 1
        assert "ORPHANED" in result.stderr

    def test_every_page_says_not_to_edit_by_hand(self) -> None:
        for path in DOCS.glob("*.md"):
            assert "Do not edit by hand" in path.read_text(encoding="utf-8"), path

    def test_every_family_page_names_what_the_family_covers(self, rules) -> None:
        # The one part of a page no rule can supply. A missing blurb would
        # otherwise render as a heading with a blank line under it.
        for path in DOCS.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            heading = next(ln for ln in text.splitlines() if ln.startswith("# "))
            assert heading.startswith(f"# `{path.stem}` — "), heading
            assert len(heading) > len(f"# `{path.stem}` — ") + 10, heading
