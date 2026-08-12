import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from djaudit import fingerprint
from djaudit.models import Confidence, Family, Finding, Location, Severity, Tier

if TYPE_CHECKING:
    from djaudit.context import ProjectContext

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def vulnerable_project() -> Path:
    return FIXTURES / "vulnerable_project"


@pytest.fixture
def overridden_project() -> Path:
    return FIXTURES / "overridden_project"


@pytest.fixture
def api_project() -> Path:
    return FIXTURES / "api_project"


@pytest.fixture
def env_settings_project() -> Path:
    return FIXTURES / "env_settings_project"


@pytest.fixture
def configurations_project() -> Path:
    return FIXTURES / "configurations_project"


@pytest.fixture
def near_miss_project() -> Path:
    return FIXTURES / "near_miss_project"


@pytest.fixture
def drf_project() -> Path:
    return FIXTURES / "drf_project"


@pytest.fixture
def orm_project() -> Path:
    return FIXTURES / "orm_project"


@pytest.fixture
def injection_project() -> Path:
    return FIXTURES / "injection_project"


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


@pytest.fixture
def make_project(tmp_path: Path):
    """Write a throwaway project from a ``{path: source}`` mapping.

    Model-graph tests are about one construct at a time -- an abstract base, a
    swapped user model, an app label overridden in ``apps.py`` -- and a shared
    on-disk fixture would either grow a case per test or force each test to
    read around the others. Building the two files the test is about keeps the
    thing under test visible in the test.
    """
    from djaudit.discovery import build_context

    def build(files: dict[str, str]) -> "ProjectContext":
        for name, source in files.items():
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        return build_context(tmp_path)

    return build
