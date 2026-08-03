"""DJS-027 -- the error-report path sending more than it redacts."""

from __future__ import annotations

import pathlib

from djaudit import engine
from djaudit.models import Confidence, Finding, Severity

MARKERS = "DATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\nINSTALLED_APPS = []\n"

MAIL_ADMINS = """LOGGING = {
    "version": 1,
    "handlers": {
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            %s
        },
    },
}
"""


def build(
    tmp_path: pathlib.Path, settings: str, *, extra: dict[str, str] | None = None
) -> pathlib.Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(MARKERS + settings)
    for name, source in (extra or {}).items():
        (root / name).write_text(source)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    result = engine.run(root, min_confidence=Confidence.TENTATIVE, min_severity=Severity.INFO)
    return [f for f in result.findings if f.rule_id == "DJS-027"]


def test_include_html_is_reported(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, MAIL_ADMINS % '"include_html": True,'))
    assert len(found) == 1
    assert found[0].severity.value == "medium"
    assert found[0].confidence.value == "firm"
    assert "include_html" in found[0].message


def test_the_stock_mail_admins_handler_is_silent(tmp_path: pathlib.Path) -> None:
    # Django ships this arrangement in DEFAULT_LOGGING. A rule that reported it
    # would report every Django project there is.
    assert findings(build(tmp_path, MAIL_ADMINS % "")) == []


def test_include_html_off_is_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, MAIL_ADMINS % '"include_html": False,')) == []


def test_another_handler_class_is_not_this_rule(tmp_path: pathlib.Path) -> None:
    body = MAIL_ADMINS.replace("django.utils.log.AdminEmailHandler", "logging.StreamHandler")
    assert findings(build(tmp_path, body % '"include_html": True,')) == []


def test_the_finding_points_at_the_entry(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, MAIL_ADMINS % '"include_html": True,'))
    assert found[0].location.line == 11
    assert found[0].location.snippet == '"include_html": True,'


def test_a_replaced_reporter_filter_is_reported(tmp_path: pathlib.Path) -> None:
    root = build(
        tmp_path,
        'DEFAULT_EXCEPTION_REPORTER_FILTER = "app.reporting.Terse"\n',
        extra={"app.py": "", "reporting.py": "class Terse:\n    pass\n"},
    )
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "reporting.py").write_text("class Terse:\n    pass\n")
    found = findings(root)
    assert len(found) == 1
    assert found[0].confidence.value == "firm"
    assert "does not extend" in found[0].message


def test_a_subclassed_reporter_filter_is_silent(tmp_path: pathlib.Path) -> None:
    # Almost everyone who sets this setting does so to redact more. Reporting
    # them would punish the people who thought about it hardest.
    root = build(tmp_path, 'DEFAULT_EXCEPTION_REPORTER_FILTER = "app.reporting.Extra"\n')
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "reporting.py").write_text(
        "from django.views.debug import SafeExceptionReporterFilter\n\n\n"
        "class Extra(SafeExceptionReporterFilter):\n    pass\n"
    )
    assert findings(root) == []


def test_djangos_own_filter_is_silent(tmp_path: pathlib.Path) -> None:
    body = 'DEFAULT_EXCEPTION_REPORTER_FILTER = "django.views.debug.SafeExceptionReporterFilter"\n'
    assert findings(build(tmp_path, body)) == []


def test_an_unassigned_filter_is_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, "")) == []


def test_a_filter_outside_the_tree_is_only_tentative(tmp_path: pathlib.Path) -> None:
    # A class in an installed package may perfectly well subclass Django's
    # filter, and we cannot see it to find out.
    root = build(tmp_path, 'DEFAULT_EXCEPTION_REPORTER_FILTER = "vendor.filters.Theirs"\n')
    found = findings(root)
    assert len(found) == 1
    assert found[0].confidence.value == "tentative"


def test_a_custom_reporter_class_on_the_handler_is_reported(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, MAIL_ADMINS % '"reporter_class": "app.reporting.Terse",')
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "reporting.py").write_text("class Terse:\n    pass\n")
    found = findings(root)
    assert len(found) == 1
    assert "reporter_class" in found[0].evidence[1].content


def test_an_unreadable_logging_dict_is_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, "LOGGING = build_logging()\n")) == []


def test_a_development_module_is_not_reported(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "proj"
    (root / "config" / "settings").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings" / "__init__.py").write_text("")
    (root / "config" / "settings" / "base.py").write_text(MARKERS)
    (root / "config" / "settings" / "production.py").write_text(
        "from .base import *  # noqa: F401,F403\n"
    )
    (root / "config" / "settings" / "development.py").write_text(
        "from .base import *  # noqa: F401,F403\n" + MAIL_ADMINS % '"include_html": True,'
    )
    result = engine.run(root, min_confidence=Confidence.TENTATIVE, min_severity=Severity.INFO)
    assert [f for f in result.findings if f.rule_id == "DJS-027"] == []
