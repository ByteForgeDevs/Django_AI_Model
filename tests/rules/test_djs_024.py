"""DJS-024 -- development tooling installed in a production settings module."""

from __future__ import annotations

import pathlib

import pytest

from djaudit import engine
from djaudit.models import Finding
from djaudit.rules.exposure import DEBUG_APPS, app_key

MARKERS = "DATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"


def build(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + body)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    return [f for f in engine.run(root).findings if f.rule_id == "DJS-024"]


def apps(*names: str) -> str:
    return "INSTALLED_APPS = " + repr(["django.contrib.auth", *names]) + "\n"


def test_app_key_takes_the_package() -> None:
    assert app_key("debug_toolbar.apps.DebugToolbarConfig") == "debug_toolbar"
    assert app_key("silk") == "silk"
    assert app_key("") is None
    assert app_key(3) is None


@pytest.mark.parametrize("key", sorted(DEBUG_APPS))
def test_every_package_is_recognised(tmp_path: pathlib.Path, key: str) -> None:
    found = findings(build(tmp_path / key, apps(key)))
    assert len(found) == 1
    assert found[0].severity == DEBUG_APPS[key].severity


def test_an_app_config_path_is_the_same_package(tmp_path: pathlib.Path) -> None:
    # A project's choice of spelling must not switch the rule off.
    body = apps("debug_toolbar.apps.DebugToolbarConfig")
    assert len(findings(build(tmp_path, body))) == 1


def test_silk_is_graded_above_debug_toolbar(tmp_path: pathlib.Path) -> None:
    # Silk has no DEBUG gate at all and its UI is unauthenticated by default;
    # the toolbar's own callback returns False whenever DEBUG is off.
    silk = findings(build(tmp_path / "s", apps("silk")))[0]
    toolbar = findings(build(tmp_path / "t", apps("debug_toolbar")))[0]
    assert silk.severity.value == "high"
    assert toolbar.severity.value == "medium"


def test_an_overridden_toolbar_callback_escalates(tmp_path: pathlib.Path) -> None:
    # Overriding SHOW_TOOLBAR_CALLBACK removes the only thing keeping the
    # toolbar off in production.
    body = apps("debug_toolbar") + (
        "DEBUG_TOOLBAR_CONFIG = {'SHOW_TOOLBAR_CALLBACK': lambda r: True}\n"
    )
    found = findings(build(tmp_path, body))
    assert found[0].severity.value == "high"
    assert "SHOW_TOOLBAR_CALLBACK" in found[0].message


def test_other_toolbar_config_does_not_escalate(tmp_path: pathlib.Path) -> None:
    body = apps("debug_toolbar") + "DEBUG_TOOLBAR_CONFIG = {'RESULTS_CACHE_SIZE': 3}\n"
    assert findings(build(tmp_path, body))[0].severity.value == "medium"


def test_silk_behind_its_own_auth_is_downgraded(tmp_path: pathlib.Path) -> None:
    body = apps("silk") + "SILKY_AUTHENTICATION = True\nSILKY_AUTHORISATION = True\n"
    found = findings(build(tmp_path, body))
    assert found[0].severity.value == "low"


def test_half_configured_silk_is_not_downgraded(tmp_path: pathlib.Path) -> None:
    body = apps("silk") + "SILKY_AUTHENTICATION = True\n"
    assert findings(build(tmp_path, body))[0].severity.value == "high"


def test_an_ordinary_app_list_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, apps("rest_framework", "myapp"))) == []


def test_a_branch_that_removes_the_app_settles_it(tmp_path: pathlib.Path) -> None:
    # NetBox's exact shape, and the reason the rule is written this way round.
    body = apps("debug_toolbar") + ("if not DEBUG:\n    INSTALLED_APPS.remove('debug_toolbar')\n")
    assert findings(build(tmp_path, body)) == []


def test_a_conditionally_added_app_stays_silent(tmp_path: pathlib.Path) -> None:
    body = (
        "import os\n" + apps() + ("if os.environ.get('DEV'):\n    INSTALLED_APPS.append('silk')\n")
    )
    assert findings(build(tmp_path, body)) == []


def test_an_unreadable_app_list_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, "INSTALLED_APPS = load_apps()\n")) == []


def test_each_package_is_its_own_finding(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, apps("silk", "django_extensions")))
    assert len(found) == 2
    assert {f.severity.value for f in found} == {"high", "low"}


def test_the_finding_points_at_the_entry(tmp_path: pathlib.Path) -> None:
    body = "INSTALLED_APPS = [\n    'django.contrib.auth',\n    'silk',\n]\n"
    found = findings(build(tmp_path, body))
    assert found[0].location.line == 6


def test_the_urlconf_caveat_is_attached(tmp_path: pathlib.Path) -> None:
    assert "urlconf" in findings(build(tmp_path, apps("silk")))[0].message


def test_a_development_module_is_not_reported(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "proj"
    (root / "config" / "settings").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings" / "__init__.py").write_text("")
    (root / "config" / "settings" / "base.py").write_text(MARKERS + apps())
    (root / "config" / "settings" / "production.py").write_text(
        "from .base import *  # noqa: F401,F403\n"
    )
    (root / "config" / "settings" / "development.py").write_text(
        "from .base import *  # noqa: F401,F403\nINSTALLED_APPS = " + repr(["silk"]) + "\n"
    )
    assert [f for f in engine.run(root).findings if f.rule_id == "DJS-024"] == []
