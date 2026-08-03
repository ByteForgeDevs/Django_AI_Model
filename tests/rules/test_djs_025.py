"""DJS-025 -- development tooling in the production dependency set."""

from __future__ import annotations

import pathlib

from djaudit import engine
from djaudit.models import Finding

MARKERS = "import os\nDATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"


def build(
    tmp_path: pathlib.Path,
    settings: str,
    *,
    requirements: str = "django-debug-toolbar==7.0.0\n",
    dev_requirements: str | None = None,
) -> pathlib.Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + settings)
    (root / "requirements.txt").write_text(requirements)
    if dev_requirements is not None:
        (root / "requirements-dev.txt").write_text(dev_requirements)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    return [f for f in engine.run(root).findings if f.rule_id == "DJS-025"]


def apps(*names: str) -> str:
    return "INSTALLED_APPS = " + repr(["django.contrib.auth", *names]) + "\n"


def test_a_deployed_uninstalled_package_is_informational(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, apps()))
    assert len(found) == 1
    assert found[0].severity.value == "info"
    assert found[0].location.file == "requirements.txt"
    assert found[0].location.line == 1


def test_a_conditional_install_is_the_louder_case(tmp_path: pathlib.Path) -> None:
    # This is the shape the rule exists for: correct today, and one environment
    # variable away from serving a SQL panel.
    body = apps() + "if os.environ.get('DEBUG_TOOLBAR'):\n    INSTALLED_APPS += ['debug_toolbar']\n"
    found = findings(build(tmp_path, body))
    assert len(found) == 1
    assert found[0].severity.value == "low"
    assert "some paths" in found[0].message


def test_the_netbox_shape_is_the_conditional_case(tmp_path: pathlib.Path) -> None:
    body = apps("debug_toolbar") + "if not DEBUG:\n    INSTALLED_APPS.remove('debug_toolbar')\n"
    found = findings(build(tmp_path, body))
    assert len(found) == 1
    assert found[0].severity.value == "low"


def test_an_installed_package_is_left_to_djs_024(tmp_path: pathlib.Path) -> None:
    # Saying it twice, once loudly and once quietly, teaches the reader to skim.
    root = build(tmp_path, apps("debug_toolbar"))
    reported = {f.rule_id for f in engine.run(root).findings}
    assert "DJS-024" in reported
    assert "DJS-025" not in reported


def test_a_development_requirements_file_is_not_deployment(tmp_path: pathlib.Path) -> None:
    root = build(
        tmp_path,
        apps(),
        requirements="django==6.0.7\n",
        dev_requirements="django-debug-toolbar==7.0.0\n",
    )
    assert findings(root) == []


def test_a_package_nobody_declares_is_not_reported(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, apps(), requirements="django==6.0.7\n")) == []


def test_the_unreadable_case_survives_at_tentative(tmp_path: pathlib.Path) -> None:
    from djaudit.models import Confidence, Severity

    root = build(tmp_path, "INSTALLED_APPS = load_apps()\n")
    result = engine.run(root, min_confidence=Confidence.TENTATIVE, min_severity=Severity.INFO)
    found = [f for f in result.findings if f.rule_id == "DJS-025"]
    assert len(found) == 1
    assert found[0].confidence.value == "tentative"
    assert "could not be read" in found[0].message


def test_a_project_with_no_manifest_is_silent(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, apps())
    (root / "requirements.txt").unlink()
    assert findings(root) == []


def test_the_finding_carries_the_distribution(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, apps()))[0]
    assert found.properties["distribution"] == "django-debug-toolbar"
    assert found.properties["installed"] == "never"
    assert found.location.snippet == "django-debug-toolbar==7.0.0"


def test_the_name_is_matched_across_spellings(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, apps(), requirements="Django_Debug_Toolbar == 7.0.0\n")
    assert len(findings(root)) == 1


def test_a_development_settings_module_does_not_count_as_installed(
    tmp_path: pathlib.Path,
) -> None:
    # Installing the toolbar in development is right, and the package still
    # reaches production, so the manifest finding must survive it.
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
        "from .base import *  # noqa: F401,F403\nINSTALLED_APPS += ['debug_toolbar']\n"
    )
    (root / "requirements.txt").write_text("django-debug-toolbar==7.0.0\n")
    found = [f for f in engine.run(root).findings if f.rule_id == "DJS-025"]
    assert len(found) == 1
    assert found[0].severity.value == "info"
