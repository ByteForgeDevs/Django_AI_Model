"""Tests for the configuration-reference gate.

It exists for the reason risk 12 keeps restating: a gate passes because what
it checks is absent. This one was verified against real drift before it was
trusted -- run against the reference as first written, it found two keys the
code accepts and the page had left out. The tests below keep that property by
feeding it the defect it exists to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import check_config_doc

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DOC = ROOT / "docs" / "configuration.md"


class TestTheRealRepository:
    def test_the_configuration_reference_is_current(self) -> None:
        assert check_config_doc.check() == 0


class TestTheConfigurationReference:
    @pytest.fixture
    def doc(self) -> object:
        original = CONFIG_DOC.read_text(encoding="utf-8")
        yield CONFIG_DOC
        CONFIG_DOC.write_text(original, encoding="utf-8")

    def edit(self, doc: Path, old: str, new: str) -> None:
        text = doc.read_text(encoding="utf-8")
        assert old in text, f"anchor missing: {old!r}"
        doc.write_text(text.replace(old, new), encoding="utf-8")

    def test_a_missing_key_is_caught(self, doc: Path, capsys: pytest.CaptureFixture[str]) -> None:
        text = doc.read_text(encoding="utf-8")
        doc.write_text(re.sub(r"^\| `exclude_paths` \|.*$\n", "", text, flags=re.M))
        assert check_config_doc.check() == 1
        assert "settings table omits it" in capsys.readouterr().out

    def test_a_key_the_code_rejects_is_caught(
        self, doc: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.edit(doc, "| `exclude_paths` |", "| `exclude_path` |")
        assert check_config_doc.check() == 1
        assert "which the code does not accept" in capsys.readouterr().out

    def test_a_flag_that_does_not_exist_is_caught(
        self, doc: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.edit(doc, "`--exclude-path`", "`--exclude`")
        assert check_config_doc.check() == 1
        assert "does not have" in capsys.readouterr().out

    def test_an_undocumented_subtable_is_caught(
        self, doc: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.edit(doc, "[tool.djaudit.severity]", "[tool.djaudit.sevrity]")
        assert check_config_doc.check() == 1
        assert "accepted and undocumented" in capsys.readouterr().out

    def test_dropping_the_refusal_is_caught(
        self, doc: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every statement of it, not one.

        The page says `live = true` is refused twice -- once in prose and once
        in an example. Removing either leaves the fact stated, so a control
        that removes one catches nothing and makes a working gate look weak.
        """
        text = doc.read_text(encoding="utf-8")
        text = text.replace("`live = true` and `external = true` are refused.", "Both are refused.")
        text = text.replace("live = true          # error: use --live", "# nothing here")
        assert "live = true" not in text and "external = true" not in text
        doc.write_text(text, encoding="utf-8")
        assert check_config_doc.check() == 1
        out = capsys.readouterr().out
        assert "does not show it" in out
        assert "'live'" in out and "'external'" in out

    def test_offering_a_forbidden_key_in_the_table_is_caught(
        self, doc: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The reference must not present `live` as a setting you may use."""
        self.edit(doc, "| `format` |", "| `live` | bool | | `--live` |\n| `format` |")
        assert check_config_doc.check() == 1
        assert "which a config file may not set" in capsys.readouterr().out

    def test_a_missing_page_is_caught(
        self, doc: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(check_config_doc, "DOC", ROOT / "docs" / "not-written-yet.md")
        assert check_config_doc.check() == 1
        assert "does not exist" in capsys.readouterr().out
