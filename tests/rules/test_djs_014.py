"""DJS-014 -- CSRF_TRUSTED_ORIGINS is too broad or has no effect.

Every case here is derived from four lines of ``CsrfViewMiddleware``: it takes
``urlsplit(origin).netloc``, strips leading asterisks, and hands the result to
``is_same_domain``, which treats a pattern as a wildcard only when it starts
with a dot. The rule is a reading of those lines, so the tests are too.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity
from djaudit.rules.hosts import csrf_pattern, csrf_verdict

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"


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
    return [f for f in findings if f.rule_id == "DJS-014"]


def one(tmp_path: Path, origins: str):
    found = audit(build(tmp_path, f"CSRF_TRUSTED_ORIGINS = {origins}\n"))
    assert len(found) == 1, f"expected exactly one finding, got {found}"
    return found[0]


class TestWhatDjangoActuallyMatches:
    """``csrf_pattern`` has to agree with the middleware or nothing below holds."""

    @pytest.mark.parametrize(
        ("entry", "pattern"),
        [
            ("https://app.example.com", "app.example.com"),
            ("https://*.example.com", ".example.com"),
            ("https://app.example.com:8443", "app.example.com:8443"),
            # No scheme means urlsplit finds no netloc at all -- the whole
            # string lands in .path, which the middleware never looks at.
            ("example.com", ""),
            ("*", ""),
            ("https://*", ""),
            ("https://*example.com", "example.com"),
        ],
    )
    def test_the_derived_pattern(self, entry, pattern):
        assert csrf_pattern(entry) == pattern


class TestClassification:
    @pytest.mark.parametrize(
        "entry",
        ["https://app.example.com", "http://localhost:8000", "https://example.com:443"],
    )
    def test_a_named_origin_is_fine(self, entry):
        assert csrf_verdict(entry) == "fine"

    def test_a_dotted_wildcard_is_broad(self):
        assert csrf_verdict("https://*.example.com") == "broad"

    @pytest.mark.parametrize("entry", ["https://*.com", "https://*.test"])
    def test_a_wildcard_over_one_label_is_a_whole_tld(self, entry):
        assert csrf_verdict(entry) == "tld"

    @pytest.mark.parametrize("entry", ["example.com", ".example.com", "*"])
    def test_a_scheme_less_entry_is_inert(self, entry):
        """The pre-4.0 spelling. It parses, it is accepted, it matches nothing."""
        assert csrf_verdict(entry) == "inert"

    @pytest.mark.parametrize("entry", ["https://*", "https://foo*.example.com"])
    def test_an_asterisk_without_a_following_dot_is_inert(self, entry):
        assert csrf_verdict(entry) == "inert"

    def test_a_non_string_entry_is_not_our_business(self):
        assert csrf_verdict(None) == "fine"


class TestReporting:
    def test_a_well_formed_list_is_silent(self, tmp_path):
        assert not audit(
            build(
                tmp_path,
                'CSRF_TRUSTED_ORIGINS = ["https://app.example.com", "https://b.example.com"]\n',
            )
        )

    def test_the_django_default_is_silent(self, tmp_path):
        """Empty is the correct state of this setting, unlike ALLOWED_HOSTS."""
        assert not audit(build(tmp_path, ""))
        assert not audit(build(tmp_path, "CSRF_TRUSTED_ORIGINS = []\n"))

    def test_a_scheme_less_entry_is_reported(self, tmp_path):
        finding = one(tmp_path, '["example.com"]')
        assert finding.severity is Severity.MEDIUM
        assert finding.confidence is Confidence.FIRM
        assert "reduces to nothing" in finding.message

    def test_a_subdomain_wildcard_is_reported_quietly(self, tmp_path):
        """Whether every subdomain is yours is the one thing we cannot read."""
        finding = one(tmp_path, '["https://*.example.com"]')
        assert finding.severity is Severity.MEDIUM
        assert finding.confidence is Confidence.TENTATIVE
        assert "every subdomain" in finding.message

    def test_a_wildcard_over_a_tld_outranks_the_others(self, tmp_path):
        finding = one(tmp_path, '["https://*.com", "https://*.example.com", "bare.com"]')
        assert finding.severity is Severity.HIGH
        assert finding.confidence is Confidence.FIRM
        assert "top-level domain" in finding.message
        assert "'https://*.com'" in finding.message

    def test_one_bad_entry_among_good_ones_is_enough(self, tmp_path):
        finding = one(tmp_path, '["https://app.example.com", "other.example.com"]')
        assert "'other.example.com'" in finding.message
        assert "'https://app.example.com'" not in finding.message

    def test_several_inert_entries_are_named_with_what_they_reduce_to(self, tmp_path):
        finding = one(tmp_path, '["example.com", "https://foo*.example.com"]')
        assert "'example.com' to nothing" in finding.message
        assert "'https://foo*.example.com' to 'foo*.example.com'" in finding.message

    def test_a_bad_entry_hidden_in_one_branch_is_still_found(self, tmp_path):
        root = build(
            tmp_path,
            'if os.environ.get("STAGING"):\n'
            '    CSRF_TRUSTED_ORIGINS = ["https://*.example.com"]\n'
            "else:\n"
            '    CSRF_TRUSTED_ORIGINS = ["https://app.example.com"]\n',
        )
        assert audit(root)

    def test_an_unreadable_list_says_nothing(self, tmp_path):
        assert not audit(build(tmp_path, "CSRF_TRUSTED_ORIGINS = build_origins()\n"))


class TestRemediation:
    def test_it_names_the_spelling_that_looks_right_and_is_not(self, tmp_path):
        finding = one(tmp_path, '["https://*"]')
        assert "https://*.example.com" in finding.remediation
        assert "https://*example.com" in finding.remediation
