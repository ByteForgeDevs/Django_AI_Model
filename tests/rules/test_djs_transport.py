"""DJS-006 -- redirect plain HTTP to HTTPS.

The rule is "must be True, ships False", and so are the cookie flags that follow it, so
FlagRule does the work and these tests cover the parts that are easy to get
wrong: that an unassigned setting is still reported, that it is reported once
rather than once per module, and that a base which merely omits it is not the
same thing as a base which turns it off.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity
from djaudit.rules._base import could_be_off
from djaudit.values import Value

SECURE = "SECURE_SSL_REDIRECT = True"
MARKERS = "INSTALLED_APPS = []\nDEBUG = False\nDATABASES = {}\nSECRET_KEY = 'x'\n"

FLAGS = ["SECURE_SSL_REDIRECT"]
RULES = {"SECURE_SSL_REDIRECT": "DJS-006"}


def build(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + body)
    return root


def audit(root: Path, rule: str):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == rule]


class TestCouldBeOff:
    def test_true_is_the_only_thing_that_counts_as_on(self):
        assert not could_be_off(Value.of(True))
        assert could_be_off(Value.of(False))
        assert could_be_off(Value.of(1))
        assert could_be_off(Value.of("yes"))

    def test_a_value_true_on_only_one_branch_is_still_off(self):
        both = Value.conditional([Value.of(True), Value.of(False)])
        assert could_be_off(both)

    def test_a_value_true_on_every_branch_is_on(self):
        both = Value.conditional([Value.of(True), Value.of(True)])
        assert not could_be_off(both)


class TestDetection:
    @pytest.mark.parametrize("flag", FLAGS)
    def test_a_flag_set_to_false_is_reported(self, tmp_path, flag):
        root = build(tmp_path, f"{flag} = False\n")
        assert audit(root, RULES[flag])

    @pytest.mark.parametrize("flag", FLAGS)
    def test_a_flag_that_is_never_set_is_reported(self, tmp_path, flag):
        """Django ships it off, so silence is the insecure state."""
        root = build(tmp_path, "")
        (finding,) = audit(root, RULES[flag])
        assert "never set" in finding.message

    @pytest.mark.parametrize("flag", FLAGS)
    def test_a_flag_set_to_true_is_not_reported(self, tmp_path, flag):
        root = build(tmp_path, SECURE)
        assert not audit(root, RULES[flag])

    def test_an_unassigned_flag_points_at_the_settings_module(self, tmp_path):
        """There is no line to blame, but the file that should have had one."""
        root = build(tmp_path, "")
        (finding,) = audit(root, "DJS-006")
        assert finding.location.file.endswith("settings.py")
        assert finding.location.line == 1

    def test_a_flag_set_from_the_environment_is_still_reported(self, tmp_path):
        root = build(tmp_path, "SECURE_SSL_REDIRECT = os.environ.get('S') == '1'\n")
        assert audit(root, "DJS-006")


class TestConfidenceReflectsWhatWeCanKnow:
    def test_ssl_redirect_never_claims_certainty(self, tmp_path):
        """A proxy may be doing the redirect, and we cannot see one from here."""
        root = build(tmp_path, "SECURE_SSL_REDIRECT = False\n")
        (finding,) = audit(root, "DJS-006")
        assert finding.confidence is Confidence.FIRM


class TestControls:
    def test_development_settings_are_left_alone(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS + SECURE)
        (root / "myproj/settings/development.py").write_text(
            "from .base import *\nSECURE_SSL_REDIRECT = False\n"
        )
        assert not audit(root, "DJS-006")

    def test_production_overriding_an_insecure_base_downgrades_it(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS + "SECURE_SSL_REDIRECT = False\n")
        (root / "myproj/settings/production.py").write_text(
            "from .base import *\nSECURE_SSL_REDIRECT = True\n"
        )
        # Not silenced: base.py can still be pointed at directly. Graded down
        # to where it stays out of the way, which is what split settings are.
        (finding,) = audit(root, "DJS-006")
        assert finding.severity is Severity.LOW
        assert finding.confidence is Confidence.TENTATIVE
        assert "should override it" in finding.message

    def test_a_base_that_merely_omits_the_flag_is_silent(self, tmp_path):
        """Nothing to fix: the flag is not wrong there, it just lives elsewhere."""
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS)
        (root / "myproj/settings/production.py").write_text(
            "from .base import *\nSECURE_SSL_REDIRECT = True\n"
        )
        assert not audit(root, "DJS-006")

    def test_a_base_that_writes_the_insecure_value_down_is_still_reported(self, tmp_path):
        """The distinction from the test above: a wrong value is worth saying."""
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS + "SECURE_SSL_REDIRECT = False\n")
        (root / "myproj/settings/production.py").write_text(
            "from .base import *\nSECURE_SSL_REDIRECT = True\n"
        )
        assert audit(root, "DJS-006")

    def test_one_missing_flag_is_one_finding_not_one_per_module(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS)
        (root / "myproj/settings/production.py").write_text("from .base import *\n")
        (finding,) = audit(root, "DJS-006")
        assert "production" in finding.location.file
