"""Tests for business-impact framing.

The load-bearing tests here are the ones that stop the module inventing
anything: every integer in the output must be counted, every caveat must come
from the rule, and every family must be covered so no finding falls through to
a vague default.
"""

from __future__ import annotations

import re

import pytest

from djaudit import engine, registry
from djaudit.llm.impact import (
    Impact,
    blast_radius,
    caveats_for,
    framing,
    impact,
    no_invented_numbers,
    render,
)
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

CURRENCY = re.compile(r"[$£€]")


def make_finding(  # noqa: PLR0913 - a builder; every field is set by some test
    *,
    rule_id: str = "DJP-001",
    family: Family = Family.DJP,
    severity: Severity = Severity.HIGH,
    file: str = "app/views.py",
    line: int = 10,
    fingerprint: str = "0000000000000001",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        title="Query in a loop",
        family=family,
        severity=severity,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        location=Location(file=file, line=line, snippet="for x in qs:"),
        message="A relation is walked per row.",
        rationale="It costs a query per row.",
        remediation="Use select_related.",
        evidence=(Evidence(kind=EvidenceKind.AST, content="for x in qs:", source=file),),
        fingerprint=fingerprint,
    )


class TestBlastRadius:
    def test_a_lone_finding_is_one_place_in_one_file(self) -> None:
        assert blast_radius(make_finding(), []) == (1, 1)

    def test_it_counts_only_the_same_rule(self) -> None:
        target = make_finding(rule_id="DJP-001", fingerprint="a")
        same = make_finding(rule_id="DJP-001", fingerprint="b", file="b.py")
        other = make_finding(rule_id="DJP-007", fingerprint="c", file="c.py")

        assert blast_radius(target, [target, same, other]) == (2, 2)

    def test_several_in_one_file_is_one_file(self) -> None:
        target = make_finding(fingerprint="a", file="a.py")
        others = [make_finding(fingerprint=str(n), file="a.py") for n in range(3)]

        assert blast_radius(target, [target, *others]) == (4, 1)

    def test_the_finding_is_counted_even_when_it_is_not_in_the_run(self) -> None:
        """Explaining a finding from an older report must not report zero places."""
        target = make_finding(fingerprint="not-in-the-run")

        assert blast_radius(target, []) == (1, 1)

    def test_it_does_not_count_the_finding_twice(self) -> None:
        target = make_finding(fingerprint="a")

        assert blast_radius(target, [target]) == (1, 1)


class TestCaveats:
    def test_they_come_from_the_rule_itself(self) -> None:
        rule_id = "DJA-001"
        expected = registry.get(rule_id).meta.limitations

        assert caveats_for(rule_id) == expected
        assert expected, "the rule has no limitations; this test proves nothing"

    def test_every_implemented_rule_has_some(self) -> None:
        """Measured at 67 of 67. If a new rule ships without them, this fails."""
        missing = [r.meta.id for r in registry.all_rules() if not r.meta.limitations]

        assert missing == []

    def test_an_unregistered_rule_yields_nothing_rather_than_a_guess(self) -> None:
        assert caveats_for("DJZ-999") == ()


class TestFraming:
    def test_every_family_is_covered(self) -> None:
        """No finding may fall through to a vague default, so there is none."""
        for family in Family:
            assessment = impact(make_finding(family=family))

            assert assessment.who
            assert assessment.cost

    def test_every_severity_is_covered(self) -> None:
        for severity in Severity:
            assert impact(make_finding(severity=severity)).urgency

    def test_it_says_who_what_how_widespread_and_how_urgent(self) -> None:
        """Headings *and* their contents.

        An earlier version of this asserted only the four headings, so blanking
        the audience sentence passed it -- the section was still there, saying
        nothing. Every heading below is checked against the value under it.
        """
        assessment = impact(make_finding())
        text = render(assessment)

        for heading, value in (
            ("Who this affects", assessment.who),
            ("What it costs", assessment.cost),
            ("How urgent", assessment.urgency),
        ):
            assert heading in text
            assert value in text
            assert f"{heading}\n  {value}" in text

        assert "How widespread" in text

    def test_every_family_says_something_different(self) -> None:
        """A shared sentence would make the section decorative."""
        pairs = {
            (impact(make_finding(family=f)).who, impact(make_finding(family=f)).cost)
            for f in Family
        }

        assert len(pairs) == len(Family)

    def test_every_severity_says_something_different(self) -> None:
        urgencies = {impact(make_finding(severity=s)).urgency for s in Severity}

        assert len(urgencies) == len(Severity)

    def test_a_critical_finding_is_not_framed_as_schedulable(self) -> None:
        text = render(impact(make_finding(severity=Severity.CRITICAL)))

        assert "incident" in text
        assert "Schedule it" not in text

    def test_one_place_is_not_described_as_several(self) -> None:
        assert "one place in this codebase." in render(impact(make_finding()))

    def test_several_places_in_one_file_say_so(self) -> None:
        target = make_finding(fingerprint="a", file="a.py")
        others = [make_finding(fingerprint=str(n), file="a.py") for n in range(2)]

        assert "3 places, all in one file." in render(impact(target, [target, *others]))

    def test_several_files_are_counted_separately(self) -> None:
        target = make_finding(fingerprint="a", file="a.py")
        others = [make_finding(fingerprint=str(n), file=f"{n}.py") for n in range(2)]

        assert "3 places across 3 files." in render(impact(target, [target, *others]))

    def test_the_caveats_are_rendered(self) -> None:
        text = render(impact(make_finding(rule_id="DJA-001", family=Family.DJA)))

        assert "When this does not apply to you" in text
        assert registry.get("DJA-001").meta.limitations[0] in text


