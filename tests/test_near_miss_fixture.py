"""The near_miss_project fixture: nothing here is a defect.

The recall fixtures answer "does the rule fire?". This one answers the question
that decides whether a security tool survives contact with a real team: on a
project that already did the work, but did not write it the way the textbook
does, does the tool shut up?

Every setting in that fixture is correct and every one of them is one sloppy
check away from a finding -- a leading dot read as a wildcard, MD5 found in a
list without noticing it is last, ATOMIC_REQUESTS seen without noticing it is
inside the alias. The headline assertion is the first one: a default run
reports nothing and exits zero.
"""

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity

# What the CLI actually uses. An `info`/`tentative` finding exists in the
# result and is invisible here by design, which is the distinction this module
# is built around.
CLI_SEVERITY = Severity.LOW
CLI_CONFIDENCE = Confidence.FIRM


@pytest.fixture
def result(near_miss_project):
    found = engine.run(
        near_miss_project,
        min_severity=Severity.INFO,
        min_confidence=Confidence.TENTATIVE,
    )
    assert not found.rule_errors, found.rule_errors
    return found


def test_a_default_run_is_silent(near_miss_project):
    """The headline. Nothing correct-but-unusual reaches a default run."""
    found = engine.run(near_miss_project, min_severity=CLI_SEVERITY, min_confidence=CLI_CONFIDENCE)
    assert found.findings == []


def test_only_the_two_informational_branches_speak_at_all(result):
    """Both describe a risk whose size depends on something outside the source.

    DJS-012 cannot know who terminates TLS; DJS-014 cannot know who is allowed
    to create a subdomain. Neither is wrong to mention it, and neither has any
    business interrupting a default run to do so.
    """
    assert sorted(f.rule_id for f in result.findings) == ["DJS-012", "DJS-014"]
    assert all(f.confidence is Confidence.TENTATIVE for f in result.findings)


@pytest.mark.parametrize(
    ("rule_id", "why"),
    [
        ("DJS-001", "DEBUG is a comparison against the environment, not a literal True"),
        ("DJS-002", "SECRET_KEY holds a path that is read at startup, not a key"),
        ("DJS-004", "an empty PASSWORD is client-certificate auth, not a leak"),
        ("DJS-013", "a leading dot is Django's subdomain syntax, not the wildcard"),
        ("DJS-016", "credentials are allowed against an explicit origin list"),
        ("DJS-017", "SAMEORIGIN is Django's default, not protection switched off"),
        ("DJS-019", "MD5 is last, which is legacy verification on an Argon2 project"),
        ("DJS-023", "ATOMIC_REQUESTS is inside the alias, where Django reads it"),
        ("DJS-024", "the toolbar is installed only under `if DEBUG`"),
        ("DJS-025", "the dev tooling is in requirements-dev.txt"),
        ("DJS-027", "the reporter filter extends Django's rather than replacing it"),
    ],
)
def test_the_shape_that_looks_like_the_defect_is_not_reported(result, rule_id, why):
    reported = [f for f in result.findings if f.rule_id == rule_id]
    assert reported == [], f"{rule_id} fired, but {why}"
