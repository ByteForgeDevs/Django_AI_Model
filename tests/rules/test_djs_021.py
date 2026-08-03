"""DJS-021 -- database connections are not reused between requests."""

from __future__ import annotations

import pathlib

from djaudit import engine
from djaudit.discovery import build_context
from djaudit.models import Finding
from djaudit.rules.database import database_configs, live_definitions
from djaudit.settings import resolve_all

MARKERS = "INSTALLED_APPS = []\nSECRET_KEY = 'x'\nDEBUG = False\n"

PG = '"ENGINE": "django.db.backends.postgresql", "NAME": "app"'
LITE = '"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"'


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
    return [f for f in engine.run(root).findings if f.rule_id == "DJS-021"]


def one_db(inner: str) -> str:
    return "DATABASES = {'default': {" + inner + "}}\n"


def test_postgres_without_conn_max_age_is_reported(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, one_db(PG)))
    assert len(found) == 1
    assert found[0].severity.value == "low"
    assert "never sets CONN_MAX_AGE" in found[0].message


def test_an_explicit_zero_is_reported(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, one_db(PG + ', "CONN_MAX_AGE": 0')))
    assert len(found) == 1
    assert "sets CONN_MAX_AGE to 0" in found[0].message


def test_a_reused_connection_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, one_db(PG + ', "CONN_MAX_AGE": 60'))) == []


def test_unlimited_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, one_db(PG + ', "CONN_MAX_AGE": None'))) == []


def test_sqlite_stays_silent(tmp_path: pathlib.Path) -> None:
    # Opening a file is not a handshake, and Django's own documentation warns
    # that persistent connections on SQLite cause locking problems.
    assert findings(build(tmp_path, one_db(LITE))) == []


def test_a_connection_pool_stays_silent(tmp_path: pathlib.Path) -> None:
    # Django refuses to start with both a pool and a non-zero CONN_MAX_AGE, so
    # a pool is a deliberate answer to this question rather than an oversight.
    body = one_db(PG + ', "OPTIONS": {"pool": True}')
    assert findings(build(tmp_path, body)) == []


def test_an_unreadable_engine_stays_silent(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, "DATABASES = load_databases()\n")) == []


def test_each_alias_is_judged_separately(tmp_path: pathlib.Path) -> None:
    body = (
        "DATABASES = {\n"
        "    'default': {" + PG + ", 'CONN_MAX_AGE': 60},\n"
        "    'replica': {" + PG + "},\n"
        "}\n"
    )
    found = findings(build(tmp_path, body))
    assert len(found) == 1
    assert "'replica'" in found[0].message


def test_one_branch_that_reuses_connections_settles_it(tmp_path: pathlib.Path) -> None:
    # Healthchecks is the shape this defends: DATABASES written three times,
    # once plainly and twice inside ifs. A project that gets it right on the
    # branch that will actually run has not made a mistake.
    body = (
        "import os\n" + one_db(PG) + "if os.environ.get('DB') == 'postgres':\n"
        "    DATABASES = {'default': {" + PG + ", 'CONN_MAX_AGE': 60}}\n"
    )
    assert findings(build(tmp_path, body)) == []


def test_a_branch_with_an_unreadable_engine_suppresses_the_finding(
    tmp_path: pathlib.Path,
) -> None:
    body = (
        "import os\n" + one_db(PG) + "if os.environ.get('DB'):\n"
        "    DATABASES = {'default': {'ENGINE': os.environ['ENGINE']}}\n"
    )
    assert findings(build(tmp_path, body)) == []


def test_the_finding_points_at_the_non_sqlite_branch(tmp_path: pathlib.Path) -> None:
    body = (
        "import os\n" + one_db(LITE) + "if os.environ.get('DB') == 'postgres':\n"
        "    DATABASES = {'default': {" + PG + "}}\n"
    )
    found = findings(build(tmp_path, body))
    assert len(found) == 1
    assert found[0].location.line > 5


def test_the_pooler_caveat_is_always_attached(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, one_db(PG)))
    assert "pgbouncer" in found[0].message


def test_live_definitions_drops_replaced_assignments(tmp_path: pathlib.Path) -> None:
    # A plain reassignment replaces everything before it, so the earlier block
    # is dead code and reporting it would report a value no connection sees.
    body = one_db(LITE) + one_db(PG)
    view = next(iter(resolve_all(build_context(build(tmp_path, body))).values()))
    resolved = view.get("DATABASES")
    assert len(resolved.definitions) == 2
    assert len(live_definitions(resolved)) == 1

    configs = database_configs(view, resolved)
    assert [config.is_sqlite for config in configs["default"]] == [False]


def test_database_configs_keeps_every_live_branch(tmp_path: pathlib.Path) -> None:
    body = (
        "import os\n" + one_db(LITE) + "if os.environ.get('DB'):\n"
        "    DATABASES = {'default': {" + PG + "}}\n"
    )
    view = next(iter(resolve_all(build_context(build(tmp_path, body))).values()))
    configs = database_configs(view, view.get("DATABASES"))["default"]
    assert [config.conditional for config in configs] == [False, True]
    assert [config.is_sqlite for config in configs] == [True, False]
