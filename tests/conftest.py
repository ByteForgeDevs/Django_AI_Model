from pathlib import Path

import pytest

from djaudit import fingerprint
from djaudit.models import Confidence, Family, Finding, Location, Severity, Tier

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def vulnerable_project() -> Path:
    return FIXTURES / "vulnerable_project"


@pytest.fixture
def overridden_project() -> Path:
    return FIXTURES / "overridden_project"


def make_finding(
    rule_id: str = "DJS-001",
    file: str = "app/views.py",
    line: int = 10,
    snippet: str = "x = 1",
) -> Finding:
    """One finding, fingerprinted exactly as the engine does.

    Tests that compare findings by identity need real fingerprints; a Finding
    built directly carries an empty one, and several would collide.
    """
    return fingerprint.assign(
        [
            Finding(
                rule_id=rule_id,
                title=f"title for {rule_id}",
                severity=Severity.HIGH,
                confidence=Confidence.FIRM,
                family=Family[rule_id.split("-", maxsplit=1)[0]],
                tier=Tier.STATIC,
                location=Location(file=file, line=line, snippet=snippet),
                message="message",
                rationale="rationale",
                remediation="remediation",
            )
        ]
    )[0]


def make_findings(rule_id: str, count: int) -> list[Finding]:
    """Distinct findings of one rule.

    Each lives in its own file: fingerprints deliberately ignore line numbers,
    so varying only the line would produce one shared identity.
    """
    return [make_finding(rule_id, file=f"app/mod{n}.py") for n in range(count)]
