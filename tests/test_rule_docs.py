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
sys.path.insert(0, str(ROOT / "scripts"))

import gen_rule_docs  # noqa: E402

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


def family_pages() -> list[Path]:
    """Family pages only. `README.md` is the generated index, not a family."""
    return [p for p in DOCS.glob("*.md") if p.stem != "README"]


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
        assert families <= {p.stem for p in family_pages()}

    def test_no_page_exists_for_a_family_with_no_rules(self, rules: list[type[Rule]]) -> None:
        # An empty family's page would describe rules that no longer run, which
        # is the failure mode generation exists to prevent.
        families = {r.meta.family.value for r in rules}
        assert {p.stem for p in family_pages()} <= families

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
        for path in family_pages():
            text = path.read_text(encoding="utf-8")
            heading = next(ln for ln in text.splitlines() if ln.startswith("# "))
            assert heading.startswith(f"# `{path.stem}` — "), heading
            assert len(heading) > len(f"# `{path.stem}` — ") + 10, heading


class TestTheIndex:
    """A finding names a rule id and nothing else, so lookup is the job."""

    @pytest.fixture
    def index(self) -> str:
        return (DOCS / "README.md").read_text(encoding="utf-8")

    def test_every_rule_appears(self, index: str, rules: list[type[Rule]]) -> None:
        for rule in rules:
            assert f"[`{rule.meta.id}`]" in index, f"{rule.meta.id} is not in the index"

    def test_every_row_carries_the_grade(self, index: str, rules: list[type[Rule]]) -> None:
        """Without severity and confidence the index is a list of names."""
        for rule in rules:
            row = next(ln for ln in index.splitlines() if f"[`{rule.meta.id}`]" in ln)
            assert f"| {rule.meta.severity.value} |" in row, row
            assert f"| {rule.meta.confidence.value} |" in row, row
            assert f"| {rule.meta.tier.value} |" in row, row

    def test_every_family_appears(self, index: str, rules: list[type[Rule]]) -> None:
        for family in {r.meta.family.value for r in rules}:
            assert f"[`{family}`]({family}.md)" in index

    def test_the_counts_are_real(self, index: str, rules: list[type[Rule]]) -> None:
        assert f"{len(rules)} rules across" in index

    def test_every_anchor_lands_on_a_heading(self, index: str) -> None:
        """`slug` reimplements GitHub's rules; a wrong fragment still links."""
        checked = 0
        for line in index.splitlines():
            if ".md#" not in line:
                continue
            target = line.split("](", 1)[1].split(")", 1)[0]
            page, _, anchor = target.partition("#")
            body = (DOCS / page).read_text(encoding="utf-8")
            headings = {
                gen_rule_docs.slug(*h[4:].strip().split(" — ", 1))
                for h in body.splitlines()
                if h.startswith("### ")
            }
            assert anchor in headings, f"{target} matches no heading in {page}"
            checked += 1
        assert checked > 0, "the index emitted no rule anchors at all"

    def test_a_drifting_anchor_is_caught(self) -> None:
        """The generator refuses to write an index whose anchors do not land.

        Fed a crafted pair rather than a monkeypatched `slug`. Patching `slug`
        catches nothing: `render_index` and `_check_anchors` both call it, so
        the two sides drift together and stay consistent. The failure this
        guards against is the two sides disagreeing, which needs one of them
        changed and not the other.
        """
        rendered = {
            DOCS / "README.md": "| [`DJP-001`](DJP.md#wrong-anchor) | t | high | firm | static |",
            DOCS / "DJP.md": "### DJP-001 — Something\n",
        }
        with pytest.raises(SystemExit, match="does not match any heading"):
            gen_rule_docs._check_anchors(rendered)

    def test_a_landing_anchor_is_accepted(self) -> None:
        """The presence control: the same shape, with the anchor corrected."""
        anchor = gen_rule_docs.slug("DJP-001", "Something")
        rendered = {
            DOCS / "README.md": f"| [`DJP-001`](DJP.md#{anchor}) | t | high | firm | static |",
            DOCS / "DJP.md": "### DJP-001 — Something\n",
        }
        gen_rule_docs._check_anchors(rendered)

    def test_a_heading_the_slug_cannot_parse_is_caught(self) -> None:
        rendered = {
            DOCS / "README.md": "| [`DJP-001`](DJP.md#x) | t | high | firm | static |",
            DOCS / "DJP.md": "### DJP-001: Something\n",
        }
        with pytest.raises(SystemExit, match="is not"):
            gen_rule_docs._check_anchors(rendered)

    def test_an_index_link_to_an_ungenerated_page_is_caught(self) -> None:
        rendered = {
            DOCS / "README.md": "| [`DJQ-001`](DJQ.md#x) | t | high | firm | static |",
        }
        with pytest.raises(SystemExit, match="which is not generated"):
            gen_rule_docs._check_anchors(rendered)

    def test_it_says_not_to_edit_by_hand(self, index: str) -> None:
        assert "Do not edit by hand" in index
