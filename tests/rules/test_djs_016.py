"""DJS-016 -- CORS allows every origin and sends credentials.

The whole rule rests on one line of ``CorsMiddleware.add_response_headers``::

    if conf.CORS_ALLOW_ALL_ORIGINS and not conf.CORS_ALLOW_CREDENTIALS:
        response[ACCESS_CONTROL_ALLOW_ORIGIN] = "*"
    else:
        response[ACCESS_CONTROL_ALLOW_ORIGIN] = origin

A wildcard is survivable only because browsers will not send cookies to one.
Turning credentials on makes the package stop sending the wildcard, and the
protection goes with it.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"
CORS_MW = 'MIDDLEWARE = ["corsheaders.middleware.CorsMiddleware"]\n'
PLAIN_MW = 'MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]\n'
BOTH = "CORS_ALLOW_ALL_ORIGINS = True\nCORS_ALLOW_CREDENTIALS = True\n"


def build(tmp_path: Path, body: str, middleware: str = CORS_MW) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + middleware + body)
    return root


def audit(root: Path, rule_id: str = "DJS-016"):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == rule_id]


class TestThePairing:
    def test_both_together_are_reported(self, tmp_path):
        assert audit(build(tmp_path, BOTH))

    @pytest.mark.parametrize(
        "body",
        [
            "CORS_ALLOW_ALL_ORIGINS = True\n",
            "CORS_ALLOW_CREDENTIALS = True\n",
            "CORS_ALLOW_ALL_ORIGINS = False\nCORS_ALLOW_CREDENTIALS = True\n",
            "",
        ],
    )
    def test_either_one_alone_is_not_this_finding(self, tmp_path, body):
        assert not audit(build(tmp_path, body))

    def test_credentials_with_a_named_origin_list_is_the_correct_shape(self, tmp_path):
        assert not audit(
            build(
                tmp_path,
                "CORS_ALLOWED_ORIGINS = ['https://app.example.com']\n"
                "CORS_ALLOW_CREDENTIALS = True\n",
            )
        )

    def test_the_legacy_allow_all_name_pairs_the_same_way(self, tmp_path):
        found = audit(
            build(tmp_path, "CORS_ORIGIN_ALLOW_ALL = True\nCORS_ALLOW_CREDENTIALS = True\n")
        )
        assert len(found) == 1
        assert "CORS_ORIGIN_ALLOW_ALL" in found[0].message

    def test_without_the_middleware_neither_setting_emits_anything(self, tmp_path):
        assert not audit(build(tmp_path, BOTH, middleware=PLAIN_MW))


class TestItNeverDoublesUpWithDjs015:
    """The two rules carry inverse preconditions, so exactly one can fire."""

    @pytest.mark.parametrize(
        "body",
        [
            "CORS_ALLOW_ALL_ORIGINS = True\n",
            BOTH,
            "CORS_ALLOW_ALL_ORIGINS = False\n",
            "",
        ],
    )
    def test_at_most_one_of_them_reports(self, tmp_path, body):
        root = build(tmp_path, body)
        assert len(audit(root, "DJS-015")) + len(audit(root, "DJS-016")) <= 1

    def test_the_pairing_belongs_to_this_rule_not_the_other(self, tmp_path):
        root = build(tmp_path, BOTH)
        assert not audit(root, "DJS-015")
        assert audit(root, "DJS-016")


class TestGrading:
    def test_it_is_critical_and_certain(self, tmp_path):
        finding = audit(build(tmp_path, BOTH))[0]
        assert finding.severity is Severity.CRITICAL
        assert finding.confidence is Confidence.CERTAIN

    def test_the_message_explains_that_the_wildcard_stops_being_sent(self, tmp_path):
        message = audit(build(tmp_path, BOTH))[0].message
        assert "echoes the caller's own origin" in message
        assert "Access-Control-Allow-Credentials: true" in message

    def test_the_remediation_offers_both_directions(self, tmp_path):
        remediation = audit(build(tmp_path, BOTH))[0].remediation
        assert "CORS_ALLOWED_ORIGINS" in remediation
        assert "CORS_ALLOW_CREDENTIALS off" in remediation


class TestTheSingleModuleFixture:
    def test_only_the_planted_pairing_is_critical(self, api_project):
        found = engine.run(
            api_project, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
        ).findings
        assert [f.rule_id for f in found if f.severity is Severity.CRITICAL] == ["DJS-016"]
