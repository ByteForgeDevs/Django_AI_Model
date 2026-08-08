"""The live-tier note's gate, checked against defects it must not miss.

`scripts/check_live_doc.py` exists so the security model cannot drift from the
runner that enforces it. That makes the gate itself load-bearing: if its
readers silently return `None` for a shape the code actually uses, it reports
success having checked nothing -- which is precisely how the first draft
behaved on `frozenset({...})` and on `1 << 20`.

So each check here is given the defect it was written to catch.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_live_doc  # noqa: E402


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the real tree, so a defect can be injected without editing it.

    Every path the note cites is created, discovered by reading the note rather
    than listed here -- a hardcoded list would make this fixture fail the day
    somebody cited one more file, and the failure would look like the gate.
    """
    root = tmp_path / "repo"
    note = (ROOT / "docs/architecture/live-tier.md").read_text()
    sources = {
        "docs/architecture/live-tier.md",
        "src/djaudit/live/runner.py",
        "src/djaudit/live/locks.py",
    }
    for relative in sorted(
        sources | set(re.findall(r"`((?:src|tests|scripts|docs)/[\w./-]+)`", note))
    ):
        target = root / relative
        original = ROOT / relative
        # The note cites `tests/live` as well as files inside it. Writing a
        # directory out as an empty file makes every later mkdir fail, and the
        # failure then surfaces as an error in the gate rather than here.
        if original.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original.read_text() if original.is_file() else "")

    for module in (ROOT / "src/djaudit/rules").glob("*.py"):
        destination = root / "src/djaudit/rules" / module.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(module.read_text())

    monkeypatch.setattr(check_live_doc, "ROOT", root)
    monkeypatch.setattr(check_live_doc, "DOC", root / "docs/architecture/live-tier.md")
    monkeypatch.setattr(check_live_doc, "SRC", root / "src/djaudit")
    monkeypatch.setattr(check_live_doc, "LIVE", root / "src/djaudit/live")
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{path.name} no longer contains {old!r}; this test is checking nothing"
    path.write_text(text.replace(old, new, 1))


class TestItPassesOnTheRealTree:
    def test_the_note_and_the_code_agree_right_now(self) -> None:
        assert check_live_doc.main() == 0


class TestTheConstantReaderHandlesTheRealShapes:
    """Every one of these returned `None` from the first draft, and `None` made
    the gate skip the check and still report success."""

    def test_a_frozenset_call_is_read(self) -> None:
        runner = ROOT / "src/djaudit/live/runner.py"
        value = check_live_doc.constant(runner, "PASSTHROUGH")
        assert isinstance(value, frozenset)
        assert "PATH" in value

    def test_a_shift_is_read(self) -> None:
        runner = ROOT / "src/djaudit/live/runner.py"
        assert check_live_doc.constant(runner, "OUTPUT_LIMIT") == 1 << 20

    def test_a_missing_constant_is_an_error_not_a_none(self) -> None:
        """Returning `None` would be indistinguishable from an unreadable one."""
        with pytest.raises(KeyError):
            check_live_doc.constant(ROOT / "src/djaudit/live/runner.py", "NO_SUCH_THING")


class TestTheDefectsItMustCatch:
    def test_a_pg_variable_added_to_the_passthrough(self, sandbox: Path) -> None:
        """The one that would silently point sqlmigrate at another database."""
        edit(
            sandbox / "src/djaudit/live/runner.py", '        "TZ",\n', '        "TZ",\n"PGHOST",\n'
        )
        assert check_live_doc.main() == 1

    def test_a_variable_dropped_from_the_passthrough(self, sandbox: Path) -> None:
        edit(sandbox / "src/djaudit/live/runner.py", '        "TZ",\n', "")
        assert check_live_doc.main() == 1

    def test_a_refusal_quietly_lifted(self, sandbox: Path) -> None:
        edit(sandbox / "src/djaudit/live/runner.py", '"PYTHONSTARTUP", ', "")
        assert check_live_doc.main() == 1

    def test_the_timeout_changing(self, sandbox: Path) -> None:
        runner = sandbox / "src/djaudit/live/runner.py"
        edit(runner, "DEFAULT_TIMEOUT = 30.0", "DEFAULT_TIMEOUT = 90.0")
        assert check_live_doc.main() == 1

    def test_the_output_ceiling_changing(self, sandbox: Path) -> None:
        runner = sandbox / "src/djaudit/live/runner.py"
        edit(runner, "OUTPUT_LIMIT = 1 << 20", "OUTPUT_LIMIT = 1 << 24")
        assert check_live_doc.main() == 1

    def test_a_cited_path_that_does_not_exist(self, sandbox: Path) -> None:
        edit(
            sandbox / "docs/architecture/live-tier.md",
            "`scripts/live_gate.py`",
            "`scripts/live_gateway.py`",
        )
        assert check_live_doc.main() == 1

    def test_the_note_disappearing(self, sandbox: Path) -> None:
        (sandbox / "docs/architecture/live-tier.md").unlink()
        assert check_live_doc.main() == 1

    def test_the_test_variable_no_longer_named(self, sandbox: Path) -> None:
        edit(sandbox / "docs/architecture/live-tier.md", "DJAUDIT_TEST_POSTGRES", "PGDSN")
        assert check_live_doc.main() == 1


class TestItDoesNotFailOnHarmlessChanges:
    """A gate that fails on reflowed prose gets disabled, and then it is not a
    gate. These are the changes it must tolerate."""

    def test_reflowing_a_paragraph(self, sandbox: Path) -> None:
        doc = sandbox / "docs/architecture/live-tier.md"
        doc.write_text(
            doc.read_text().replace("**2 declare\n`Tier.LIVE`**", "**2\ndeclare\n`Tier.LIVE`**")
        )
        assert check_live_doc.main() == 0

    def test_adding_a_sentence(self, sandbox: Path) -> None:
        doc = sandbox / "docs/architecture/live-tier.md"
        doc.write_text(doc.read_text() + "\nA further note that claims nothing checkable.\n")
        assert check_live_doc.main() == 0
