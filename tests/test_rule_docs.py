"""The rule documentation has to be true, and it has to stay true.

``docs/rules/DJS.md`` is generated from the registry, so the only way it can be
wrong is if somebody edits the page instead of the rule, or edits the rule and
forgets to regenerate. Both are caught here rather than in review.

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
from djaudit.models import Family
from djaudit.registry import Rule

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "gen_rule_docs.py"
DOC = ROOT / "docs" / "rules" / "DJS.md"


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
    def test_committed_copy_is_current(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"docs/rules/DJS.md is stale; run scripts/gen_rule_docs.py\n{result.stderr}"
        )

    def test_every_djs_rule_has_a_section(self, rules: list[type[Rule]]) -> None:
        text = DOC.read_text(encoding="utf-8")
        for rule in rules:
            if rule.meta.family is not Family.DJS:
                continue
            assert f"### {rule.meta.id} — {rule.meta.title}" in text

    def test_it_says_not_to_edit_by_hand(self) -> None:
        assert "Do not edit by hand" in DOC.read_text(encoding="utf-8")
