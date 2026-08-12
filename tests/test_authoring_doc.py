"""Tests for the rule authoring guide's gate.

The guide is the one document whose claims are all executable, so the gate
executes them rather than reading them. These tests are about the gate: that it
runs what it says it runs, and that each kind of drift it claims to catch is a
kind of drift it does catch.

Writing the gate found the guide promising `ctx.tree()`, an accessor that has
never existed -- the real one is `ctx.parse()`. Writing these tests found that
the gate loaded the registry too late to notice a reused rule id, so a guide
that stole a shipped id crashed inside the engine with a traceback instead of
being told what was wrong.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_authoring_doc  # noqa: E402

from djaudit.registry import RuleMeta, all_rules  # noqa: E402

DOC = ROOT / "docs" / "authoring-rules.md"


@pytest.fixture
def text() -> str:
    return DOC.read_text(encoding="utf-8")


class TestTheGuideItself:
    def test_it_passes_its_own_gate(self) -> None:
        assert check_authoring_doc.check() == 0

    def test_every_rulemeta_field_is_documented(self, text: str) -> None:
        documented = check_authoring_doc._documented_fields(text)
        for field in dataclasses.fields(RuleMeta):
            assert field.name in documented, f"{field.name} is undocumented"

    def test_it_carries_runnable_python(self, text: str) -> None:
        assert check_authoring_doc._blocks(text), "no python blocks at all"

    def test_the_example_id_is_not_a_shipped_rule(self) -> None:
        """A guide that steals an id would break every audit that imports it."""
        assert check_authoring_doc.EXAMPLE_ID not in {r.meta.id for r in all_rules()}

    def test_the_example_does_not_leak_into_the_registry(self) -> None:
        """The gate registers a rule; it must put the registry back."""
        before = {r.meta.id for r in all_rules()}
        check_authoring_doc.check()
        assert {r.meta.id for r in all_rules()} == before


class TestItCatchesDrift:
    """Each case is a real edit to the guide, run through the real gate."""

    @pytest.fixture
    def run_with(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        def run(old: str, new: str) -> int:
            source = DOC.read_text(encoding="utf-8")
            assert old in source, f"{old!r} is not in the guide, so nothing would change"
            copy = tmp_path / "authoring-rules.md"
            copy.write_text(source.replace(old, new, 1), encoding="utf-8")
            monkeypatch.setattr(check_authoring_doc, "DOC", copy)
            return check_authoring_doc.check()

        return run

    def test_a_renamed_context_accessor(self, run_with) -> None:
        assert run_with("ctx.parse(path)", "ctx.syntax_tree(path)") == 1

    def test_an_invented_enum_member(self, run_with) -> None:
        assert run_with("EvidenceKind.SQL", "EvidenceKind.POSTGRES") == 1

    def test_a_repository_path_that_does_not_exist(self, run_with) -> None:
        assert run_with("`scripts/fixture_controls_probe.py`", "`scripts/nope.py`") == 1

    def test_an_undocumented_field(self, run_with) -> None:
        assert run_with("| `limitations` |", "| `limitationz` |") == 1

    def test_an_example_that_stops_detecting(self, run_with) -> None:
        assert (
            run_with("if not isinstance(node, ast.Assert):", "if isinstance(node, ast.Assert):")
            == 1
        )

    def test_an_example_that_points_at_the_wrong_node(self, run_with) -> None:
        assert run_with("ctx.location(path, node)", "ctx.location(path, tree)") == 1

    def test_an_example_that_reuses_a_shipped_id(self, run_with) -> None:
        """Diagnosed rather than crashed: the registry is loaded before the exec."""
        assert run_with('id="DJS-900"', 'id="DJS-001"') == 1

    def test_an_unmodified_copy_still_passes(self, run_with) -> None:
        """The presence control: the harness itself does not fail the guide."""
        assert run_with("# Writing a rule", "# Writing a rule") == 0
