"""Tests for the tri-state Value type.

The distinction these tests protect: "we know it is X", "it might be X", and
"we have no idea" must stay three different answers. Every settings rule in
Phase 1 builds its confidence on them.
"""

from __future__ import annotations

from djaudit.values import UNKNOWN, Value, ValueKind


class TestConstruction:
    def test_literal(self) -> None:
        value = Value.of(True)
        assert value.is_literal
        assert value.possible() == (True,)
        assert value.fully_resolved

    def test_unknown_carries_a_reason(self) -> None:
        value = Value.unknown("call to vault.fetch")
        assert value.is_unknown
        assert value.possible() == ()
        assert not value.fully_resolved
        assert "vault.fetch" in value.describe()

    def test_conditional_of_two_literals(self) -> None:
        value = Value.conditional([Value.of(True), Value.of(False)])
        assert value.is_conditional
        assert value.possible() == (True, False)

    def test_conditional_with_one_distinct_branch_collapses(self) -> None:
        # Both arms agree, so there is nothing conditional about the result and
        # no reason to lower a rule's confidence.
        value = Value.conditional([Value.of(True), Value.of(True)])
        assert value.kind is ValueKind.LITERAL
        assert value.literal is True

    def test_nested_conditionals_are_flattened(self) -> None:
        inner = Value.conditional([Value.of("a"), Value.of("b")])
        outer = Value.conditional([Value.of("c"), inner])
        assert outer.possible() == ("c", "a", "b")
        assert all(not b.is_conditional for b in outer.branches)

    def test_no_branches_is_unknown_not_a_crash(self) -> None:
        assert Value.conditional([]).is_unknown


class TestEnvironmentTaint:
    def test_taint_propagates_through_a_conditional(self) -> None:
        value = Value.conditional([Value.of(True, env_dependent=True), Value.of(False)])
        assert value.env_dependent

    def test_taint_survives_collapsing_to_one_branch(self) -> None:
        value = Value.conditional([Value.of(True, env_dependent=True), Value.of(True)])
        assert value.is_literal
        assert value.env_dependent

    def test_taint_is_visible_in_the_description(self) -> None:
        assert "environment-dependent" in Value.of(True, env_dependent=True).describe()

    def test_a_tainted_literal_is_still_a_literal(self) -> None:
        # env_dependent lowers confidence; it must never change the verdict.
        assert Value.of(True, env_dependent=True).is_always(True)


class TestIsAlways:
    def test_matching_literal(self) -> None:
        assert Value.of(True).is_always(True)

    def test_mismatched_literal(self) -> None:
        assert not Value.of(False).is_always(True)

    def test_every_branch_matching(self) -> None:
        assert Value.conditional([Value.of(True), Value.of(True)]).is_always(True)

    def test_one_branch_differing(self) -> None:
        assert not Value.conditional([Value.of(True), Value.of(False)]).is_always(True)

    def test_unknown_is_never_always(self) -> None:
        assert not UNKNOWN.is_always(True)
        assert not UNKNOWN.is_always(None)

    def test_a_conditional_containing_an_unknown_branch_is_never_always(self) -> None:
        # The unknown branch could be anything, so no universal claim holds.
        value = Value.conditional([Value.of(True), Value.unknown("env")])
        assert not value.is_always(True)


class TestCouldBe:
    def test_matching_literal(self) -> None:
        assert Value.of("secret").could_be("secret")

    def test_mismatched_literal(self) -> None:
        assert not Value.of("a").could_be("b")

    def test_any_branch_matching(self) -> None:
        assert Value.conditional([Value.of(True), Value.of(False)]).could_be(True)

    def test_unknown_could_be_anything(self) -> None:
        # A rule asking "could this be unsafe" about a value it cannot see must
        # hear yes and report tentatively, not hear no and stay silent.
        assert UNKNOWN.could_be(True)
        assert UNKNOWN.could_be("anything at all")

    def test_a_partly_unknown_conditional_could_be_anything(self) -> None:
        value = Value.conditional([Value.of(False), Value.unknown("env")])
        assert value.could_be(True)


class TestBooleanIntegerConfusion:
    """`1 == True` in Python. A settings rule asking a precise question must not
    inherit that."""

    def test_one_is_not_true(self) -> None:
        assert not Value.of(1).is_always(True)
        assert not Value.of(1).could_be(True)

    def test_zero_is_not_false(self) -> None:
        assert not Value.of(0).is_always(False)

    def test_true_is_not_one(self) -> None:
        assert not Value.of(True).is_always(1)

    def test_genuine_integers_still_compare(self) -> None:
        assert Value.of(3600).is_always(3600)


class TestDescribe:
    def test_literal(self) -> None:
        assert Value.of("abc").describe() == "'abc'"

    def test_conditional(self) -> None:
        assert Value.conditional([Value.of(1), Value.of(2)]).describe() == "1 or 2"

    def test_unknown_without_a_reason(self) -> None:
        assert Value(ValueKind.UNKNOWN).describe() == "unknown"


class TestTaintDoesNotSplitBranches:
    def test_agreeing_branches_collapse_despite_differing_provenance(self) -> None:
        # `True if X else env.bool("D", True)` is True either way. Leaving it
        # conditional would lower every downstream rule's confidence for a
        # difference that is provenance, not outcome.
        value = Value.conditional([Value.of(True), Value.of(True, env_dependent=True)])
        assert value.is_literal
        assert value.literal is True
        assert value.env_dependent

    def test_differing_branches_still_stay_conditional(self) -> None:
        value = Value.conditional([Value.of(True), Value.of(False, env_dependent=True)])
        assert value.is_conditional
        assert value.possible() == (True, False)
