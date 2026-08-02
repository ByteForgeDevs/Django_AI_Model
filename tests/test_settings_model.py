"""Tests for the resolved-settings data model."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from djaudit.settings import Definition, Origin, ResolvedSetting
from djaudit.values import Value


def definition(
    name: str = "DEBUG",
    value: Value | None = None,
    dotted: str = "proj.settings.base",
    line: int = 10,
    conditional: bool = False,
) -> Definition:
    node = ast.parse("DEBUG = True").body[0]
    node.lineno = line
    return Definition(
        name=name,
        value=value if value is not None else Value.of(True),
        module=Path(f"{dotted.replace('.', '/')}.py"),
        dotted=dotted,
        node=node,
        conditional=conditional,
    )


class TestDefinition:
    def test_line_comes_from_the_node(self) -> None:
        assert definition(line=42).line == 42

    def test_describe_names_the_module_and_line(self) -> None:
        assert "proj.settings.base:10" in definition().describe()

    def test_describe_marks_a_conditional_assignment(self) -> None:
        assert "conditional" in definition(conditional=True).describe()

    def test_describe_omits_the_marker_when_unconditional(self) -> None:
        assert "conditional" not in definition().describe()


class TestResolvedSetting:
    def test_the_last_definition_wins(self) -> None:
        base = definition(dotted="base", value=Value.of(True))
        prod = definition(dotted="production", value=Value.of(False))
        resolved = ResolvedSetting(
            name="DEBUG",
            value=Value.of(False),
            origin=Origin.EXPLICIT,
            definitions=(base, prod),
        )
        assert resolved.definition is prod
        assert resolved.overridden == (base,)

    def test_a_single_definition_overrides_nothing(self) -> None:
        resolved = ResolvedSetting(
            name="DEBUG",
            value=Value.of(True),
            origin=Origin.EXPLICIT,
            definitions=(definition(),),
        )
        assert resolved.overridden == ()

    def test_a_default_has_no_definition(self) -> None:
        resolved = ResolvedSetting("DEBUG", Value.of(False), Origin.DJANGO_DEFAULT)
        assert resolved.definition is None
        assert resolved.is_default
        assert not resolved.is_explicit

    def test_conditional_reflects_the_winning_assignment(self) -> None:
        resolved = ResolvedSetting(
            name="DEBUG",
            value=Value.of(True),
            origin=Origin.EXPLICIT,
            definitions=(definition(conditional=True), definition(conditional=False)),
        )
        assert not resolved.conditional

    def test_a_setting_with_no_definitions_is_not_conditional(self) -> None:
        assert not ResolvedSetting("X", Value.of(1), Origin.DJANGO_DEFAULT).conditional

    @pytest.mark.parametrize(
        ("value", "always", "maybe"),
        [
            (Value.of(True), True, True),
            (Value.of(False), False, False),
            (Value.unknown("hidden"), False, True),
        ],
    )
    def test_queries_delegate_to_the_value(self, value: Value, always: bool, maybe: bool) -> None:
        resolved = ResolvedSetting("DEBUG", value, Origin.EXPLICIT)
        assert resolved.is_always(True) is always
        assert resolved.could_be(True) is maybe

    def test_describe_distinguishes_a_default_from_an_assignment(self) -> None:
        # "never set" and "explicitly set to the same value" are different
        # facts about a project and a finding should not blur them.
        assert (
            "Django default"
            in ResolvedSetting("DEBUG", Value.of(False), Origin.DJANGO_DEFAULT).describe()
        )
        assert (
            "Django default"
            not in ResolvedSetting("DEBUG", Value.of(False), Origin.EXPLICIT).describe()
        )

    def test_describe_an_absent_setting(self) -> None:
        assert "not set" in ResolvedSetting("X", Value.unknown("-"), Origin.ABSENT).describe()

    def test_provenance_lists_the_whole_chain(self) -> None:
        resolved = ResolvedSetting(
            name="DEBUG",
            value=Value.of(False),
            origin=Origin.EXPLICIT,
            definitions=(
                definition(dotted="base", value=Value.of(True)),
                definition(dotted="production", value=Value.of(False)),
            ),
        )
        chain = resolved.provenance()
        assert "base" in chain
        assert "production" in chain
        assert chain.count("|") == 1

    def test_provenance_of_a_default_still_reads_sensibly(self) -> None:
        resolved = ResolvedSetting("DEBUG", Value.of(False), Origin.DJANGO_DEFAULT)
        assert "Django default" in resolved.provenance()
