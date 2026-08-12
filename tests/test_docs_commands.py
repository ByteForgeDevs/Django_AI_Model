"""Tests for the documented-commands and links gate.

It exists for the reason risk 12 keeps restating: a gate passes because what
it checks is absent. This one was verified against real drift before it was
trusted -- run against the repository as it stood, it found an invocation
click did not recognise. The tests below keep that property by feeding it the
defect it exists to catch.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_docs_commands  # noqa: E402

CONFIG_DOC = ROOT / "docs" / "configuration.md"


class TestTheRealRepository:
    def test_every_documented_command_exists(self) -> None:
        assert check_docs_commands.check() == 0

    def test_it_is_actually_reading_something(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A gate that found no invocations would also report success."""
        check_docs_commands.check()
        out = capsys.readouterr().out
        counts = [int(n) for n in re.findall(r"(\d+) (?:invocations|links)", out)]
        assert counts and all(n > 0 for n in counts), out


class TestCommandsThatDoNotExist:
    @pytest.fixture
    def readme(self) -> object:
        path = ROOT / "README.md"
        original = path.read_text(encoding="utf-8")
        yield path
        path.write_text(original, encoding="utf-8")

    def append(self, path: Path, command: str) -> None:
        path.write_text(
            path.read_text(encoding="utf-8") + f"\n```\n{command}\n```\n", encoding="utf-8"
        )

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("djaudit audit .", "unknown subcommand"),
            ("djaudit run . --min-sevrity high", "has no option"),
            ("djaudit run . --min-severity", "requires a value"),
            ("djaudit run . --live=yes", "takes no value"),
        ],
    )
    def test_it_catches(
        self,
        readme: Path,
        command: str,
        expected: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self.append(readme, command)
        assert check_docs_commands.check() == 1
        assert expected in capsys.readouterr().out

    def test_prose_is_not_checked(self, readme: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Only fenced blocks. A sentence may name a flag it is not running."""
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\nRun djaudit run . --not-a-flag one day.\n",
            encoding="utf-8",
        )
        assert check_docs_commands.check() == 0, capsys.readouterr().out


class TestLinksThatDoNotResolve:
    @pytest.fixture
    def doc(self) -> object:
        original = CONFIG_DOC.read_text(encoding="utf-8")
        yield CONFIG_DOC
        CONFIG_DOC.write_text(original, encoding="utf-8")

    def test_a_dangling_link_is_caught(self, doc: Path, capsys: pytest.CaptureFixture[str]) -> None:
        doc.write_text(
            doc.read_text(encoding="utf-8") + "\nSee [nowhere](nowhere.md).\n", encoding="utf-8"
        )
        assert check_docs_commands.check() == 1
        assert "does not resolve" in capsys.readouterr().out

    def test_an_external_url_is_left_alone(
        self, doc: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc.write_text(
            doc.read_text(encoding="utf-8") + "\nSee [docs](https://example.invalid/x).\n",
            encoding="utf-8",
        )
        assert check_docs_commands.check() == 0, capsys.readouterr().out
