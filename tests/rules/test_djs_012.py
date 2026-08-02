"""DJS-012 -- SECURE_PROXY_SSL_HEADER trusts a request header.

This rule fires on correctly configured deployments by design, so most of the
work is in not being noise: absent is safe and silent, a well-formed value is
low/tentative and stays out of a default run, and the one branch that is a real
bug -- a header spelled so that it can never match -- is escalated.
"""

from pathlib import Path

from djaudit import engine
from djaudit.models import Confidence, Severity

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
    return [f for f in findings if f.rule_id == "DJS-012"]


class TestSilence:
    def test_not_setting_it_is_the_safe_default_and_says_nothing(self, tmp_path):
        assert not audit(build(tmp_path, ""))

    def test_explicit_none_says_nothing(self, tmp_path):
        assert not audit(build(tmp_path, "SECURE_PROXY_SSL_HEADER = None\n"))

    def test_a_value_we_cannot_resolve_says_nothing(self, tmp_path):
        """Healthchecks' exact shape: built from an environment variable."""
        root = build(
            tmp_path,
            "SECURE_PROXY_SSL_HEADER = tuple(os.environ['P'].split(',', maxsplit=1))\n",
        )
        assert not audit(root)


class TestWellFormed:
    def test_the_textbook_value_is_reported_quietly(self, tmp_path):
        """NetBox's exact line. Correct configuration, worth exactly one review."""
        root = build(tmp_path, "SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')\n")
        (finding,) = audit(root)
        assert finding.severity is Severity.LOW
        assert finding.confidence is Confidence.TENTATIVE

    def test_it_names_the_header_the_way_a_reader_would_write_it(self, tmp_path):
        """HTTP_X_FORWARDED_PROTO is a WSGI key; nobody types it into a proxy."""
        root = build(tmp_path, "SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')\n")
        (finding,) = audit(root)
        assert "X-Forwarded-Proto" in finding.message

    def test_the_remediation_asks_for_the_invariant_to_be_checked_once(self, tmp_path):
        root = build(tmp_path, "SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')\n")
        (finding,) = audit(root)
        assert "strips any copy the client supplied" in finding.remediation
        assert "suppress this finding" in finding.remediation


class TestSilentlyBroken:
    def test_an_http_header_spelling_can_never_match_and_is_escalated(self, tmp_path):
        """request.META is keyed by WSGI name, so this setting does nothing."""
        root = build(tmp_path, "SECURE_PROXY_SSL_HEADER = ('X-Forwarded-Proto', 'https')\n")
        (finding,) = audit(root)
        assert finding.severity is Severity.MEDIUM
        assert finding.confidence is Confidence.FIRM
        assert "has no effect at all" in finding.message

    def test_the_remediation_spells_out_the_working_value(self, tmp_path):
        root = build(tmp_path, "SECURE_PROXY_SSL_HEADER = ('X-Forwarded-Proto', 'https')\n")
        (finding,) = audit(root)
        assert "'HTTP_X_FORWARDED_PROTO'" in finding.remediation

    def test_a_lowercase_spelling_is_caught_too(self, tmp_path):
        root = build(tmp_path, "SECURE_PROXY_SSL_HEADER = ('x-forwarded-proto', 'https')\n")
        (finding,) = audit(root)
        assert "'HTTP_X_FORWARDED_PROTO'" in finding.remediation

    def test_a_bare_string_crashes_django_on_every_request(self, tmp_path):
        """Django unpacks this into two names and raises when it cannot."""
        root = build(tmp_path, "SECURE_PROXY_SSL_HEADER = 'HTTP_X_FORWARDED_PROTO'\n")
        (finding,) = audit(root)
        assert finding.severity is Severity.MEDIUM
        assert "ImproperlyConfigured" in finding.message

    def test_a_one_item_tuple_crashes_too(self, tmp_path):
        root = build(tmp_path, "SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO',)\n")
        (finding,) = audit(root)
        assert "not a two-item sequence" in finding.message
