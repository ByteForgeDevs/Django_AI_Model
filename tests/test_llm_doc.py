"""The LLM architecture note is checked, not just written.

`scripts/check_llm_doc.py` is the gate. These tests are the gate's own proof:
each shows it failing on one class of drift, because a checker that passes on
everything and a document that is correct look identical from the outside.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "architecture" / "llm-layer.md"
SCRIPT = ROOT / "scripts" / "check_llm_doc.py"


def check() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_the_note_exists_because_the_package_cites_it() -> None:
    """`djaudit/llm/__init__.py` sends readers here in its first paragraph."""
    init = (ROOT / "src" / "djaudit" / "llm" / "__init__.py").read_text()
    assert "docs/architecture/llm-layer.md" in init
    assert DOC.exists()


def test_the_note_is_currently_accurate() -> None:
    result = check()
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("original", "replacement", "expected"),
    [
        pytest.param(
            "tests/llm/test_hostile_provider.py",
            "tests/llm/test_renamed_away.py",
            "does not exist",
            id="a cited file that was renamed",
        ),
        pytest.param(
            "`MINIMUM_OBSERVATIONS = 5`",
            "`MINIMUM_OBSERVATIONS = 9`",
            "code says 5",
            id="a constant that drifted",
        ),
        pytest.param(
            "settles findings from 6 rules",
            "settles findings from 12 rules",
            "code has 6",
            id="the corpus prior grew",
        ),
        pytest.param(
            "`DJS-011`, `DJS-018`",
            "`DJS-011`",
            "does not mention DJS-018",
            id="a fixable rule went undocumented",
        ),
        pytest.param(
            "`DJS-007`",
            "`DJS-999`",
            "FIXERS does not list it",
            id="a rule claimed fixable that is not",
        ),
        pytest.param(
            "`Level.TESTED`",
            "`Level.PROVEN`",
            "no longer mentions Level.TESTED",
            id="a verification level was renamed",
        ),
        pytest.param(
            "99.790",
            "99.999",
            "no longer reports the measured figure 99.790",
            id="a measured figure was rounded up",
        ),
    ],
)
def test_the_gate_fails_on_drift(
    original: str, replacement: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each defect is introduced for real, then undone.

    The file is restored in a `finally` rather than by a fixture so a failure
    here cannot leave the working tree modified.
    """
    kept = DOC.read_text()
    assert original in kept, f"the note no longer contains {original!r}"
    try:
        DOC.write_text(kept.replace(original, replacement))
        result = check()
        assert result.returncode == 1, "the gate passed on a broken note"
        assert expected in result.stdout, result.stdout
    finally:
        DOC.write_text(kept)
    assert DOC.read_text() == kept


def test_a_missing_note_is_itself_a_failure(tmp_path: Path) -> None:
    """The worst case is the file simply going away."""
    kept = DOC.read_text()
    try:
        DOC.unlink()
        result = check()
        assert result.returncode == 1
        assert "does not exist" in result.stdout
    finally:
        DOC.write_text(kept)
