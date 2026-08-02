"""DJS-007 -- HSTS not enforced for at least a year.

The first numeric rule in the family, and the first where the interesting
question is not "on or off" but "long enough". It is also the rule with the
most dangerous remediation we ship, so the tests cover the wording as well as
the detection.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity
from djaudit.rules._base import could_be_under
from djaudit.rules.transport import ONE_YEAR, _duration
from djaudit.values import Value

MARKERS = "INSTALLED_APPS = []\nDEBUG = False\nDATABASES = {}\nSECRET_KEY = 'x'\n"


def build(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + body)
    return root


def audit(root: Path):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == "DJS-007"]


class TestCouldBeUnder:
    def test_a_value_at_the_threshold_is_enough(self):
        assert not could_be_under(Value.of(ONE_YEAR), ONE_YEAR)

    def test_a_value_above_the_threshold_is_enough(self):
        assert not could_be_under(Value.of(ONE_YEAR * 2), ONE_YEAR)

    def test_a_value_below_the_threshold_is_not(self):
        assert could_be_under(Value.of(ONE_YEAR - 1), ONE_YEAR)

    def test_zero_is_not(self):
        assert could_be_under(Value.of(0), ONE_YEAR)

    @pytest.mark.parametrize("literal", [None, "31536000", True, False])
    def test_a_value_that_is_not_a_number_cannot_show_the_duration_is_long_enough(self, literal):
        """True is the trap: it is an int in Python, and it is not a duration."""
        assert could_be_under(Value.of(literal), ONE_YEAR)

    def test_the_shorter_branch_decides(self):
        both = Value.conditional([Value.of(ONE_YEAR), Value.of(60)])
        assert could_be_under(both, ONE_YEAR)

    def test_every_branch_long_enough_is_enough(self):
        both = Value.conditional([Value.of(ONE_YEAR), Value.of(ONE_YEAR * 3)])
        assert not could_be_under(both, ONE_YEAR)


class TestDetection:
    def test_never_set_is_reported(self, tmp_path):
        (finding,) = audit(build(tmp_path, ""))
        assert "is never set" in finding.message

    def test_zero_is_reported_as_zero_not_as_absent(self, tmp_path):
        (finding,) = audit(build(tmp_path, "SECURE_HSTS_SECONDS = 0\n"))
        assert "is 0" in finding.message

    def test_a_full_year_is_not_reported(self, tmp_path):
        assert not audit(build(tmp_path, f"SECURE_HSTS_SECONDS = {ONE_YEAR}\n"))

    def test_longer_than_a_year_is_not_reported(self, tmp_path):
        assert not audit(build(tmp_path, "SECURE_HSTS_SECONDS = 63072000\n"))

    def test_a_short_window_is_reported_as_an_unfinished_ramp(self, tmp_path):
        """Django's own advice is to ramp up, so a small value is a story."""
        (finding,) = audit(build(tmp_path, "SECURE_HSTS_SECONDS = 3600\n"))
        assert "short of the one year" in finding.message
        assert "ramp-up that was started and never finished" in finding.message

    def test_one_second_short_of_a_year_is_still_reported(self, tmp_path):
        assert audit(build(tmp_path, f"SECURE_HSTS_SECONDS = {ONE_YEAR - 1}\n"))


class TestGrading:
    def test_it_never_claims_certainty_because_a_cdn_may_send_the_header(self, tmp_path):
        (finding,) = audit(build(tmp_path, "SECURE_HSTS_SECONDS = 0\n"))
        assert finding.confidence is Confidence.FIRM

    def test_severity_stays_low(self, tmp_path):
        """Deliberately quieter than the cookie flags: the proxy usually owns this."""
        (finding,) = audit(build(tmp_path, "SECURE_HSTS_SECONDS = 0\n"))
        assert finding.severity is Severity.LOW

    def test_the_remediation_warns_that_the_header_cannot_be_recalled(self, tmp_path):
        """The one piece of our advice that can take a site offline if rushed."""
        (finding,) = audit(build(tmp_path, ""))
        assert "sticky" in finding.remediation
        assert "Ramp up" in finding.remediation


class TestDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (30, "30 seconds"),
            (60, "about 1 minute"),
            (3600, "about 1 hour"),
            (86400, "about 1 day"),
            (2592000, "about 30 days"),
        ],
    )
    def test_it_reads_as_a_duration_a_person_recognises(self, seconds, expected):
        assert expected in _duration(seconds)


class TestSplitSettings:
    def test_a_base_raised_by_every_environment_is_not_reported(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS + "SECURE_HSTS_SECONDS = 0\n")
        (root / "myproj/settings/production.py").write_text(
            f"from .base import *\nSECURE_HSTS_SECONDS = {ONE_YEAR}\n"
        )
        (finding,) = audit(root)
        assert finding.severity is Severity.LOW
        assert "sets a longer max-age" in finding.message
