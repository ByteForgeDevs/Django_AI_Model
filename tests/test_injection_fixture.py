"""The injection_project fixture: where the DJI family's recall is measured.

Before this fixture existed, all twelve injection rules had their recall
measured by their own unit tests and by mutation, and by nothing else. A rule
whose only positive evidence is its own test file has been checked against the
author's idea of the defect rather than against the defect.

The benchmarks cannot help. The whole family reports **nothing** on all three
corpora — correctly, because mature projects parameterise their SQL and check
their redirect targets — so for `DJI-009` through `DJI-012` this project is the
only positive evidence that exists anywhere in the repository.

The manifest asserts the twelve defects and the silence of `shop/controls.py`.
What this module adds is the reasoning: which near-miss guards which rule, and
the one case where writing the fixture proved a rule wrong.
"""

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity


@pytest.fixture
def result(injection_project):
    found = engine.run(
        injection_project,
        min_severity=Severity.INFO,
        min_confidence=Confidence.TENTATIVE,
    )
    # Half this fixture is control cases, and a crashed rule satisfies every
    # one of them by doing nothing.
    assert not found.rule_errors, found.rule_errors
    return found


def ids(result, rule_id):
    return sorted(f.location.line for f in result.findings if f.rule_id == rule_id)


def test_every_injection_rule_fires_here(result):
    """The headline, and the only place in the repo where it can be checked."""
    fired = {f.rule_id for f in result.findings}
    expected = {f"DJI-{n:03d}" for n in range(1, 13)}
    assert expected <= fired, expected - fired


def test_each_rule_fires_exactly_once(result):
    """One defect per rule, so a finding names which rule found it.

    It also catches a rule that has started reporting its neighbour's defect,
    which a total count alone would hide.
    """
    counts = {f"DJI-{n:03d}": len(ids(result, f"DJI-{n:03d}")) for n in range(1, 13)}
    assert counts == dict.fromkeys(counts, 1), counts


def test_nothing_in_the_controls_file_is_ever_reported(result):
    """The near-misses, and the half of the fixture that decides the family.

    Every control reaches the same sink with the same tainted value. What
    differs is that it arrives as a parameter, through an allowlist, or after a
    sanitiser — so a rule that reports one of these has only detected that
    request data and a dangerous call share a function.
    """
    reported = sorted(
        (f.rule_id, f.location.line)
        for f in result.findings
        if f.location.file.endswith("controls.py")
    )
    assert reported == []


def test_the_four_rules_the_benchmarks_cannot_reach(result):
    """DJI-009 to DJI-012 are silent on every corpus, by construction.

    Mature projects do not leave SSRF, open redirects, unescaped request data
    or path traversal lying around, so their benchmark zeros say nothing about
    whether the rules work. These four lines are the entire positive evidence.
    """
    for rule_id in ("DJI-009", "DJI-010", "DJI-011", "DJI-012"):
        assert len(ids(result, rule_id)) == 1, rule_id


def test_both_allowlist_shapes_are_accepted(result):
    """DJI-006, and the reason this fixture was worth building.

    The rule shipped knowing only the two allowlist shapes the benchmark
    corpora happen to write, and reported the mapping lookup — the idiom the
    documentation recommends — as a defect. Since it reports nothing on any
    corpus, its precision had never been measured on real code, so "the corpus
    does not write it" had been standing in for "nobody writes it". The lookup
    is a strictly stronger guarantee than the membership test already accepted,
    because it cannot produce an unlisted column at all. Both shapes are kept
    so neither can regress.
    """
    assert len(ids(result, "DJI-006")) == 1


def test_the_settings_and_api_families_stay_out_of_this(result):
    """A fixture that trips several families cannot show which found what."""
    other = sorted({f.rule_id for f in result.findings if not f.rule_id.startswith("DJI")})
    assert other == []


def test_a_default_run_still_surfaces_all_twelve(injection_project):
    """Every one of these is `firm` or better, so the defaults show them all.

    That matters more here than elsewhere: an injection finding suppressed by
    the default confidence floor is a vulnerability the user never sees.
    """
    found = engine.run(injection_project, min_severity=Severity.LOW, min_confidence=Confidence.FIRM)
    fired = {f.rule_id for f in found.findings}
    assert fired == {f"DJI-{n:03d}" for n in range(1, 13)}