class TestItInventsNothing:
    """The tests that make this module safe to put in front of a manager."""

    def test_no_number_appears_that_was_not_counted(self) -> None:
        target = make_finding(fingerprint="a", file="a.py")
        others = [make_finding(fingerprint=str(n), file=f"{n}.py") for n in range(4)]
        assessment = impact(target, [target, *others])

        assert no_invented_numbers(assessment, framing(assessment))

    def test_the_check_would_catch_an_invented_number(self) -> None:
        """Assert the contrast. A check that passes on everything checks nothing."""
        assessment = impact(make_finding())

        assert not no_invented_numbers(assessment, "this affects 40% of requests")

    def test_it_holds_across_every_finding_in_the_corpus(self, drf_project) -> None:
        """The artefact that ships, not a template read by eye."""
        result = engine.run(drf_project, min_confidence=Confidence.TENTATIVE)
        assert result.findings, "no findings; this proves nothing"

        for finding in result.findings:
            assessment = impact(finding, result.findings)

            assert no_invented_numbers(assessment, framing(assessment)), finding.rule_id

    def test_no_money_is_ever_mentioned(self, drf_project) -> None:
        result = engine.run(drf_project, min_confidence=Confidence.TENTATIVE)

        for finding in result.findings:
            text = render(impact(finding, result.findings))

            assert not CURRENCY.search(text)
            assert "cost you" not in text

    def test_no_probability_is_ever_claimed(self, drf_project) -> None:
        """ "Likely to be exploited" is a sentence nobody can check."""
        result = engine.run(drf_project, min_confidence=Confidence.TENTATIVE)

        for finding in result.findings:
            text = render(impact(finding, result.findings)).lower()

            for weasel in ("% of", "likely to be exploited", "probably", "chance of"):
                assert weasel not in text

    def test_the_caveats_are_reproduced_word_for_word(self, drf_project) -> None:
        """Not paraphrased.

        The digit check cannot run over the caveats -- DJA-008's limitation
        cites DJA-010 by name -- so the guarantee for that section is stricter
        instead: it is the rule's text or it is not there. Restating a limit in
        this module's own words is how a limit becomes a reassurance.
        """
        result = engine.run(drf_project, min_confidence=Confidence.TENTATIVE)
        checked = 0

        for finding in result.findings:
            assessment = impact(finding, result.findings)
            text = render(assessment)

            for caveat in registry.get(finding.rule_id).meta.limitations:
                assert caveat in text
                checked += 1

        assert checked > 10, "too few caveats reached to mean anything"

    def test_a_caveat_containing_digits_is_still_rendered(self) -> None:
        """The finding that produced the split, pinned.

        DJA-008's limitation contains "DJA-010" and "two findings". An earlier
        version ran the digit check over the whole rendering and failed on it,
        and the tempting fix -- loosening the regex -- would have stopped the
        check catching anything at all.
        """
        limitations = registry.get("DJA-008").meta.limitations
        assert any(re.search(r"\d", c) for c in limitations), "DJA-008 no longer has the shape"

        text = render(impact(make_finding(rule_id="DJA-008", family=Family.DJA)))

        assert limitations[0] in text

    def test_the_templates_themselves_carry_no_digits(self) -> None:
        """Belt and braces: the only digits may come from the counts."""
        assessment = impact(make_finding())

        assert not re.search(r"\d", assessment.who + assessment.cost + assessment.urgency)


class TestTheShape:
    def test_an_assessment_is_immutable(self) -> None:
        assessment = impact(make_finding())

        with pytest.raises((AttributeError, TypeError)):
            assessment.who = "someone else"  # type: ignore[misc]

    def test_it_is_the_dataclass_it_claims_to_be(self) -> None:
        assert isinstance(impact(make_finding()), Impact)
