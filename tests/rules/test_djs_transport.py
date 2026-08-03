"""DJS-006, DJS-009, DJS-010, DJS-011 -- transport and cookie flags.

All four are "must be True, ships False", so the shared FlagRule does the work
and these tests are mostly about the two things that are not shared: that an
unassigned setting is still reported, and that the confidence differs where the
confidence should differ.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity
from djaudit.rules._base import could_be_off
from djaudit.values import Value

SECURE = "\n".join(
    [
        "SECURE_SSL_REDIRECT = True",
        "SESSION_COOKIE_SECURE = True",
        "CSRF_COOKIE_SECURE = True",
        "SESSION_COOKIE_HTTPONLY = True",
    ]
)
MARKERS = "INSTALLED_APPS = []\nDEBUG = False\nDATABASES = {}\nSECRET_KEY = 'x'\n"

FLAGS = ["SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE"]
RULES = {
    "SECURE_SSL_REDIRECT": "DJS-006",
    "SESSION_COOKIE_SECURE": "DJS-009",
    "CSRF_COOKIE_SECURE": "DJS-010",
    "SESSION_COOKIE_HTTPONLY": "DJS-011",
}


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
        """Django ships all three off, so silence is the insecure state."""
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
        (finding,) = audit(root, "DJS-009")
        assert finding.location.file.endswith("settings.py")
        assert finding.location.line == 1

    def test_a_flag_set_from_the_environment_is_still_reported(self, tmp_path):
        root = build(tmp_path, "SESSION_COOKIE_SECURE = os.environ.get('S') == '1'\n")
        assert audit(root, "DJS-009")


class TestConfidenceReflectsWhatWeCanKnow:
    def test_a_cookie_flag_off_in_the_source_is_certain(self, tmp_path):
        """Nothing in front of Django changes a cookie attribute."""
        root = build(tmp_path, "SESSION_COOKIE_SECURE = False\n")
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.CERTAIN

    def test_ssl_redirect_never_claims_certainty(self, tmp_path):
        """A proxy may be doing the redirect, and we cannot see one from here."""
        root = build(tmp_path, "SECURE_SSL_REDIRECT = False\n")
        (finding,) = audit(root, "DJS-006")
        assert finding.confidence is Confidence.FIRM

    def test_relying_on_djangos_default_costs_a_step(self, tmp_path):
        root = build(tmp_path, "")
        (finding,) = audit(root, "DJS-009")
        assert finding.confidence is Confidence.FIRM

    def test_the_session_cookie_outranks_the_csrf_cookie(self, tmp_path):
        """One is a session to steal; the other needs a second flaw to use."""
        root = build(tmp_path, "SESSION_COOKIE_SECURE = False\nCSRF_COOKIE_SECURE = False\n")
        (session,) = audit(root, "DJS-009")
        (csrf,) = audit(root, "DJS-010")
        assert session.severity is Severity.HIGH
        assert csrf.severity is Severity.MEDIUM


class TestHttpOnly:
    def test_djangos_default_is_secure_so_silence_is_correct(self, tmp_path):
        """The only rule of the four where doing nothing is the right answer."""
        root = build(tmp_path, "")
        assert not audit(root, "DJS-011")

    def test_turning_it_off_is_reported(self, tmp_path):
        root = build(tmp_path, "SESSION_COOKIE_HTTPONLY = False\n")
        (finding,) = audit(root, "DJS-011")
        assert finding.confidence is Confidence.CERTAIN


class TestControls:
    def test_development_settings_are_left_alone(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS + SECURE)
        (root / "myproj/settings/development.py").write_text(
            "from .base import *\nSESSION_COOKIE_SECURE = False\n"
        )
        assert not audit(root, "DJS-009")

    def test_production_overriding_an_insecure_base_downgrades_it(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS + "SESSION_COOKIE_SECURE = False\n")
        (root / "myproj/settings/production.py").write_text(
            "from .base import *\nSESSION_COOKIE_SECURE = True\n"
        )
        # Not silenced: base.py can still be pointed at directly. Graded down
        # to where it stays out of the way, which is what split settings are.
        (finding,) = audit(root, "DJS-009")
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
            "from .base import *\nSESSION_COOKIE_SECURE = True\n"
        )
        assert not audit(root, "DJS-009")

    def test_a_base_that_writes_the_insecure_value_down_is_still_reported(self, tmp_path):
        """The distinction from the test above: a wrong value is worth saying."""
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS + "SESSION_COOKIE_SECURE = False\n")
        (root / "myproj/settings/production.py").write_text(
            "from .base import *\nSESSION_COOKIE_SECURE = True\n"
        )
        assert audit(root, "DJS-009")

    def test_one_missing_flag_is_one_finding_not_one_per_module(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS)
        (root / "myproj/settings/production.py").write_text("from .base import *\n")
        (finding,) = audit(root, "DJS-009")
        assert "production" in finding.location.file
