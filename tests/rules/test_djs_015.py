"""DJS-015 -- CORS is open to every origin.

Two things make this rule more than a flag check. django-cors-headers reads
``getattr(settings, "CORS_ALLOW_ALL_ORIGINS", getattr(settings,
"CORS_ORIGIN_ALLOW_ALL", False))``, so the modern name wins by being assigned at
all rather than by being assigned something; and the setting only produces a
header when the middleware that reads it is installed.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"
CORS_MW = 'MIDDLEWARE = ["corsheaders.middleware.CorsMiddleware"]\n'
PLAIN_MW = 'MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]\n'


def build(tmp_path: Path, body: str, middleware: str = CORS_MW) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + middleware + body)
    return root


def audit(root: Path):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == "DJS-015"]


class TestTheAliasChain:
    def test_the_modern_name_is_read(self, tmp_path):
        assert audit(build(tmp_path, "CORS_ALLOW_ALL_ORIGINS = True\n"))

    def test_the_pre_3_5_name_is_read_too(self, tmp_path):
        """What NetBox writes, and what most unmigrated projects write."""
        found = audit(build(tmp_path, "CORS_ORIGIN_ALLOW_ALL = True\n"))
        assert len(found) == 1
        assert "CORS_ORIGIN_ALLOW_ALL" in found[0].message

    def test_the_modern_name_wins_by_existing_at_all(self, tmp_path):
        """getattr finds the new name first, so the old one is already dead.

        Reporting the legacy True here would be telling someone to fix a line
        the package never reads.
        """
        assert not audit(
            build(
                tmp_path,
                "CORS_ORIGIN_ALLOW_ALL = True\nCORS_ALLOW_ALL_ORIGINS = False\n",
            )
        )

    def test_the_legacy_name_still_decides_when_the_modern_one_is_absent(self, tmp_path):
        assert audit(build(tmp_path, "CORS_ORIGIN_ALLOW_ALL = True\n"))

    @pytest.mark.parametrize("body", ["", "CORS_ALLOW_ALL_ORIGINS = False\n"])
    def test_off_or_unset_is_silent(self, tmp_path, body):
        assert not audit(build(tmp_path, body))


class TestPreconditions:
    def test_without_the_middleware_the_setting_emits_nothing(self, tmp_path):
        assert not audit(build(tmp_path, "CORS_ALLOW_ALL_ORIGINS = True\n", middleware=PLAIN_MW))

    def test_an_unreadable_middleware_list_does_not_buy_silence(self, tmp_path):
        """Projects assemble MIDDLEWARE conditionally all the time, and reading
        "cannot tell" as "not installed" would lose every one of them."""
        assert audit(
            build(
                tmp_path,
                "CORS_ALLOW_ALL_ORIGINS = True\n",
                middleware="MIDDLEWARE = build_middleware()\n",
            )
        )

    def test_credentials_hand_the_finding_to_djs_016(self, tmp_path):
        """One mistake, one ticket. The pairing is a different and worse defect."""
        assert not audit(
            build(
                tmp_path,
                "CORS_ALLOW_ALL_ORIGINS = True\nCORS_ALLOW_CREDENTIALS = True\n",
            )
        )


class TestGrading:
    def test_it_is_medium_and_firm(self, tmp_path):
        finding = audit(build(tmp_path, "CORS_ALLOW_ALL_ORIGINS = True\n"))[0]
        assert finding.severity is Severity.MEDIUM
        assert finding.confidence is Confidence.FIRM

    def test_the_message_names_the_header_it_produces(self, tmp_path):
        finding = audit(build(tmp_path, "CORS_ALLOW_ALL_ORIGINS = True\n"))[0]
        assert "Access-Control-Allow-Origin: *" in finding.message

    def test_the_remediation_names_the_setting_that_replaces_it(self, tmp_path):
        finding = audit(build(tmp_path, "CORS_ALLOW_ALL_ORIGINS = True\n"))[0]
        assert "CORS_ALLOWED_ORIGINS" in finding.remediation
        assert "CORS_URLS_REGEX" in finding.remediation
