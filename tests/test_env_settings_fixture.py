"""The env_settings_project fixture, and the confidence gap it guards.

Every other fixture writes its settings as literals, which is the shape Django
had in 2013 and the shape almost nothing has now. This one reads everything
through django-environ, and the interesting property is not that defects are
still found -- it is that they are found *less certainly*, because the operator
may have set the variable and the source cannot say.

The controls matter more than the findings. `env.db()` parses a URL that is not
in the repository, and a credential held correctly has no default at all; a
tool that guesses at either is a tool that fires hardest on the projects that
configured themselves properly.
"""

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity

LITERAL_GRADES = {
    "DJS-001": Confidence.CERTAIN,
    "DJS-013": Confidence.FIRM,
}


@pytest.fixture
def found(env_settings_project):
    result = engine.run(
        env_settings_project,
        min_severity=Severity.INFO,
        min_confidence=Confidence.TENTATIVE,
    )
    assert not result.rule_errors, result.rule_errors
    return {f.rule_id: f for f in result.findings}


def test_the_fixture_reports_exactly_its_three_planted_defects(found):
    assert sorted(found) == ["DJS-001", "DJS-003", "DJS-013"]


@pytest.mark.parametrize("rule_id", sorted(LITERAL_GRADES))
def test_an_environment_default_is_less_certain_than_a_literal(found, rule_id):
    """The point of the fixture. Same defect, weaker claim."""
    assert found[rule_id].confidence is Confidence.TENTATIVE
    assert found[rule_id].confidence is not LITERAL_GRADES[rule_id]


def test_severity_does_not_move_with_confidence(found):
    """A defect that only might be live is still the same defect if it is."""
    assert found["DJS-001"].severity is Severity.CRITICAL
    assert found["DJS-013"].severity is Severity.HIGH


def test_the_startproject_key_stays_firm(found):
    """Not tentative: the fallback is a specific published value, not a guess.

    ``django-insecure-`` is what ``startproject`` writes and what every Django
    tutorial contains, so a project falling back to it is falling back to a key
    an attacker already has. The environment can only make that better, never
    worse, which is why the indirection does not weaken the claim the way it
    does for DEBUG.
    """
    assert found["DJS-003"].confidence is Confidence.FIRM


def test_a_url_parsed_from_the_environment_is_not_guessed_at(found):
    """``env.db()`` builds the connection dict out of DATABASE_URL.

    The password, the host and the sslmode are all inside a string the source
    never contains, so there is nothing to read and nothing to report.
    """
    assert "DJS-004" not in found
    assert "DJS-022" not in found


def test_a_credential_with_no_default_is_not_a_leaked_credential(found):
    """``env("EMAIL_HOST_PASSWORD")`` is the correct way to hold a secret.

    django-environ raises ImproperlyConfigured when it is unset. A rule that
    matched the setting's name rather than its value would fire here, which
    would mean firing on every project that did this right.
    """
    assert "DJS-005" not in found


def test_the_env_schema_is_read_rather_than_the_call_site(found):
    """``SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT")`` has no inline default.

    The only place the value exists is the schema passed to ``Env()`` several
    lines earlier. Without following that, this reads as unset and DJS-006
    fires -- on a project that has the setting switched on.
    """
    assert "DJS-006" not in found
    assert "DJS-009" not in found
