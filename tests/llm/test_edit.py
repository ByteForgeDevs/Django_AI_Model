"""Tests for verified byte-range edits."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

import pytest

from djaudit.llm.edit import (
    Edit,
    EditError,
    _prove_only_the_ranges_moved,
    apply,
    diff,
    line_starts,
    offset_of,
    replace_value,
    span,
    touched_lines,
    verified_span,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def value_of(source: str, name: str) -> ast.expr:
    """The right-hand side of the first `name = ...` in `source`."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"no assignment to {name}")


class TestOffsets:
    def test_line_starts_lands_on_each_line(self) -> None:
        source = "a = 1\nbb = 2\n\nc = 3\n"

        assert line_starts(source) == [0, 6, 13, 14, 20]

    def test_a_non_ascii_character_earlier_on_the_line_does_not_shift_the_offset(self) -> None:
        """`ast` columns are UTF-8 byte offsets, and this is where that bites.

        `NAME = "\u00e9\u00e9\u00e9"` puts three two-byte characters before the
        target, so a naive `len(line[:col])` lands three characters late --
        still inside a string, still parseable, and pointing at the wrong text.
        """
        source = 'LABEL = "\u00e9\u00e9\u00e9"\nDEBUG = True\n'
        node = value_of(source, "DEBUG")

        start, end = span(source, node)

        assert source[start:end] == "True"

    def test_a_non_ascii_character_on_the_same_line_as_the_target(self) -> None:
        source = 'CONFIG = {"caf\u00e9": True, "tea": False}\n'
        mapping = value_of(source, "CONFIG")
        assert isinstance(mapping, ast.Dict)

        spans = [span(source, v) for v in mapping.values]

        assert [source[a:b] for a, b in spans] == ["True", "False"]

    def test_a_line_past_the_end_is_refused(self) -> None:
        with pytest.raises(EditError, match="line 99 is not in a file"):
            offset_of("a = 1\n", 99, 0)


class TestVerifiedSpan:
    def test_a_plain_value_verifies(self) -> None:
        source = "DEBUG = True\n"

        located = verified_span(source, value_of(source, "DEBUG"))

        assert located is not None
        assert source[located[0] : located[1]] == "True"

    def test_a_multiline_call_verifies(self) -> None:
        source = "X = dict(\n    a=1,\n    b=2,\n)\n"

        located = verified_span(source, value_of(source, "X"))

        assert located is not None
        assert source[located[0] : located[1]] == "dict(\n    a=1,\n    b=2,\n)"

    def test_a_parenthesised_multiline_expression_is_refused(self) -> None:
        """The measured 0.21%.

        `ast` reports the range of the `|` expression and not the parentheses
        that hold its two lines together, so the slice is a fragment. It must
        come back `None`, not come back wrong.
        """
        source = "Q = (Q(a=1) |\n     Q(b=2))\n"

        assert verified_span(source, value_of(source, "Q")) is None

    def test_an_implicitly_concatenated_string_is_refused(self) -> None:
        source = 'MESSAGE = ("first part "\n           "second part")\n'

        assert verified_span(source, value_of(source, "MESSAGE")) is None

    def test_a_node_from_another_file_is_refused_rather_than_mislocated(self) -> None:
        """Positions from one text mean nothing in another."""
        other = "\n\n\n\n\n\n\n\n\nVALUE = some_name\n"
        node = value_of(other, "VALUE")

        assert verified_span("SHORT = 1\n", node) is None

    def test_a_slice_that_parses_but_says_something_else_is_refused(self) -> None:
        """The case the round-trip comparison actually guards.

        Every other refusal here is reached by an exception -- an unlocatable
        line, or a fragment that will not parse. This is the one where the
        slice is perfectly good Python at exactly the right offsets and simply
        is not the node asked about. Both texts are the same length and shape,
        so the range is valid in both; only the comparison can tell them apart.
        A mutation round found nothing else reaching it.
        """
        node = value_of("X = beta1\n", "X")

        assert verified_span("X = alpha\n", node) is None

    def test_the_comparison_is_of_meaning_and_not_of_kind(self) -> None:
        """Same node type, different value: `type(a) is type(b)` would pass."""
        node = value_of("X = 1234\n", "X")

        assert verified_span("X = 9999\n", node) is None

    def test_two_texts_that_do_agree_are_accepted(self) -> None:
        """The contrast: refusing everything would satisfy the two above."""
        node = value_of("X = alpha\n", "X")

        assert verified_span("X = alpha\n", node) == (4, 9)

    def test_a_synthesised_node_has_no_position(self) -> None:
        with pytest.raises(EditError, match="carries no position"):
            span("x = 1\n", ast.Name(id="x"))


