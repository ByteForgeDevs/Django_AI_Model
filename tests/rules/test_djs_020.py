"""DJS-020 -- no password validators are configured."""

from __future__ import annotations

import pathlib

from djaudit import engine
from djaudit.models import Finding
from djaudit.rules._base import lists_entry
from djaudit.settings import resolve_all
from djaudit.values import Value

MARKERS = "DATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"
AUTH_APPS = "INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes']\n"

FOUR = (
    "AUTH_PASSWORD_VALIDATORS = [\n"
    "    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},\n"
    "    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},\n"
    "]\n"
)


def build(tmp_path: pathlib.Path, body: str, *, apps: str = AUTH_APPS) -> pathlib.Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + apps + body)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    return [f for f in engine.run(root).findings if f.rule_id == "DJS-020"]


def test_the_forgotten_case_reports_at_firm(tmp_path: pathlib.Path) -> None:
    # The normal shape of this defect, and the one that decided the ceiling:
    # graded from the Django default, it must still clear the default output
    # threshold rather than sinking to tentative.
    found = findings(build(tmp_path, ""))
    assert len(found) == 1
    assert found[0].severity.value == "medium"
    assert found[0].confidence.value == "firm"
    assert "never set" in found[0].message


def test_an_explicitly_emptied_list_is_certain(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, "AUTH_PASSWORD_VALIDATORS = []\n"))
    assert len(found) == 1
    assert found[0].confidence.value == "certain"


def test_configured_validators_stay_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, FOUR)) == []


def test_one_validator_is_enough_to_stay_silent(tmp_path: pathlib.Path) -> None:
    # The rule is about the absence of a policy, not about which validators a
    # project chose. Grading the choice would be opinion rather than evidence.
    body = "AUTH_PASSWORD_VALIDATORS = [{'NAME': 'accounts.validators.Corporate'}]\n"
    assert findings(build(tmp_path, body)) == []


def test_every_finding_carries_the_caveat(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, "AUTH_PASSWORD_VALIDATORS = []\n"))
    assert "form or serializer" in found[0].message


def test_a_project_without_contrib_auth_is_not_reported(tmp_path: pathlib.Path) -> None:
    apps = "INSTALLED_APPS = ['django.contrib.contenttypes']\n"
    assert findings(build(tmp_path, "", apps=apps)) == []


def test_an_unreadable_app_list_is_still_reported(tmp_path: pathlib.Path) -> None:
    # "Could not read INSTALLED_APPS" must not be read as "auth is not
    # installed", or every conditionally built app list becomes a miss.
    apps = "INSTALLED_APPS = build_apps()\n"
    assert len(findings(build(tmp_path, "", apps=apps))) == 1


def test_a_branch_that_installs_auth_is_enough(tmp_path: pathlib.Path) -> None:
    apps = (
        "import os\n"
        "if os.environ.get('LDAP'):\n"
        "    INSTALLED_APPS = ['django.contrib.contenttypes']\n"
        "else:\n"
        "    INSTALLED_APPS = ['django.contrib.auth']\n"
    )
    assert len(findings(build(tmp_path, "", apps=apps))) == 1


def test_an_unreadable_validator_list_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, "AUTH_PASSWORD_VALIDATORS = load_policy()\n")) == []


def test_lists_entry_is_three_valued(tmp_path: pathlib.Path) -> None:
    from djaudit.discovery import build_context

    readable = next(iter(resolve_all(build_context(build(tmp_path / "a", ""))).values()))
    assert lists_entry(readable, "INSTALLED_APPS", "django.contrib.auth") is True
    assert lists_entry(readable, "INSTALLED_APPS", "django.contrib.admin") is False

    opaque = next(
        iter(
            resolve_all(
                build_context(build(tmp_path / "b", "", apps="INSTALLED_APPS = f()\n"))
            ).values()
        )
    )
    assert lists_entry(opaque, "INSTALLED_APPS", "django.contrib.auth") is None


def test_definitely_empty_rejects_an_unknown_value() -> None:
    from djaudit.rules._base import definitely_empty

    assert definitely_empty(Value.of([])) is True
    assert definitely_empty(Value.unknown("built at runtime")) is False
