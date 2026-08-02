"""Baselines are the adoption story; a broken one silences real findings."""

import json

import pytest

from djaudit import fingerprint as fp
from djaudit.baseline import BASELINE_VERSION, Baseline, BaselineError
from djaudit.fingerprint import FINGERPRINT_VERSION
from djaudit.models import Location
from tests.test_models import make_finding


@pytest.fixture
def findings():
    return fp.assign(
        [
            make_finding(location=Location(file="a.py", line=1, snippet="one")),
            make_finding(location=Location(file="b.py", line=2, snippet="two")),
        ]
    )


class TestRoundTrip:
    def test_save_then_load_preserves_fingerprints(self, tmp_path, findings):
        path = tmp_path / "baseline.json"
        Baseline.from_findings(findings).save(path)
        assert Baseline.load(path).fingerprints == {f.fingerprint for f in findings}

    def test_entries_are_human_readable_for_review(self, tmp_path, findings):
        path = tmp_path / "baseline.json"
        Baseline.from_findings(findings).save(path)
        entry = json.loads(path.read_text())["findings"][0]
        assert {"fingerprint", "rule_id", "file", "line", "message"} <= entry.keys()

    def test_creates_missing_parent_directories(self, tmp_path, findings):
        path = tmp_path / "nested" / "deeper" / "baseline.json"
        Baseline.from_findings(findings).save(path)
        assert path.is_file()


class TestFiltering:
    def test_known_findings_are_dropped(self, findings):
        baseline = Baseline.from_findings(findings)
        assert baseline.filter(findings) == []

    def test_new_findings_survive(self, findings):
        baseline = Baseline.from_findings(findings[:1])
        assert baseline.filter(findings) == findings[1:]

    def test_matching_ignores_line_numbers(self, tmp_path):
        original = fp.assign([make_finding(location=Location("a.py", 10, snippet="DEBUG = True"))])
        moved = fp.assign([make_finding(location=Location("a.py", 84, snippet="DEBUG = True"))])
        assert Baseline.from_findings(original).filter(moved) == []


class TestCompatibility:
    def test_missing_file_is_reported_clearly(self, tmp_path):
        with pytest.raises(BaselineError, match="not found"):
            Baseline.load(tmp_path / "absent.json")

    def test_invalid_json_is_reported_clearly(self, tmp_path):
        path = tmp_path / "baseline.json"
        path.write_text("{not json")
        with pytest.raises(BaselineError, match="not valid JSON"):
            Baseline.load(path)

    def test_unknown_baseline_version_is_rejected(self, tmp_path):
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps({"baseline_version": 99, "findings": []}))
        with pytest.raises(BaselineError, match="not supported"):
            Baseline.load(path)

    def test_stale_fingerprint_scheme_is_rejected(self, tmp_path):
        """A changed hashing scheme must fail loudly rather than silently unsuppress."""
        path = tmp_path / "baseline.json"
        path.write_text(
            json.dumps(
                {
                    "baseline_version": BASELINE_VERSION,
                    "fingerprint_version": "djaudit/v0",
                    "findings": [],
                }
            )
        )
        with pytest.raises(BaselineError, match="fingerprint scheme"):
            Baseline.load(path)

    def test_current_scheme_is_accepted(self, tmp_path):
        path = tmp_path / "baseline.json"
        path.write_text(
            json.dumps(
                {
                    "baseline_version": BASELINE_VERSION,
                    "fingerprint_version": FINGERPRINT_VERSION,
                    "findings": [],
                }
            )
        )
        assert len(Baseline.load(path)) == 0