class TestApply:
    def test_it_replaces_only_the_range(self) -> None:
        source = "# a comment\nDEBUG = True  # trailing\n\n\nOTHER = 1\n"
        edit = replace_value(source, value_of(source, "DEBUG"), "False", "hardening")

        result = apply(source, [edit])

        assert result == "# a comment\nDEBUG = False  # trailing\n\n\nOTHER = 1\n"

    def test_comments_blank_lines_and_spacing_all_survive(self) -> None:
        """The reason this exists rather than `ast.unparse`."""
        source = (
            "import os\n\n\n"
            "# Keep this off in production.\n"
            "DEBUG   =    True\n\n"
            "ALLOWED_HOSTS = [\n"
            "    # the staging box\n"
            "    'example.test',\n"
            "]\n"
        )
        edit = replace_value(source, value_of(source, "DEBUG"), "False", "hardening")

        result = apply(source, [edit])

        assert result == source.replace("True", "False")
        assert "# the staging box" in result
        assert "DEBUG   =    False" in result

    def test_several_edits_land_together(self) -> None:
        source = "A = 1\nB = 2\nC = 3\n"
        edits = [
            replace_value(source, value_of(source, name), repr(n), "why")
            for name, n in (("A", 10), ("C", 30))
        ]

        assert apply(source, edits) == "A = 10\nB = 2\nC = 30\n"

    def test_edits_apply_in_any_order_given(self) -> None:
        source = "A = 1\nB = 2\nC = 3\n"
        edits = [
            replace_value(source, value_of(source, name), repr(n), "why")
            for name, n in (("C", 30), ("A", 10))
        ]

        assert apply(source, edits) == apply(source, list(reversed(edits)))

    def test_an_insertion_keeps_both_sides(self) -> None:
        source = "objects.all()\n"
        at = source.index(".all()")

        assert apply(source, [Edit(at, at, ".select_related('x')", "n+1")]) == (
            "objects.select_related('x').all()\n"
        )

    def test_no_edits_is_the_original(self) -> None:
        source = "A = 1\n"

        assert apply(source, []) == source

    def test_overlapping_edits_are_refused(self) -> None:
        source = "VALUE = foo(bar)\n"
        outer = replace_value(source, value_of(source, "VALUE"), "baz", "why")
        inner = Edit(outer.start + 4, outer.start + 7, "qux", "why")

        with pytest.raises(EditError, match="overlap"):
            apply(source, [outer, inner])

    def test_two_insertions_at_one_point_are_refused(self) -> None:
        """Their order would decide the result, and nothing defines it."""
        with pytest.raises(EditError, match="overlap"):
            apply("abc\n", [Edit(1, 1, "X", "one"), Edit(1, 1, "Y", "two")])

    def test_adjacent_edits_are_allowed(self) -> None:
        assert apply("abcd\n", [Edit(0, 2, "X", "one"), Edit(2, 4, "Y", "two")]) == "XY\n"

    def test_an_edit_past_the_end_is_refused(self) -> None:
        with pytest.raises(EditError, match="past the end"):
            apply("abc\n", [Edit(2, 99, "X", "why")])

    def test_a_backwards_range_is_refused_at_construction(self) -> None:
        with pytest.raises(EditError, match="cannot span"):
            Edit(9, 2, "X", "why")

    def test_the_untouched_proof_is_load_bearing(self) -> None:
        """Exercise the invariant `apply` checks on itself.

        A caller cannot make `apply` splice wrongly -- that would be a bug in
        `apply`. So the check is driven directly: the same edit set, once
        against an honest result and once against one that also changed a
        character the edits never covered.
        """
        edits = [Edit(0, 1, "Z", "why")]

        _prove_only_the_ranges_moved("abc\n", "Zbc\n", edits)

        with pytest.raises(EditError, match="outside their ranges"):
            _prove_only_the_ranges_moved("abc\n", "ZbX\n", edits)

    def test_apply_actually_runs_the_proof(self) -> None:
        """That the helper works does not mean anything calls it.

        Deleting the call from `apply` left every direct test of the helper
        green. So the call site is pinned separately: make the proof raise, and
        require `apply` to carry that out.
        """
        import djaudit.llm.edit as module

        def refuse(source: str, result: str, ordered: Sequence[Edit]) -> None:
            raise EditError("PROOF RAN")

        original = module._prove_only_the_ranges_moved
        module._prove_only_the_ranges_moved = refuse
        try:
            with pytest.raises(EditError, match="PROOF RAN"):
                module.apply("abc\n", [Edit(0, 1, "Z", "why")])
        finally:
            module._prove_only_the_ranges_moved = original

        assert module.apply("abc\n", [Edit(0, 1, "Z", "why")]) == "Zbc\n"

    def test_the_untouched_proof_catches_a_short_splice(self) -> None:
        """Dropping a character after the edit, rather than altering one."""
        with pytest.raises(EditError, match="outside their ranges"):
            _prove_only_the_ranges_moved("abcd\n", "Zbc\n", [Edit(0, 1, "Z", "why")])


