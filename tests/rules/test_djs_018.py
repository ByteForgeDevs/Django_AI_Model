"""DJS-018 -- MIME type sniffing is allowed.

The only flag in this family Django already ships switched on, which changes
what the rule can say. There is no "you forgot" case: reaching a finding means
someone wrote the line and set it to False.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity

MARKERS = "INSTALLED_APPS = []\nDATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"


def build(tmp_path: Path, body: str = "") -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + body)
    return root


def audit(root: Path):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == "DJS-018"]


class TestWhenItFires:
    def test_switching_it_off_is_reported(self, tmp_path):
        found = audit(build(tmp_path, "SECURE_CONTENT_TYPE_NOSNIFF = False\n"))
        assert len(found) == 1
        assert found[0].severity is Severity.MEDIUM
        assert found[0].confidence is Confidence.FIRM

    @pytest.mark.parametrize("body", ["", "SECURE_CONTENT_TYPE_NOSNIFF = True\n"])
    def test_leaving_it_alone_or_confirming_it_is_silent(self, tmp_path, body):
        assert not audit(build(tmp_path, body))

    def test_never_assigning_it_is_not_a_forgotten_flag(self, tmp_path):
        """Django's default is True, so an absent setting is already correct --
        the opposite of every other flag in this family."""
        assert not audit(build(tmp_path))

    def test_an_unreadable_value_says_nothing(self, tmp_path):
        assert not audit(build(tmp_path, "SECURE_CONTENT_TYPE_NOSNIFF = decide()\n"))

    def test_a_branch_that_switches_it_off_is_enough(self, tmp_path):
        assert audit(
            build(
                tmp_path,
                'if os.environ.get("LEGACY"):\n'
                "    SECURE_CONTENT_TYPE_NOSNIFF = False\n"
                "else:\n"
                "    SECURE_CONTENT_TYPE_NOSNIFF = True\n",
            )
        )


class TestWhatItSays:
    def test_the_message_names_the_header_that_stops_being_sent(self, tmp_path):
        message = audit(build(tmp_path, "SECURE_CONTENT_TYPE_NOSNIFF = False\n"))[0].message
        assert "X-Content-Type-Options: nosniff" in message

    def test_the_rationale_names_uploads_as_the_route(self, tmp_path):
        rationale = audit(build(tmp_path, "SECURE_CONTENT_TYPE_NOSNIFF = False\n"))[0].rationale
        assert "stored XSS" in rationale

    def test_the_remediation_answers_the_reason_people_switch_it_off(self, tmp_path):
        """It is nearly always turned off to make one response render, and
        Content-Disposition is the fix for that."""
        remediation = audit(build(tmp_path, "SECURE_CONTENT_TYPE_NOSNIFF = False\n"))[0].remediation
        assert "Content-Disposition" in remediation
