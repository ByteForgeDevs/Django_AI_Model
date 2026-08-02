"""DJS-008 -- HSTS does not cover subdomains.

The first rule whose answer depends on another setting. Off is the *correct*
value for SECURE_HSTS_INCLUDE_SUBDOMAINS while HSTS is switched off, so most of
these tests are about staying quiet rather than about detecting anything.
"""

from pathlib import Path

from djaudit import engine
from djaudit.models import Confidence, Severity
from djaudit.rules.transport import ONE_YEAR

MARKERS = "INSTALLED_APPS = []\nDEBUG = False\nDATABASES = {}\nSECRET_KEY = 'x'\n"


def build(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + body)
    return root


def audit(root: Path, rule: str = "DJS-008"):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == rule]


class TestOnlyWhenHstsIsOn:
    def test_silent_when_hsts_is_never_configured(self, tmp_path):
        """The overwhelmingly common case, and the one that would be all noise."""
        assert not audit(build(tmp_path, ""))

    def test_silent_when_hsts_is_explicitly_zero(self, tmp_path):
        """NetBox's exact shape: the flag is off and so is the policy it extends."""
        assert not audit(
            build(tmp_path, "SECURE_HSTS_SECONDS = 0\nSECURE_HSTS_INCLUDE_SUBDOMAINS = False\n")
        )

    def test_reported_once_hsts_is_switched_on(self, tmp_path):
        root = build(tmp_path, f"SECURE_HSTS_SECONDS = {ONE_YEAR}\n")
        (finding,) = audit(root)
        assert "is never set" in finding.message

    def test_reported_when_hsts_is_on_but_short(self, tmp_path):
        """A ramp still sends the header, so subdomains are still uncovered."""
        assert audit(build(tmp_path, "SECURE_HSTS_SECONDS = 60\n"))

    def test_silent_when_the_duration_cannot_be_resolved(self, tmp_path):
        """Guessing here would fire on every project that reads it from the env."""
        root = build(
            tmp_path,
            "SECURE_HSTS_SECONDS = int(os.environ['HSTS'])\n"
            "SECURE_HSTS_INCLUDE_SUBDOMAINS = False\n",
        )
        assert not audit(root)


class TestDetection:
    def test_explicit_false_with_hsts_on_is_reported(self, tmp_path):
        root = build(
            tmp_path,
            f"SECURE_HSTS_SECONDS = {ONE_YEAR}\nSECURE_HSTS_INCLUDE_SUBDOMAINS = False\n",
        )
        (finding,) = audit(root)
        assert finding.severity is Severity.LOW
        assert "every subdomain is still reachable over plain HTTP" in finding.message

    def test_true_with_hsts_on_is_not_reported(self, tmp_path):
        root = build(
            tmp_path,
            f"SECURE_HSTS_SECONDS = {ONE_YEAR}\nSECURE_HSTS_INCLUDE_SUBDOMAINS = True\n",
        )
        assert not audit(root)

    def test_the_remediation_says_to_check_subdomains_first(self, tmp_path):
        """Same stickiness trap as DJS-007, and a worse blast radius."""
        root = build(tmp_path, f"SECURE_HSTS_SECONDS = {ONE_YEAR}\n")
        (finding,) = audit(root)
        assert "sticky" in finding.remediation
        assert "cannot do HTTPS becomes unreachable" in finding.remediation