class TestReplaceValue:
    def test_a_replacement_that_is_not_an_expression_is_refused(self) -> None:
        source = "DEBUG = True\n"

        with pytest.raises(EditError, match="is not an expression"):
            replace_value(source, value_of(source, "DEBUG"), "if x:", "why")

    def test_a_statement_masquerading_as_a_value_is_refused(self) -> None:
        source = "DEBUG = True\n"

        with pytest.raises(EditError, match="is not an expression"):
            replace_value(source, value_of(source, "DEBUG"), "DEBUG = False", "why")

    def test_an_unlocatable_value_is_refused_rather_than_guessed(self) -> None:
        source = "Q = (Q(a=1) |\n     Q(b=2))\n"

        with pytest.raises(EditError, match="cannot be located exactly"):
            replace_value(source, value_of(source, "Q"), "None", "why")

    def test_the_reason_travels_with_the_edit(self) -> None:
        source = "DEBUG = True\n"

        edit = replace_value(source, value_of(source, "DEBUG"), "False", "DJS-001")

        assert edit.why == "DJS-001"


class TestTouchedLines:
    def test_it_reports_the_line_an_edit_lands_on(self) -> None:
        source = "A = 1\nB = 2\nC = 3\n"
        edit = replace_value(source, value_of(source, "B"), "9", "why")

        assert touched_lines(source, [edit]) == {2}

    def test_a_multiline_edit_reports_every_line(self) -> None:
        source = "A = 1\nX = dict(\n    a=1,\n)\nC = 3\n"
        edit = replace_value(source, value_of(source, "X"), "{}", "why")

        assert touched_lines(source, [edit]) == {2, 3, 4}

    def test_an_insertion_reports_one_line(self) -> None:
        source = "A = 1\nB = 2\n"
        at = source.index("B")

        assert touched_lines(source, [Edit(at, at, "# ", "why")]) == {2}


class TestDiff:
    def test_no_change_is_no_diff(self) -> None:
        assert diff("A = 1\n", "A = 1\n", "settings.py") == ""

    def test_it_names_both_sides_for_git_apply(self) -> None:
        text = diff("A = 1\n", "A = 2\n", "conf/settings.py")

        assert "--- a/conf/settings.py" in text
        assert "+++ b/conf/settings.py" in text
        assert "-A = 1" in text
        assert "+A = 2" in text

    def test_it_shows_context_around_the_change(self) -> None:
        before = "".join(f"L{i}\n" for i in range(10))
        after = before.replace("L5\n", "L5x\n")

        text = diff(before, after, "f.py")

        assert "L2" in text
        assert "L8" in text
        assert "L0" not in text

    def test_a_file_with_no_final_newline_still_ends_with_one(self) -> None:
        text = diff("A = 1", "A = 2", "f.py")

        assert text.endswith("\n")


class TestAgainstRealSource:
    """The measurement that decided against a CST dependency, as a gate.

    The module docstring cites 99.790% across 86,783 assignments in the three
    benchmark targets. Those are not in the repo, so this re-runs the same
    procedure over the fixtures -- enough to fail if `verified_span` ever starts
    accepting a range it cannot prove, or rejecting ordinary code.
    """

    def measure(self) -> tuple[int, int, list[str]]:
        exact = refused = 0
        wrong: list[str] = []
        for path in sorted(FIXTURES.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                located = verified_span(source, node.value)
                if located is None:
                    refused += 1
                    continue
                sliced = source[located[0] : located[1]]
                if ast.dump(ast.parse(sliced, mode="eval").body) == ast.dump(node.value):
                    exact += 1
                else:
                    wrong.append(f"{path.name}:{node.lineno} {sliced!r}")
        return exact, refused, wrong

    def test_every_accepted_span_really_round_trips(self) -> None:
        exact, _, wrong = self.measure()

        assert wrong == []
        assert exact > 200

    def test_almost_nothing_is_refused(self) -> None:
        """A `verified_span` that refused everything would also pass the above."""
        exact, refused, _ = self.measure()

        assert refused / (exact + refused) < 0.05

    def test_an_edit_to_a_real_settings_file_changes_one_line(self) -> None:
        path = FIXTURES / "vulnerable_project" / "config" / "settings" / "base.py"
        source = path.read_text(encoding="utf-8")
        node = value_of(source, "DEBUG")

        result = apply(source, [replace_value(source, node, "False", "DJS-001")])

        changed = [
            line for line in diff(source, result, path.name).splitlines() if line[:1] in "+-"
        ]
        assert [c for c in changed if not c.startswith(("---", "+++"))] == [
            "-DEBUG = True",
            "+DEBUG = False",
        ]
