"""DJS-023 -- a per-alias database key set where Django cannot see it."""

from __future__ import annotations

import pathlib

import pytest

from djaudit import engine
from djaudit.models import Finding
from djaudit.rules.database import PER_ALIAS_KEYS

MARKERS = "INSTALLED_APPS = []\nSECRET_KEY = 'x'\nDEBUG = False\n"
PG = '"ENGINE": "django.db.backends.postgresql", "HOST": ""'


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
    return [f for f in engine.run(root).findings if f.rule_id == "DJS-023"]


def one_db(inner: str = "") -> str:
    return "DATABASES = {'default': {" + PG + inner + "}}\n"


def test_atomic_requests_at_module_level_is_reported(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, one_db() + "ATOMIC_REQUESTS = True\n"))
    assert len(found) == 1
    assert found[0].severity.value == "medium"
    assert found[0].confidence.value == "certain"
    assert "does nothing" in found[0].message


@pytest.mark.parametrize("key", sorted(PER_ALIAS_KEYS))
def test_every_key_is_covered(tmp_path: pathlib.Path, key: str) -> None:
    found = findings(build(tmp_path / key, one_db() + f"{key} = 60\n"))
    assert len(found) == 1
    assert key in found[0].message


def test_the_same_key_inside_the_alias_is_correct(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, one_db(', "ATOMIC_REQUESTS": True'))) == []


def test_a_constant_referenced_from_the_alias_is_not_a_defect(
    tmp_path: pathlib.Path,
) -> None:
    # Naming the value and then using it is a perfectly good way to write this,
    # and it is indistinguishable from the mistake until you look for the use.
    body = "import os\nCONN_MAX_AGE = int(os.environ.get('CONN_MAX_AGE', '60'))\n" + one_db(
        ', "CONN_MAX_AGE": CONN_MAX_AGE'
    )
    assert findings(build(tmp_path, body)) == []


def test_a_reference_from_another_module_still_counts(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "proj"
    (root / "config" / "settings").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')\n"
    )
    (root / "config" / "__init__.py").write_text("")
    (root / "config" / "settings" / "__init__.py").write_text("")
    (root / "config" / "settings" / "base.py").write_text(MARKERS + "CONN_MAX_AGE = 60\n")
    (root / "config" / "settings" / "production.py").write_text(
        "from .base import *  # noqa: F401,F403\n" + one_db(', "CONN_MAX_AGE": CONN_MAX_AGE')
    )
    assert [f for f in engine.run(root).findings if f.rule_id == "DJS-023"] == []


def test_an_unrelated_setting_is_not_selected(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, one_db() + "SESSION_COOKIE_AGE = 60\n")) == []


def test_engine_and_name_are_deliberately_not_covered(tmp_path: pathlib.Path) -> None:
    # Nobody writes ENGINE at module level believing Django reads it, and the
    # names are far too common as ordinary helper constants.
    body = one_db() + "ENGINE = 'x'\nNAME = 'y'\nPASSWORD = 'z'\n"
    assert findings(build(tmp_path, body)) == []


def test_the_caveat_is_attached(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, one_db() + "ATOMIC_REQUESTS = True\n"))
    assert "for its own purposes" in found[0].message


def test_a_project_without_databases_is_still_reported(tmp_path: pathlib.Path) -> None:
    # No DATABASES to reference the name from, so the assignment is still inert.
    found = findings(build(tmp_path, "DATABASES = {}\nATOMIC_REQUESTS = True\n"))
    assert len(found) == 1


def test_severity_tracks_what_the_reader_would_believe(tmp_path: pathlib.Path) -> None:
    atomic = findings(build(tmp_path / "a", one_db() + "ATOMIC_REQUESTS = True\n"))
    conn = findings(build(tmp_path / "b", one_db() + "CONN_MAX_AGE = 60\n"))
    assert atomic[0].severity.value == "medium"
    assert conn[0].severity.value == "low"
