"""DJS-005 -- a service credential readable in the source.

This is the first rule that matches settings by name rather than by knowing
them, so most of the work is in not firing. `PASSWORD_HASHERS`,
`AUTH_PASSWORD_VALIDATORS` and `CACHE_KEY_PREFIX` all read like credentials and
none of them is one; a rule that flags them is a rule that gets switched off.
"""

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity
from djaudit.rules.secrets import looks_like_a_secret_name

MARKERS = "INSTALLED_APPS = []\nDEBUG = False\nDATABASES = {}\n"
VALUE = "fixture-credential-value-not-real-0123456789"


def build(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "project"
    path = root / "myproj/settings.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import os\n" + MARKERS + "SECRET_KEY = os.environ['SK']\n" + body)
    return root


def audit(root: Path, rule: str = "DJS-005"):
    findings = engine.run(
        root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE
    ).findings
    return [f for f in findings if f.rule_id == rule]


class TestNameMatching:
    @pytest.mark.parametrize(
        "name",
        [
            "STRIPE_SECRET_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "EMAIL_HOST_PASSWORD",
            "TWILIO_AUTH_TOKEN",
            "GITHUB_TOKEN",
            "SLACK_CLIENT_SECRET",
            "S3_ACCESS_KEY",
            "MAILGUN_API_KEY",
            "JWT_SIGNING_KEY",
            "FIELD_ENCRYPTION_KEY",
            "SERVICE_PRIVATE_KEY",
            "REGISTRY_CREDENTIALS",
            "LEGACY_PASSWD",
            "VENDOR_APIKEY",
            "OAUTH_REFRESH_TOKEN",
        ],
    )
    def test_credential_names_match(self, name):
        assert looks_like_a_secret_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "PASSWORD_HASHERS",
            "AUTH_PASSWORD_VALIDATORS",
            "PASSWORD_RESET_TIMEOUT",
            "CACHE_KEY_PREFIX",
            "SORT_KEY_DEFAULT",
            "OAUTH_TOKEN_URL",
            "TOKEN_LIFETIME",
            "API_TOKEN_PEPPERS",
            "EMAIL_SSL_KEYFILE",
            "TRELLO_APP_KEY",
            "METRICS_KEY",
            "SESSION_COOKIE_NAME",
        ],
    )
    def test_configuration_names_do_not_match(self, name):
        """Every one of these is real: the first eight ship in Django, NetBox or
        healthchecks, and all read like secrets while holding policy or paths."""
        assert not looks_like_a_secret_name(name)

    def test_secret_key_itself_is_left_to_the_rules_that_know_it(self):
        assert not looks_like_a_secret_name("SECRET_KEY")


class TestDetection:
    def test_a_credential_in_the_source_is_reported(self, tmp_path):
        root = build(tmp_path, f'STRIPE_SECRET_KEY = "{VALUE}"\n')
        (finding,) = audit(root)
        assert finding.severity is Severity.HIGH
        assert finding.confidence is Confidence.CERTAIN

    def test_the_setting_is_named_so_the_reader_knows_what_to_revoke(self, tmp_path):
        root = build(tmp_path, f'STRIPE_SECRET_KEY = "{VALUE}"\n')
        (finding,) = audit(root)
        assert "STRIPE_SECRET_KEY" in finding.message

    def test_key_material_is_critical_rather_than_high(self, tmp_path):
        """A service token is bounded by that service; a signing key is not."""
        root = build(tmp_path, f'JWT_SIGNING_KEY = "{VALUE}"\n')
        (finding,) = audit(root)
        assert finding.severity is Severity.CRITICAL

    def test_a_service_token_stays_high(self, tmp_path):
        root = build(tmp_path, f'TWILIO_AUTH_TOKEN = "{VALUE}"\n')
        (finding,) = audit(root)
        assert finding.severity is Severity.HIGH

    def test_an_environment_fallback_is_reported_but_only_firm(self, tmp_path):
        root = build(tmp_path, f'AWS_SECRET_ACCESS_KEY = os.environ.get("A", "{VALUE}")\n')
        (finding,) = audit(root)
        assert finding.confidence is Confidence.FIRM
        assert "falls back to" in finding.message

    def test_each_credential_is_its_own_finding(self, tmp_path):
        root = build(
            tmp_path,
            f'STRIPE_SECRET_KEY = "{VALUE}"\nMAILGUN_API_KEY = "{VALUE}-two"\n',
        )
        assert len(audit(root)) == 2


class TestControls:
    def test_a_credential_read_from_the_environment_is_not_reported(self, tmp_path):
        root = build(tmp_path, 'GITHUB_TOKEN = os.environ["GH_TOKEN"]\n')
        assert not audit(root)

    def test_an_empty_credential_is_not_reported(self, tmp_path):
        """An unset credential is an unfinished deployment, not a disclosure."""
        root = build(tmp_path, 'EMAIL_HOST_PASSWORD = ""\n')
        assert not audit(root)

    def test_a_credential_set_to_none_is_not_reported(self, tmp_path):
        root = build(tmp_path, "EMAIL_HOST_PASSWORD = os.environ.get('PW')\n")
        assert not audit(root)

    def test_an_import_path_is_not_reported(self, tmp_path):
        """`FOO_TOKEN = "myapp.tokens.Backend"` names a class, not a secret."""
        root = build(tmp_path, 'NOTIFICATION_TOKEN = "myapp.notifications.TokenBackend"\n')
        assert not audit(root)

    def test_a_non_string_value_is_not_reported(self, tmp_path):
        root = build(tmp_path, "TOKEN_ROTATION_TOKEN = 30\n")
        assert not audit(root)

    def test_django_own_password_settings_are_not_reported(self, tmp_path):
        root = build(
            tmp_path,
            'PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]\n'
            'AUTH_PASSWORD_VALIDATORS = [{"NAME": "x.y.Z"}]\n'
            "PASSWORD_RESET_TIMEOUT = 3600\n",
        )
        assert not audit(root)

    def test_the_secret_key_is_not_reported_twice(self, tmp_path):
        """DJS-002 and DJS-003 own it and say more about it than this rule can."""
        root = tmp_path / "project"
        path = root / "myproj/settings.py"
        path.parent.mkdir(parents=True)
        path.write_text(MARKERS + f'SECRET_KEY = "{VALUE}"\n')
        assert not audit(root)
        assert audit(root, "DJS-002")

    def test_development_settings_are_left_alone(self, tmp_path):
        root = tmp_path / "project"
        (root / "myproj/settings").mkdir(parents=True)
        (root / "myproj/settings/__init__.py").write_text("")
        (root / "myproj/settings/base.py").write_text(MARKERS + "SECRET_KEY = 'x'\n")
        (root / "myproj/settings/development.py").write_text(
            f'from .base import *\nSTRIPE_SECRET_KEY = "{VALUE}"\n'
        )
        assert not audit(root)


class TestRedaction:
    def test_the_credential_never_appears_in_the_finding(self, tmp_path):
        root = build(tmp_path, f'STRIPE_SECRET_KEY = "{VALUE}"\n')
        (finding,) = audit(root)
        assert VALUE not in str(finding.to_dict())

    def test_the_snippet_is_redacted_too(self, tmp_path):
        root = build(tmp_path, f'STRIPE_SECRET_KEY = "{VALUE}"\n')
        (finding,) = audit(root)
        assert VALUE not in finding.location.snippet
