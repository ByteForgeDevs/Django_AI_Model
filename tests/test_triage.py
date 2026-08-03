"""Tests for the triage file format."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from djaudit.fingerprint import FINGERPRINT_VERSION
from djaudit.triage import (
    DEFAULT_MAX_FALSE_POSITIVE_RATE,
    TRIAGE_VERSION,
    Triage,
    TriageEntry,
    TriageError,
    Verdict,
)


class TestVerdict:
    def test_accepted_risk_counts_as_a_correct_detection(self) -> None:
        # The target choosing not to fix something says nothing about whether
        # we were right to report it.
        assert Verdict.ACCEPTED_RISK.is_correct_detection is True

    def test_true_positive_counts_as_a_correct_detection(self) -> None:
        assert Verdict.TRUE_POSITIVE.is_correct_detection is True

    def test_false_positive_does_not(self) -> None:
        assert Verdict.FALSE_POSITIVE.is_correct_detection is False


class TestTriageRoundTrip:
    def test_save_then_load_preserves_every_field(self, tmp_path: Path) -> None:
        original = Triage(
            target="healthchecks",
            repo="https://github.com/healthchecks/healthchecks",
            sha="5086d282ea33831e8c86eb9d0802dc2602c66ef7",
            entries=(
                TriageEntry(
                    fingerprint="abc123",
                    rule_id="DJS-001",
                    verdict=Verdict.FALSE_POSITIVE,
                    file="hc/settings.py",
                    line=42,
                    note="DEBUG is env-driven and defaults off in prod.",
                    reviewed="2024-01-01",
                    reviewer="Zambagarrah",
                ),
            ),
            max_false_positive_rate=0.05,
        )
        path = tmp_path / "healthchecks.json"
        original.save(path)

        loaded = Triage.load(path)
        assert loaded == original

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "t.json"
        Triage(target="t").save(path)
        assert path.is_file()

    def test_entries_are_written_in_source_order(self, tmp_path: Path) -> None:
        triage = Triage(
            target="t",
            entries=(
                TriageEntry("f3", "DJS-002", Verdict.TRUE_POSITIVE, file="b.py", line=1),
                TriageEntry("f1", "DJS-001", Verdict.TRUE_POSITIVE, file="a.py", line=90),
                TriageEntry("f2", "DJS-001", Verdict.TRUE_POSITIVE, file="a.py", line=9),
            ),
        )
        path = tmp_path / "t.json"
        triage.save(path)

        written = json.loads(path.read_text())
        assert [e["fingerprint"] for e in written["findings"]] == ["f2", "f1", "f3"]

    def test_file_is_newline_terminated(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        Triage(target="t").save(path)
        assert path.read_text().endswith("\n")


class TestTriageLoadRejections:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(TriageError, match="not found"):
            Triage.load(tmp_path / "absent.json")

    def test_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text("{not json")
        with pytest.raises(TriageError, match="not valid JSON"):
            Triage.load(path)

    def test_non_object_payload(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text("[]")
        with pytest.raises(TriageError, match="must be a JSON object"):
            Triage.load(path)

    def test_future_triage_version(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    "triage_version": TRIAGE_VERSION + 1,
                    "fingerprint_version": FINGERPRINT_VERSION,
                    "target": "t",
                    "findings": [],
                }
            )
        )
        with pytest.raises(TriageError, match="not supported"):
            Triage.load(path)

    def test_stale_fingerprint_scheme_is_refused(self, tmp_path: Path) -> None:
        # Fingerprints are the only thing tying a verdict to a finding. If the
        # scheme changed, every verdict silently stops matching and the whole
        # file would read as "everything regressed, everything untriaged".
        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    "triage_version": TRIAGE_VERSION,
                    "fingerprint_version": "djaudit/v0",
                    "target": "t",
                    "findings": [],
                }
            )
        )
        with pytest.raises(TriageError, match="re-reviewed"):
            Triage.load(path)

    def test_unknown_verdict(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    "triage_version": TRIAGE_VERSION,
                    "fingerprint_version": FINGERPRINT_VERSION,
                    "target": "t",
                    "findings": [{"fingerprint": "a", "verdict": "probably_fine"}],
                }
            )
        )
        with pytest.raises(TriageError, match="unknown verdict"):
            Triage.load(path)

    def test_entry_without_a_fingerprint(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    "triage_version": TRIAGE_VERSION,
                    "fingerprint_version": FINGERPRINT_VERSION,
                    "target": "t",
                    "findings": [{"verdict": "true_positive"}],
                }
            )
        )
        with pytest.raises(TriageError, match="missing"):
            Triage.load(path)


class TestWithEntries:
    def test_existing_verdicts_are_never_overwritten(self) -> None:
        reviewed = TriageEntry("f1", "DJS-001", Verdict.FALSE_POSITIVE, note="checked by hand")
        triage = Triage(target="t", entries=(reviewed,))

        merged = triage.with_entries(
            [
                TriageEntry("f1", "DJS-001", Verdict.TRUE_POSITIVE),
                TriageEntry("f2", "DJS-002", Verdict.TRUE_POSITIVE),
            ]
        )

        assert merged.by_fingerprint["f1"] == reviewed
        assert len(merged) == 2

    def test_defaults_are_sensible(self) -> None:
        assert Triage(target="t").max_false_positive_rate == DEFAULT_MAX_FALSE_POSITIVE_RATE
