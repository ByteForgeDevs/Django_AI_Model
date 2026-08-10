"""The adapter interface: what it refuses, and what it does when a tool is gone.

The tests that matter here are not the mapping ones. They are the two the
whole layer exists for: a claim that is not a decision must not be
constructible, and a code nobody ruled on must not vanish.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from djaudit.adapters.base import (
    Availability,
    Claim,
    Claimed,
    ClaimTable,
    External,
    Report,
    as_finding,
    is_test_path,
)
from djaudit.adapters.process import PROBE_TIMEOUT, probe_tool, read_version
from djaudit.models import Confidence, EvidenceKind, Family, Severity, Tier

RUFF = Availability(tool="ruff", version="0.16.1")


def hit(code: str, file: str = "hc/accounts/models.py", line: int = 104) -> External:
    return External(code=code, message=f"{code} happened", file=file, line=line)


ADOPTED = Claimed(
    code="S324",
    claim=Claim.ADOPT,
    why="no djaudit rule reads hashlib call sites",
    title="Insecure hash function",
    family=Family.DJS,
    severity=Severity.MEDIUM,
)


class TestAClaimMustBeADecision:
    """Each of these was a comment in a table until it was a `ValueError`."""

    def test_a_claim_without_a_reason_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a decision"):
            Claimed(code="S101", claim=Claim.REJECTED, why="")

    def test_subsumed_must_name_the_rule_that_covers_it(self) -> None:
        # Otherwise "subsumed" is indistinguishable from "deleted", and the
        # claim that our answer is better is unfalsifiable.
        with pytest.raises(ValueError, match="must name the djaudit rule"):
            Claimed(code="DJ001", claim=Claim.SUBSUMED, why="DJD-002 covers this")

    def test_subsumed_naming_a_rule_is_accepted(self) -> None:
        claimed = Claimed(
            code="DJ001", claim=Claim.SUBSUMED, why="DJD-002 covers this", ours=("DJD-002",)
        )
        assert claimed.ours == ("DJD-002",)
        assert not claimed.adopted

    def test_rejected_may_not_name_one_of_our_rules(self) -> None:
        # A code our own rule covers is subsumed. Filing it as noise would
        # record the wrong reason for the same outcome, and the reason is the
        # only part anybody reads later.
        with pytest.raises(ValueError, match="subsumed, not rejected"):
            Claimed(code="S101", claim=Claim.REJECTED, why="tests", ours=("DJS-001",))

    @pytest.mark.parametrize(
        ("missing", "kwargs"),
        [
            ("a title", {"family": Family.DJS, "severity": Severity.LOW}),
            ("a family", {"title": "t", "severity": Severity.LOW}),
            ("a severity", {"title": "t", "family": Family.DJS}),
        ],
    )
    def test_an_adopted_code_needs_enough_to_be_ranked(
        self, missing: str, kwargs: dict[str, object]
    ) -> None:
        with pytest.raises(ValueError, match=missing):
            Claimed(code="S324", claim=Claim.ADOPT, why="worth having", **kwargs)  # type: ignore[arg-type]

    def test_an_empty_code_is_refused(self) -> None:
        with pytest.raises(ValueError, match="needs a code"):
            Claimed(code="", claim=Claim.REJECTED, why="noise")

    def test_a_code_claimed_twice_is_refused(self) -> None:
        # Two rows disagreeing about one code is a merge artefact, and
        # whichever `dict` happened to win would decide it silently. Both rows
        # are individually valid, so this can only be caught by the table.
        with pytest.raises(ValueError, match="S101 claimed more than once"):
            ClaimTable(
                tool="ruff",
                claims=(
                    Claimed(code="S101", claim=Claim.REJECTED, why="tests are made of assert"),
                    Claimed(
                        code="S101",
                        claim=Claim.SUBSUMED,
                        why="second thoughts",
                        ours=("DJS-001",),
                    ),
                ),
            )


class TestACodeNobodyRuledOn:
    """The case a point release creates, and the one silence would hide."""

    def test_an_unclaimed_code_is_named_rather_than_dropped(self) -> None:
        table = ClaimTable(tool="ruff", claims=(ADOPTED,))
        assert table.unclaimed((hit("S324"), hit("S999"))) == ("S999",)

    def test_an_unclaimed_code_is_not_reported_as_a_finding(self) -> None:
        # Named, but not adopted. Surfacing it as a task is the point;
        # shipping a finding nobody read would be the other silent failure.
        table = ClaimTable(tool="ruff", claims=(ADOPTED,))
        assert [f.rule_id for f in table.adopt((hit("S999"),), RUFF)] == []

    def test_a_new_code_seen_only_in_tests_is_not_a_task(self) -> None:
        # Presence control below: the same code outside `tests/` is a task.
        table = ClaimTable(tool="ruff", claims=(ADOPTED,))
        assert table.unclaimed((hit("S999", "hc/tests/test_login.py"),)) == ()

    def test_the_same_new_code_outside_tests_is_a_task(self) -> None:
        table = ClaimTable(tool="ruff", claims=(ADOPTED,))
        assert table.unclaimed((hit("S999", "hc/accounts/views.py"),)) == ("S999",)


class TestWhatItLeavesOut:
    @pytest.mark.parametrize(
        "path",
        [
            "hc/tests/test_login.py",
            "tests/conftest.py",
            "src/pretix/testing/helpers.py",
            "hc/api/test/test_ping.py",
            "hc/api/views_test.py",
            "conftest.py",
        ],
    )
    def test_test_code_is_excluded(self, path: str) -> None:
        assert is_test_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "hc/tests/helpers.py",
            "hc/tests/factories.py",
            "src/pretix/test/util.py",
            "test/support.py",
        ],
    )
    def test_a_plain_file_inside_a_test_directory_is_excluded(self, path: str) -> None:
        # Every path in the list above also has a `test_`-shaped *filename*, so
        # the filename check alone satisfies all of them and the directory check
        # is never the thing deciding. These are named so ordinarily that only
        # the directory can exclude them, which is what makes each entry of
        # `TEST_DIRECTORIES` load-bearing rather than decorative.
        assert is_test_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "hc/accounts/models.py",
            "src/pretix/base/models/invoices.py",
            "hc/front/views.py",
            "protest/views.py",
            "contest.py",
        ],
    )
    def test_production_code_is_not(self, path: str) -> None:
        # `protest` and `contest` are the reason this is a path-part check and
        # not a substring one.
        assert not is_test_path(path)

    def test_an_adopted_code_in_a_test_file_is_still_dropped(self) -> None:
        table = ClaimTable(tool="ruff", claims=(ADOPTED,))
        assert table.adopt((hit("S324", "hc/tests/test_hash.py"),), RUFF) == ()

    def test_the_same_code_outside_tests_is_kept(self) -> None:
        table = ClaimTable(tool="ruff", claims=(ADOPTED,))
        assert len(table.adopt((hit("S324", "hc/lib/hash.py"),), RUFF)) == 1

    def test_a_subsumed_code_is_dropped(self) -> None:
        table = ClaimTable(
            tool="ruff",
            claims=(
                ADOPTED,
                Claimed(
                    code="DJ001",
                    claim=Claim.SUBSUMED,
                    why="DJD-002 reports once per model",
                    ours=("DJD-002",),
                ),
            ),
        )
        found = table.adopt((hit("DJ001"), hit("S324")), RUFF)
        assert [f.properties["code"] for f in found] == ["S324"]


class TestWhatAnAdoptedFindingLooksLike:
    def test_the_rule_id_names_the_tool(self) -> None:
        # A reader must be able to tell at a glance that nothing in
        # `src/djaudit/rules/` decided this.
        assert as_finding(hit("S324"), ADOPTED, RUFF).rule_id == "RUFF-S324"

    def test_it_cannot_collide_with_one_of_our_own_rule_ids(self) -> None:
        found = as_finding(hit("S324"), ADOPTED, RUFF)
        assert not found.rule_id.startswith(tuple(f.value for f in Family))

    def test_the_evidence_names_the_tool_and_its_version(self) -> None:
        evidence = as_finding(hit("S324"), ADOPTED, RUFF).evidence[0]
        assert evidence.kind is EvidenceKind.COMMAND_OUTPUT
        assert evidence.source == "ruff 0.16.1"
        assert evidence.content == "S324: S324 happened"

    def test_it_takes_the_family_the_claim_declared(self) -> None:
        found = as_finding(hit("S324"), ADOPTED, RUFF)
        assert found.family is Family.DJS
        assert found.severity is Severity.MEDIUM
        assert found.confidence is Confidence.FIRM
        assert found.tier is Tier.STATIC

    def test_the_properties_say_where_it_came_from(self) -> None:
        assert as_finding(hit("S324"), ADOPTED, RUFF).properties == {
            "external": "ruff",
            "code": "S324",
        }

    def test_a_url_becomes_a_reference_and_its_absence_leaves_none(self) -> None:
        with_url = External(
            code="S324", message="m", file="a.py", line=1, url="https://example.invalid/s324"
        )
        assert as_finding(with_url, ADOPTED, RUFF).references == ("https://example.invalid/s324",)
        assert as_finding(hit("S324"), ADOPTED, RUFF).references == ()


class TestWhenTheToolIsNotThere:
    """djaudit works without any of these, and this is where that is proven."""

    def test_a_missing_tool_is_a_value_rather_than_an_exception(self) -> None:
        found = probe_tool("djaudit-no-such-tool")
        assert not found
        assert found.reason == "`djaudit-no-such-tool` is not on PATH"

    def test_a_missing_tool_explains_itself(self) -> None:
        assert probe_tool("djaudit-no-such-tool").describe() == (
            "djaudit-no-such-tool unavailable: `djaudit-no-such-tool` is not on PATH"
        )

    def test_a_tool_that_is_present_is_truthy_and_carries_a_version(self) -> None:
        # Presence control for the tests above. ruff is a dev dependency, so
        # this is the one tool the suite can rely on finding.
        found = probe_tool("ruff")
        assert found, found.reason
        assert found.version
        assert found.describe() == f"ruff {found.version}"

    def test_a_tool_that_exits_non_zero_is_unavailable_rather_than_fatal(self) -> None:
        found = probe_tool("ruff", argument="--definitely-not-a-flag")
        assert not found
        assert "exited" in found.reason

    def test_the_probe_timeout_is_short_enough_to_be_a_probe(self) -> None:
        assert PROBE_TIMEOUT <= 10.0


class TestReadingAVersionString:
    @pytest.mark.parametrize(
        ("tool", "stdout", "expected"),
        [
            # All three measured on this machine. bandit prints a second line
            # naming its Python, which is why only the first is read.
            ("ruff", "ruff 0.16.1\n", "0.16.1"),
            ("bandit", "bandit 1.9.4\n  python version = 3.13.9 (main)\n", "1.9.4"),
            ("pip-audit", "pip-audit 2.10.1\n", "2.10.1"),
            ("mystery", "", ""),
            ("mystery", "version 4 of the mystery tool\n", "version 4 of the mystery tool"),
            ("mystery", "9.9.9\n", "9.9.9"),
        ],
    )
    def test_the_measured_shapes_are_read(self, tool: str, stdout: str, expected: str) -> None:
        assert read_version(stdout, tool) == expected

    def test_bandits_second_line_is_not_mistaken_for_the_version(self) -> None:
        # The failure this prevents is silent: a version of "(main)" in the
        # evidence of every bandit finding.
        assert "python" not in read_version("bandit 1.9.4\n  python version = 3.13.9\n", "bandit")


class TestWhatAReportSays:
    def test_a_report_with_no_availability_did_not_run(self) -> None:
        report = Report(tool="bandit")
        assert not report.ran
        assert report.explain() == "bandit did not run"

    def test_an_unavailable_tool_explains_itself_in_the_report(self) -> None:
        report = Report(
            tool="bandit", availability=Availability(tool="bandit", reason="not on PATH")
        )
        assert not report.ran
        assert report.explain() == "bandit unavailable: not on PATH"

    def test_a_report_that_ran_counts_what_it_contributed(self) -> None:
        table = ClaimTable(tool="ruff", claims=(ADOPTED,))
        found = table.adopt((hit("S324", "hc/lib/hash.py"),), RUFF)
        report = Report(tool="ruff", findings=found, availability=RUFF)
        assert report.ran
        assert report.explain() == "ruff 0.16.1 contributed 1 findings"


class TestTheLocationSurvivesTheMapping:
    def test_the_span_is_carried_across(self) -> None:
        item = External(
            code="S324",
            message="m",
            file="hc/lib/hash.py",
            line=7,
            column=5,
            end_line=7,
            end_column=19,
        )
        location = as_finding(item, ADOPTED, RUFF).location
        assert (location.file, location.line, location.column) == ("hc/lib/hash.py", 7, 5)
        assert (location.end_line, location.end_column) == (7, 19)


class TestWhatTheRefusalActuallySays:
    """The wording, not just the fact of a refusal.

    Every one of these messages is read exactly once: by whoever has just
    written a claim the table would not accept. A `match=` on a few words
    leaves the rest of the sentence unverified, and the parts most worth
    verifying are the ones that name things -- the code, the rules, the
    missing fields -- because those are what turn the message into an
    instruction. Each case supplies *two* names, since a joiner between one
    item and nothing else is invisible.
    """

    def test_a_rejected_code_naming_our_rules_lists_all_of_them(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            Claimed(
                code="DJ001",
                claim=Claim.REJECTED,
                why="noise",
                ours=("DJD-002", "DJD-003"),
            )
        assert str(excinfo.value) == (
            "DJ001: rejected names DJD-002, DJD-003; a code covered "
            "by one of our rules is subsumed, not rejected"
        )

    def test_an_adopted_code_is_told_every_field_it_is_missing(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            Claimed(code="S324", claim=Claim.ADOPT, why="hashlib", severity=Severity.MEDIUM)
        assert str(excinfo.value) == "S324: an adopted code needs a title, a family"

    def test_a_claim_with_no_reason_says_so(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            Claimed(code="S324", claim=Claim.REJECTED, why="")
        assert str(excinfo.value) == "S324: a claim without a reason is not a decision"

    def test_a_subsumed_code_naming_nothing_says_so(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            Claimed(code="DJ001", claim=Claim.SUBSUMED, why="ours is better")
        assert str(excinfo.value) == "DJ001: subsumed must name the djaudit rule that covers it"

    def test_a_claim_with_no_code_says_so(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            Claimed(code="", claim=Claim.REJECTED, why="noise")
        assert str(excinfo.value) == "a claim needs a code"

    def test_every_duplicated_code_is_named_and_the_tool_with_them(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            ClaimTable(
                tool="ruff",
                claims=(
                    Claimed(code="S101", claim=Claim.REJECTED, why="pytest is made of assert"),
                    Claimed(code="S101", claim=Claim.REJECTED, why="again"),
                    Claimed(code="S106", claim=Claim.REJECTED, why="fixtures"),
                    Claimed(code="S106", claim=Claim.REJECTED, why="again"),
                ),
            )
        assert str(excinfo.value) == "ruff: S101, S106 claimed more than once"

    def test_mapping_a_claim_with_no_severity_says_what_is_missing(self) -> None:
        # `adopt` filters to adopted claims, so this guard is only reachable by
        # calling `as_finding` directly -- which is public, and which a future
        # adapter written in a hurry will do.
        half = Claimed(
            code="DJ001",
            claim=Claim.SUBSUMED,
            why="not really adopted",
            ours=("DJD-002",),
            family=Family.DJD,
        )
        with pytest.raises(ValueError) as excinfo:
            as_finding(hit("DJ001"), half, RUFF)
        assert str(excinfo.value) == "DJ001: an adopted code needs a family and a severity"


class TestTheClaimNamesOnTheWire:
    """`Claim` is a `StrEnum`, so these strings are the serialized form.

    Written out as literals rather than derived from the enum, because a test
    that reads the constant it is checking agrees with whatever the constant
    happens to say.
    """

    def test_the_three_decisions_serialize_under_their_own_names(self) -> None:
        assert Claim.ADOPT.value == "adopt"
        assert Claim.SUBSUMED.value == "subsumed"
        assert Claim.REJECTED.value == "rejected"

    def test_there_is_no_fourth_decision_and_no_default(self) -> None:
        assert sorted(c.value for c in Claim) == ["adopt", "rejected", "subsumed"]


class TestADecisionCannotBeEditedAfterTheFact:
    """These are records of a judgement, and a report holds them while it is
    assembled. If one could be rewritten in place, a claim in the report would
    stop being the claim that was reviewed, and `slots` is what makes a typo'd
    attribute an error instead of a silent no-op on a shared object.
    """

    @pytest.mark.parametrize(
        ("value", "attribute"),
        [
            (RUFF, "tool"),
            (ADOPTED, "why"),
            (External(code="S324", message="m", file="a.py", line=1), "file"),
            (ClaimTable(tool="ruff", claims=(ADOPTED,)), "tool"),
            (Report(tool="ruff"), "tool"),
        ],
    )
    def test_it_cannot_be_reassigned(self, value: object, attribute: str) -> None:
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, "rewritten")

    @pytest.mark.parametrize(
        "value",
        [
            RUFF,
            ADOPTED,
            External(code="S324", message="m", file="a.py", line=1),
            ClaimTable(tool="ruff", claims=(ADOPTED,)),
            Report(tool="ruff"),
        ],
    )
    def test_it_carries_no_instance_dictionary(self, value: object) -> None:
        # The absence of `__dict__` is the whole of what `slots` guarantees.
        # Asserting on what a stray assignment *raises* would pin a CPython
        # detail instead: a frozen slots dataclass answers an undeclared name
        # with `TypeError`, out of the stale `__class__` cell that `slots=True`
        # leaves behind in the generated `__setattr__`. That is not a promise
        # this project wants to make.
        assert not hasattr(value, "__dict__")
