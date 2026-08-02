from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def vulnerable_project() -> Path:
    return FIXTURES / "vulnerable_project"


@pytest.fixture
def overridden_project() -> Path:
    return FIXTURES / "overridden_project"
