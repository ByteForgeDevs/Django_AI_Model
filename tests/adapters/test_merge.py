"""Folding external reports into a run, and why it is done by fingerprint.

The measurement behind `djaudit.adapters.merge` says two things: external
findings never share a location with ours, and findings that *do* share a
location are usually not duplicates. These tests hold both ends of that. The
first is why merging is cheap; the second is why merging by location would be a
bug, so there is a test here that fails if anyone makes location part of
identity again.
"""

from __future__ import annotations

import pytest

from djaudit import fingerprint as fp
from djaudit.adapters.base import Availability, Report
from djaudit.adapters.merge import merge
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)

RUFF = Availability(tool="ruff", version="0.16.1")
GONE = Availability(tool="ruff", version="", reason="not installed")


def finding(
    rule_id: str = "DJD-002",
    *,
    file: str = "app/models.py",
    line: int = 10,
    snippet: str = "name = models.CharField(null=True)",
    severity: Severity = Severity.MEDIUM,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        title="t",
        family=Family.DJD,
        severity=severity,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        location=Location(file=file, line=line, snippet=snippet),
        message="m",
        rationale="r",
        remediation="fix",
        evidence=(Evidence(kind=EvidenceKind.AST, content="c", source=file),),
    )


class TestWhatCountsAsTheSameFinding:
    def test_two_findings_at_one_line_both_survive(self) -> None:
        # The corpus case this exists for: `cp.variation` and `cp.item` on one
        # line of pretix are two unprefetched attributes and two queries per
        # row. Merging by location would report one of them and delete the
        # other, and the deleted one would never be missed by anything.
        # They are indistinguishable in everything a fingerprint reads -- same
        # rule, same file, same line, same source text -- and are told apart
        # only by the occurrence index `identified` hands out.
        both = [finding(rule_id="DJP-001"), finding(rule_id="DJP-001")]
        merged = merge(both, [])
        assert len(merged.findings) == 2
        assert len({f.fingerprint for f in merged.findings}) == 2
        assert merged.duplicates == 0

    def test_one_finding_offered_twice_already_identified_is_held_once(self) -> None:
        # The contrast for the test above. Two findings that arrive already
        # carrying one identity are one finding offered twice, not two things
        # that happen to look alike, and re-deriving their identity here would
        # turn them back into two.
        (only,) = fp.assign([finding()])
        merged = merge([only, only], [])
        assert len(merged.findings) == 1

    def test_an_external_finding_at_one_of_our_lines_survives(self) -> None:
        # 268 of ours against 41 of ruff's over three projects produced no
        # shared line at all, but nothing prevents one, and when it happens the
        # two are different observations about the same code.
        external = finding(rule_id="RUFF-S324", severity=Severity.LOW)
        merged = merge([finding()], [Report(tool="ruff", findings=(external,), availability=RUFF)])
        assert len(merged.findings) == 2
        assert merged.duplicates == 0

    def test_an_external_finding_we_already_hold_is_dropped(self) -> None:
        (shared,) = fp.assign([finding(rule_id="RUFF-S324")])
        merged = merge([shared], [Report(tool="ruff", findings=(shared,), availability=RUFF)])
        assert len(merged.findings) == 1
        assert merged.duplicates == 1

    def test_ours_is_the_one_kept(self) -> None:
        # Same fingerprint, different content. Ours carries a rationale and a
        # remediation written for the rule; a linter's carries one line.
        (mine,) = fp.assign([finding(rule_id="RUFF-S324")])
        theirs = Finding(
            rule_id="RUFF-S324",
            title="theirs",
            family=Family.DJS,
            severity=Severity.LOW,
            confidence=Confidence.FIRM,
            tier=Tier.STATIC,
            location=mine.location,
            message="theirs",
            rationale="",
            remediation="",
        )
        merged = merge([mine], [Report(tool="ruff", findings=(theirs,), availability=RUFF)])
        assert len(merged.findings) == 1
        assert merged.findings[0].title == "t"

    def test_two_adapters_reporting_one_thing_report_it_once(self) -> None:
        shared = finding(rule_id="RUFF-S324")
        merged = merge(
            [],
            [
                Report(tool="ruff", findings=(shared,), availability=RUFF),
                Report(tool="other", findings=(shared,), availability=RUFF),
            ],
        )
        assert len(merged.findings) == 1
        assert merged.duplicates == 1


