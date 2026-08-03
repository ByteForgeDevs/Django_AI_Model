"""DJS-017 -- clickjacking protection is off.

Two ways to end up framable, one fix family, one rule. Either the header is
never sent because ``XFrameOptionsMiddleware`` is not installed, or it is sent
with a value browsers do not act on. ``SAMEORIGIN`` is neither of those and is
deliberately not reported.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"
XFRAME = "django.middleware.clickjacking.XFrameOptionsMiddleware"
WITH_MW = f'MIDDLEWARE = ["{XFRAME}"]\n'
WITHOUT_MW = 'MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]\n'


def build(tmp_path: Path, body: str = "", middleware: str = WITH_MW) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + middleware + body)
    return root


def audit(root: Path):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == "DJS-017"]


class TestValuesBrowsersHonour:
    @pytest.mark.parametrize("value", ["DENY", "SAMEORIGIN", "deny", "sameorigin"])
    def test_the_two_defined_values_are_silent_in_any_case(self, tmp_path, value):
        """Django uppercases the setting before sending it, so case is not a defect."""
        assert not audit(build(tmp_path, f'X_FRAME_OPTIONS = "{value}"\n'))

    def test_leaving_it_unset_is_silent_because_django_defaults_to_deny(self, tmp_path):
        assert not audit(build(tmp_path))

    def test_sameorigin_is_a_choice_not_a_finding(self, tmp_path):
        """NetBox sets exactly this, and Django's own check calls DENY a
        preference rather than a requirement."""
        assert not audit(build(tmp_path, 'X_FRAME_OPTIONS = "SAMEORIGIN"\n'))


class TestValuesBrowsersIgnore:
    def test_allowall_is_reported(self, tmp_path):
        found = audit(build(tmp_path, 'X_FRAME_OPTIONS = "ALLOWALL"\n'))
        assert len(found) == 1
        assert "not a value the X-Frame-Options header defines" in found[0].message

    def test_allow_from_gets_its_own_explanation(self, tmp_path):
        found = audit(build(tmp_path, 'X_FRAME_OPTIONS = "ALLOW-FROM https://x.test"\n'))
        assert len(found) == 1
        assert "every current browser has dropped support" in found[0].message

    def test_the_value_is_quoted_as_it_was_written(self, tmp_path):
        """Uppercasing is for the comparison, not for the report."""
        found = audit(build(tmp_path, 'X_FRAME_OPTIONS = "allow-from https://x.test"\n'))
        assert "'allow-from https://x.test'" in found[0].message

    def test_it_is_reported_at_medium_and_firm(self, tmp_path):
        finding = audit(build(tmp_path, 'X_FRAME_OPTIONS = "ALLOWALL"\n'))[0]
        assert finding.severity is Severity.MEDIUM
        assert finding.confidence is Confidence.FIRM


class TestTheMiddleware:
    def test_without_it_nothing_is_sent_at_all(self, tmp_path):
        found = audit(build(tmp_path, middleware=WITHOUT_MW))
        assert len(found) == 1
        assert "XFrameOptionsMiddleware is not installed" in found[0].message

    def test_that_branch_is_graded_beside_its_sibling_not_below_it(self, tmp_path):
        """The setting is at its default, which normally costs a step of
        confidence -- but this branch's evidence is MIDDLEWARE, not the value."""
        finding = audit(build(tmp_path, middleware=WITHOUT_MW))[0]
        assert finding.confidence is Confidence.FIRM

    def test_an_unreadable_middleware_list_is_not_evidence_of_absence(self, tmp_path):
        assert not audit(build(tmp_path, middleware="MIDDLEWARE = build()\n"))

    def test_a_partly_readable_list_is_not_evidence_of_absence(self, tmp_path):
        """NetBox's shape: some branches resolve, one does not."""
        root = build(
            tmp_path,
            middleware=(
                'if os.environ.get("EXTRA"):\n'
                "    MIDDLEWARE = build()\n"
                "else:\n"
                '    MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]\n'
            ),
        )
        assert not audit(root)

    @pytest.mark.parametrize(
        "csp",
        [
            "CSP_FRAME_ANCESTORS = [\"'none'\"]\n",
            'CONTENT_SECURITY_POLICY = {"DIRECTIVES": {}}\n',
            'SECURE_CSP = {"frame-ancestors": []}\n',
        ],
    )
    def test_a_content_security_policy_replaces_it(self, tmp_path, csp):
        """frame-ancestors supersedes X-Frame-Options, so a project that has
        moved to CSP has not left anything switched off."""
        assert not audit(build(tmp_path, csp, middleware=WITHOUT_MW))

    def test_a_bad_value_is_still_reported_without_the_middleware(self, tmp_path):
        """One finding, not two -- it is one site that can be framed."""
        found = audit(build(tmp_path, 'X_FRAME_OPTIONS = "ALLOWALL"\n', WITHOUT_MW))
        assert len(found) == 1


class TestRemediation:
    def test_it_points_at_csp_for_the_case_the_header_cannot_express(self, tmp_path):
        finding = audit(build(tmp_path, 'X_FRAME_OPTIONS = "ALLOW-FROM https://x.test"\n'))[0]
        assert "frame-ancestors" in finding.remediation
        assert "xframe_options_exempt" in finding.remediation
