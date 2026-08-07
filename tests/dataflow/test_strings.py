"""Tests for string composition analysis.

The property under test is the split between the template and the parts. Every
`DJI` rule about SQL reasons over the parts, so a shape that yields the wrong
parts is a wrong answer in every rule downstream, and a shape that yields none
is a silent miss.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.dataflow.strings import Composition, Interpolation, interpolation


def parse(source: str) -> ast.expr:
    module = ast.parse(source, mode="eval")
    return module.body


def analyse(source: str) -> Interpolation | None:
    return interpolation(parse(source))


def parts(source: str) -> list[str]:
    found = analyse(source)
    assert found is not None, f"{source!r} was not seen as a composition"
    return [ast.unparse(part) for part in found.parts]


class TestWhatItRecognises:
    @pytest.mark.parametrize(
        ("source", "kind"),
        [
            ('f"a{b}c"', Composition.FSTRING),
            ('"a%sc" % b', Composition.PERCENT),
            ('"a" + b', Composition.CONCAT),
            ('"a{}c".format(b)', Composition.FORMAT),
            ('", ".join(b)', Composition.JOIN),
        ],
    )
    def test_each_composition(self, source: str, kind: Composition) -> None:
        found = analyse(source)
        assert found is not None
        assert found.kind is kind

    def test_every_composition_has_phrasing(self) -> None:
        """The message text is derived, so a new shape cannot ship unspoken."""
        for kind in Composition:
            found = Interpolation(kind, parse('"x"'), ())
            assert found.phrasing.strip()


class TestWhatItSplices:
    def test_an_fstring_yields_only_the_replacements(self) -> None:
        assert parts('f"SELECT {col} FROM {table} WHERE 1=1"') == ["col", "table"]

    def test_a_percent_tuple_yields_each_operand(self) -> None:
        assert parts('"WHERE a=%s AND b=%s" % (x, y)') == ["x", "y"]

    def test_a_percent_scalar_yields_the_one_operand(self) -> None:
        assert parts('"WHERE a=%s" % x') == ["x"]

    def test_a_percent_dict_is_taken_whole(self) -> None:
        """We cannot address `%(name)s` to its value, so the mapping is the part."""
        assert parts('"WHERE a=%(n)s" % mapping') == ["mapping"]

    def test_concatenation_flattens_and_drops_literals(self) -> None:
        assert parts('"SELECT " + col + " FROM " + table') == ["col", "table"]

    def test_format_yields_positional_and_keyword_arguments(self) -> None:
        assert parts('"{} {t}".format(col, t=table)') == ["col", "table"]

    def test_join_over_a_display_yields_its_elements(self) -> None:
        assert parts('", ".join([a, b])') == ["a", "b"]

    def test_join_over_repeated_placeholders_yields_the_placeholder(self) -> None:
        """The pretix idiom. The count cannot reach the output at all."""
        assert parts('", ".join(["%s"] * len(rows))') == ["'%s'"]

    def test_join_over_a_comprehension_yields_the_element(self) -> None:
        assert parts('", ".join(f"x({k})" for k in keys)') == ["f'x({k})'"]

    def test_join_over_an_opaque_argument_yields_it_whole(self) -> None:
        assert parts('", ".join(fragments)') == ["fragments"]


class TestWhatItDeclines:
    @pytest.mark.parametrize(
        "source",
        [
            '"SELECT 1"',
            'f"SELECT 1"',
            "b'bytes'",
            "name",
            "obj.attr",
            "call(x)",
            '"a" "b"',
        ],
    )
    def test_a_string_that_was_not_composed(self, source: str) -> None:
        assert analyse(source) is None

    def test_an_fstring_with_no_replacement_field(self) -> None:
        """Python parses it as a JoinedStr, but nothing was spliced in.

        Reporting it would flag a constant for the way its author quoted it.
        """
        assert analyse('f"SELECT * FROM t"') is None

    def test_a_join_with_the_wrong_arity(self) -> None:
        assert analyse('", ".join()') is None

    def test_a_format_with_no_arguments(self) -> None:
        assert analyse('"SELECT 1".format()') is None

    def test_a_concatenation_of_two_literals(self) -> None:
        """Nothing was spliced in, so there is nothing for a rule to judge."""
        assert analyse('"SELECT " + "1"') is None

    def test_subtraction_is_not_concatenation(self) -> None:
        assert analyse("a - b") is None
