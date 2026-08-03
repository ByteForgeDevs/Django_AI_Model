"""DJS-026 -- the admin mounted at the default path."""

from __future__ import annotations

import pathlib

import pytest

from djaudit import engine
from djaudit.models import Confidence, Finding, Severity
from djaudit.rules.exposure import at_default_path, mounts_admin

MARKERS = "DATABASES = {}\nSECRET_KEY = 'x'\nDEBUG = False\n"


def build(tmp_path: pathlib.Path, urls: str, *, apps: tuple[str, ...] = ()) -> pathlib.Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings.py").write_text(
        MARKERS
        + "INSTALLED_APPS = "
        + repr(["django.contrib.admin", *apps])
        + '\nROOT_URLCONF = "config.urls"\n'
    )
    (root / "config" / "urls.py").write_text(urls)
    return root


def findings(root: pathlib.Path) -> list[Finding]:
    result = engine.run(root, min_confidence=Confidence.TENTATIVE, min_severity=Severity.INFO)
    return [f for f in result.findings if f.rule_id == "DJS-026"]


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        ("admin.site.urls", True),
        ("site.urls", True),
        ("admin.site.get_urls()", True),
        ("my_admin_site.urls", True),
        ("staff_admin.site.urls", True),
        ("include('app.urls')", False),
        ("views.index", False),
        ("api.urls", False),
    ],
)
def test_admin_mounts_are_recognised(view: str, expected: bool) -> None:
    assert mounts_admin(view) is expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("admin/", True),
        ("admin", True),
        ("/admin/", True),
        ("^admin/", True),
        ("^admin/$", True),
        ("staff-console/", False),
        ("admin/login/", False),
        ("", False),
    ],
)
def test_the_default_path_is_recognised(pattern: str, expected: bool) -> None:
    assert at_default_path(pattern) is expected


def test_a_literal_default_mount_is_reported(tmp_path: pathlib.Path) -> None:
    root = build(
        tmp_path,
        "from django.contrib import admin\nurlpatterns = [path('admin/', admin.site.urls)]\n",
    )
    found = findings(root)
    assert len(found) == 1
    assert found[0].severity.value == "info"
    assert found[0].confidence.value == "firm"
    assert found[0].location.file == "config/urls.py"
    assert found[0].location.line == 2


def test_an_interpolated_prefix_is_only_tentative(tmp_path: pathlib.Path) -> None:
    # Healthchecks' shape: the tail is knowable, the whole is not, and claiming
    # certainty about a prefix we cannot read would be a lie.
    root = build(tmp_path, "urlpatterns = [path(f'{prefix}admin/', admin.site.urls)]\n")
    found = findings(root)
    assert len(found) == 1
    assert found[0].confidence.value == "tentative"
    assert "could not read" in found[0].message


def test_the_regex_spelling_counts(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, "urlpatterns = [re_path(r'^admin/', admin.site.urls)]\n")
    assert len(findings(root)) == 1


def test_a_moved_admin_is_silent(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, "urlpatterns = [path('staff-console-8f21/', admin.site.urls)]\n")
    assert findings(root) == []


def test_a_non_admin_route_is_silent(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, "urlpatterns = [path('admin/', include('app.urls'))]\n")
    assert findings(root) == []


@pytest.mark.parametrize("guard", ["django_otp", "two_factor", "axes", "admin_honeypot"])
def test_a_guard_app_settles_it(tmp_path: pathlib.Path, guard: str) -> None:
    # Each of these answers what the default path costs, which is a better
    # answer than moving the URL. Telling someone who has done the harder thing
    # to also do the easier one is how a tool gets ignored.
    root = build(
        tmp_path / guard,
        "urlpatterns = [path('admin/', admin.site.urls)]\n",
        apps=(guard,),
    )
    assert findings(root) == []


def test_a_missing_urlconf_is_silent(tmp_path: pathlib.Path) -> None:
    root = build(tmp_path, "urlpatterns = [path('admin/', admin.site.urls)]\n")
    (root / "config" / "urls.py").unlink()
    assert findings(root) == []


def test_the_finding_is_hidden_at_the_cli_default(tmp_path: pathlib.Path) -> None:
    # Info severity is below the CLI's default floor on purpose: this is worth
    # knowing and is not worth interrupting anyone for.
    root = build(tmp_path, "urlpatterns = [path('admin/', admin.site.urls)]\n")
    result = engine.run(root, min_severity=Severity.LOW)
    assert [f for f in result.findings if f.rule_id == "DJS-026"] == []


def test_one_finding_per_route_not_per_settings_module(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "proj"
    (root / "config" / "settings").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings" / "__init__.py").write_text("")
    (root / "config" / "settings" / "base.py").write_text(
        MARKERS + 'INSTALLED_APPS = []\nROOT_URLCONF = "config.urls"\n'
    )
    (root / "config" / "settings" / "production.py").write_text(
        "from .base import *  # noqa: F401,F403\n"
    )
    (root / "config" / "urls.py").write_text("urlpatterns = [path('admin/', admin.site.urls)]\n")
    result = engine.run(root, min_confidence=Confidence.TENTATIVE, min_severity=Severity.INFO)
    assert len([f for f in result.findings if f.rule_id == "DJS-026"]) == 1