class TestWhoAssignsIdentity:
    """Adapters do not fingerprint; this module does, and only where it is safe."""

    def test_an_adapter_hands_over_findings_with_no_identity_at_all(self) -> None:
        # The premise everything else here rests on. If adapters ever start
        # fingerprinting, `identified` becomes a no-op and the merge would
        # dedup on whatever they chose instead.
        report = Report(tool="ruff", findings=(finding(rule_id="RUFF-S324"),), availability=RUFF)
        assert [f.fingerprint for f in report.findings] == [""]

    def test_they_are_given_one_on_the_way_in(self) -> None:
        report = Report(tool="ruff", findings=(finding(rule_id="RUFF-S324"),), availability=RUFF)
        assert all(f.fingerprint for f in merge([], [report]).findings)

    def test_an_identity_that_already_exists_is_never_recomputed(self) -> None:
        # A baseline holds fingerprints. Re-deriving one over a different set
        # could hand the same finding a different occurrence index, and every
        # baseline entry written for it would stop matching in silence.
        (mine,) = fp.assign([finding()])
        forged = mine.with_fingerprint("a-fingerprint-a-baseline-holds")
        assert merge([forged], []).findings[0].fingerprint == "a-fingerprint-a-baseline-holds"

    def test_a_half_identified_set_is_refused_rather_than_guessed_at(self) -> None:
        (identified_one,) = fp.assign([finding()])
        with pytest.raises(ValueError, match="fingerprinted and some are not"):
            merge([identified_one, finding(rule_id="DJD-003")], [])

    def test_each_report_is_identified_on_its_own(self) -> None:
        # Two tools reporting one thing is one finding; one tool reporting the
        # same text twice is two. Identifying the whole union at once would get
        # the first wrong, and identifying nothing would get the second wrong.
        same = finding(rule_id="RUFF-S324")
        twice = merge([], [Report(tool="ruff", findings=(same, same), availability=RUFF)])
        across = merge(
            [],
            [
                Report(tool="ruff", findings=(same,), availability=RUFF),
                Report(tool="other", findings=(same,), availability=RUFF),
            ],
        )
        assert (len(twice.findings), twice.duplicates) == (2, 0)
        assert (len(across.findings), across.duplicates) == (1, 1)


class TestTheOrderTheyComeBackIn:
    def test_external_findings_are_ranked_with_ours_not_after_them(self) -> None:
        # An appended block of external findings would rank a low-severity
        # linter note above a critical finding of ours purely by provenance.
        low = finding(rule_id="DJD-002", severity=Severity.LOW, file="a.py")
        high = finding(rule_id="RUFF-S324", severity=Severity.CRITICAL, file="z.py")
        merged = merge([low], [Report(tool="ruff", findings=(high,), availability=RUFF)])
        assert [f.rule_id for f in merged.findings] == ["RUFF-S324", "DJD-002"]

    def test_the_order_does_not_depend_on_which_report_arrived_first(self) -> None:
        one = finding(rule_id="RUFF-S324", file="b.py")
        two = finding(rule_id="RUFF-S113", file="a.py")
        forward = merge([], [Report(tool="ruff", findings=(one, two), availability=RUFF)])
        backward = merge([], [Report(tool="ruff", findings=(two, one), availability=RUFF)])
        assert [f.rule_id for f in forward.findings] == [f.rule_id for f in backward.findings]


class TestWhatTheRunIsToldAboutTheTools:
    def test_a_tool_that_ran_says_what_it_contributed(self) -> None:
        merged = merge([], [Report(tool="ruff", findings=(finding(),), availability=RUFF)])
        assert merged.notices == ("ruff 0.16.1 contributed 1 findings",)

    def test_a_tool_that_could_not_run_still_leaves_a_trace(self) -> None:
        # The contrast for the test above. A short report because a tool was
        # missing and a short report because a project is clean look identical
        # in the findings, and they are not the same situation.
        merged = merge([], [Report(tool="ruff", availability=GONE)])
        assert merged.findings == ()
        assert merged.notices == ("ruff unavailable: not installed",)

    def test_a_tool_s_own_complaints_are_attributed_to_it(self) -> None:
        merged = merge(
            [],
            [Report(tool="pip-audit", availability=RUFF, diagnostics=("requirements.txt: hm",))],
        )
        assert merged.diagnostics == ("pip-audit: requirements.txt: hm",)

    @pytest.mark.parametrize(
        ("codes", "expected"),
        [
            (("S999",), "1 rule code reported but never ruled on: S999"),
            (("S999", "DJ099"), "2 rule codes reported but never ruled on: S999, DJ099"),
        ],
    )
    def test_a_code_nobody_ruled_on_is_named_not_counted(
        self, codes: tuple[str, ...], expected: str
    ) -> None:
        # A count tells a reader that a decision is missing; the codes tell them
        # which decision, which is the only form of the message anyone can act
        # on without re-running the tool by hand.
        merged = merge([], [Report(tool="ruff", availability=RUFF, unclaimed=codes)])
        assert merged.diagnostics == (f"ruff: {expected}",)

    def test_a_tool_with_nothing_to_say_says_nothing(self) -> None:
        merged = merge([], [Report(tool="ruff", availability=RUFF)])
        assert merged.diagnostics == ()
        assert merged.notices == ("ruff 0.16.1 contributed 0 findings",)

    def test_no_adapters_at_all_leaves_our_findings_alone(self) -> None:
        merged = merge([finding()], [])
        assert len(merged.findings) == 1
        assert merged.notices == ()
        assert merged.diagnostics == ()

    def test_the_result_cannot_be_edited_after_it_is_built(self) -> None:
        merged = merge([], [])
        assert not hasattr(merged, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            merged.duplicates = 3  # type: ignore[misc]
