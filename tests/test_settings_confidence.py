"""The confidence policy.

Confidence is the field CI gates on, so it has to mean the same thing in every
rule. These tests pin the mapping down rather than leaving it to each rule's
judgement.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from djaudit.models import Confidence
from djaudit.settings import (
    Assessment,
    Definition,
    Origin,
    ResolvedSetting,
    assess,
    lower_confidence,
)
from djaudit.values import Value


def resolved(
    value: Value,
    *,
    origin: Origin = Origin.EXPLICIT,
    conditional: bool = False,
) -> ResolvedSetting:
    definition = Definition(
        name="DEBUG",
        value=value,
        module=Path("settings.py"),
        dotted="settings",
        node=ast.parse("DEBUG = True").body[0],
        conditional=conditional,
    )
    return ResolvedSetting(
        name="DEBUG",
        value=value,
        origin=origin,
        definitions=(definition,),
    )


class TestLowerConfidence:
    def test_one_step(self) -> None:
        assert lower_confidence(Confidence.CERTAIN) is Confidence.FIRM

    def test_several_steps(self) -> None:
        assert lower_confidence(Confidence.CERTAIN, 2) is Confidence.TENTATIVE

    def test_it_floors_at_tentative(self) -> None:
        assert lower_confidence(Confidence.CERTAIN, 99) is Confidence.TENTATIVE

    def test_zero_steps_is_identity(self) -> None:
        assert lower_confidence(Confidence.FIRM, 0) is Confidence.FIRM


class TestAssess:
    def test_a_plain_literal_is_certain(self) -> None:
        assert assess(resolved(Value.of(True))).confidence is Confidence.CERTAIN

    def test_a_plain_literal_has_nothing_to_caveat(self) -> None:
        assert assess(resolved(Value.of(True))).caveats == ()

    def test_an_environment_read_is_firm(self) -> None:
        value = Value.of(True, env_dependent=True)
        assert assess(resolved(value)).confidence is Confidence.FIRM

    def test_a_conditional_assignment_is_firm(self) -> None:
        assert assess(resolved(Value.of(True), conditional=True)).confidence is Confidence.FIRM

    def test_a_django_default_is_firm(self) -> None:
        graded = assess(resolved(Value.of(True), origin=Origin.DJANGO_DEFAULT))
        assert graded.confidence is Confidence.FIRM

    def test_downgrades_compound(self) -> None:
        # Environment-dependent AND conditional is weaker than either alone.
        value = Value.of(True, env_dependent=True)
        assert assess(resolved(value, conditional=True)).confidence is Confidence.TENTATIVE

    def test_a_conditional_value_is_tentative(self) -> None:
        value = Value.conditional([Value.of(True), Value.of(False)])
        assert assess(resolved(value)).confidence is Confidence.TENTATIVE

    def test_an_unresolved_setting_is_tentative(self) -> None:
        assert assess(resolved(Value.unknown("opaque"), origin=Origin.UNRESOLVED)).confidence is (
            Confidence.TENTATIVE
        )

    def test_an_unresolved_setting_says_so(self) -> None:
        assert assess(resolved(Value.unknown("opaque"), origin=Origin.UNRESOLVED)).caveats == (
            "value could not be determined statically",
        )

    def test_the_ceiling_caps_a_perfect_resolution(self) -> None:
        graded = assess(resolved(Value.of(True)), ceiling=Confidence.FIRM)
        assert graded.confidence is Confidence.FIRM

    def test_the_ceiling_still_degrades(self) -> None:
        value = Value.of(True, env_dependent=True)
        assert assess(resolved(value), ceiling=Confidence.FIRM).confidence is Confidence.TENTATIVE

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"conditional": True}, "conditional block"),
            ({"origin": Origin.DJANGO_DEFAULT}, "Django's default"),
        ],
    )
    def test_each_downgrade_explains_itself(self, kwargs: object, expected: str) -> None:
        graded = assess(resolved(Value.of(True), **kwargs))  # type: ignore[arg-type]
        assert any(expected in caveat for caveat in graded.caveats)

    def test_an_environment_read_explains_itself(self) -> None:
        graded = assess(resolved(Value.of(True, env_dependent=True)))
        assert any("environment" in caveat for caveat in graded.caveats)


class TestNote:
    def test_no_caveats_produces_nothing(self) -> None:
        assert Assessment(Confidence.CERTAIN).note() == ""

    def test_caveats_are_joined(self) -> None:
        assert Assessment(Confidence.FIRM, ("a", "b")).note() == " (a; b)"
