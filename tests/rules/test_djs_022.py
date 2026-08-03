"""DJS-022 -- a Postgres connection that permits an unencrypted session."""

from __future__ import annotations

import pathlib

import pytest

from djaudit import engine
from djaudit.models import Finding
from djaudit.rules.database import definitely_local
from djaudit.settings import Entry
from djaudit.values import Value

MARKERS = "INSTALLED_APPS = []\nSECRET_KEY = 'x'\nDEBUG = False\n"
PG = '"ENGINE": "django.db.backends.postgresql", "HOST": "db.example"'


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
    return [f for f in engine.run(root).findings if f.rule_id == "DJS-022"]


def one_db(inner: str) -> str:
    return "DATABASES = {'default': {" + inner + "}}\n"


def test_no_options_at_all_is_reported(tmp_path: pathlib.Path) -> None:
    found = findings(build(tmp_path, one_db(PG)))
    assert len(found) == 1
    assert found[0].severity.value == "medium"
    assert "never sets sslmode" in found[0].message


@pytest.mark.parametrize("mode", ["prefer", "allow"])
def test_a_downgradable_mode_is_reported(tmp_path: pathlib.Path, mode: str) -> None:
    body = one_db(PG + f', "OPTIONS": {{"sslmode": "{mode}"}}')
    found = findings(build(tmp_path / mode, body))
    assert len(found) == 1
    assert found[0].severity.value == "medium"


def test_disable_is_graded_higher(tmp_path: pathlib.Path) -> None:
    body = one_db(PG + ', "OPTIONS": {"sslmode": "disable"}')
    found = findings(build(tmp_path, body))
    assert found[0].severity.value == "high"


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_a_protected_mode_stays_silent(tmp_path: pathlib.Path, mode: str) -> None:
    body = one_db(PG + f', "OPTIONS": {{"sslmode": "{mode}"}}')
    assert findings(build(tmp_path / mode, body)) == []


def test_a_non_postgres_engine_stays_silent(tmp_path: pathlib.Path) -> None:
    body = one_db('"ENGINE": "django.db.backends.mysql", "HOST": "db.example"')
    assert findings(build(tmp_path, body)) == []


def test_postgis_is_still_postgres(tmp_path: pathlib.Path) -> None:
    # Same libpq connection, same OPTIONS. A rule that only recognised the
    # stock backend would go quiet on the projects most likely to be deployed.
    body = one_db('"ENGINE": "django.contrib.gis.db.backends.postgis", "HOST": "db.x"')
    assert len(findings(build(tmp_path, body))) == 1


def test_an_absent_host_is_a_unix_socket(tmp_path: pathlib.Path) -> None:
    assert findings(build(tmp_path, one_db('"ENGINE": "django.db.backends.postgresql"'))) == []


@pytest.mark.parametrize("host", ["", "localhost", "127.0.0.1", "/var/run/postgresql"])
def test_a_local_host_stays_silent(tmp_path: pathlib.Path, host: str) -> None:
    body = one_db(f'"ENGINE": "django.db.backends.postgresql", "HOST": "{host}"')
    assert findings(build(tmp_path / host.replace("/", "_"), body)) == []


def test_an_env_dependent_host_is_still_reported(tmp_path: pathlib.Path) -> None:
    # The literal we can see is the fallback; the deployment that matters is
    # the one that sets the variable, and it is not a Unix socket.
    body = (
        "import os\n"
        'DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", '
        '"HOST": os.environ.get("DB_HOST", "")}}\n'
    )
    found = findings(build(tmp_path, body))
    assert len(found) == 1
    assert found[0].confidence.value != "certain"


def test_unreadable_options_stay_silent(tmp_path: pathlib.Path) -> None:
    # The sslmode could be in there and we would never know, so silence is the
    # only honest answer.
    body = one_db(PG + ', "OPTIONS": build_options()')
    assert findings(build(tmp_path, body)) == []


def test_an_env_dependent_sslmode_is_reported_from_its_fallback(
    tmp_path: pathlib.Path,
) -> None:
    body = (
        "import os\n"
        'DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", '
        '"HOST": "db.example", '
        '"OPTIONS": {"sslmode": os.environ.get("DB_SSLMODE", "prefer")}}}\n'
    )
    found = findings(build(tmp_path, body))
    assert len(found) == 1
    assert found[0].confidence.value == "tentative"


def test_one_unsafe_branch_is_enough(tmp_path: pathlib.Path) -> None:
    # The inverse of DJS-021. That rule asks whether anyone thought about a
    # setting, so any branch settles it; this one asks whether a deployment
    # exists that talks to the database in the clear, and a second branch
    # doing it properly does not un-expose the first.
    body = (
        "import os\n"
        + one_db(PG + ', "OPTIONS": {"sslmode": "verify-full"}')
        + "if os.environ.get('LEGACY'):\n"
        "    DATABASES = {'default': {" + PG + "}}\n"
    )
    assert len(findings(build(tmp_path, body))) == 1


def test_only_one_finding_per_alias(tmp_path: pathlib.Path) -> None:
    body = "import os\n" + one_db(PG) + "if os.environ.get('X'):\n    " + one_db(PG)
    assert len(findings(build(tmp_path, body))) == 1


def test_each_alias_is_reported_separately(tmp_path: pathlib.Path) -> None:
    body = (
        "DATABASES = {\n"
        "    'default': {" + PG + ", 'OPTIONS': {'sslmode': 'require'}},\n"
        "    'analytics': {" + PG + "},\n"
        "}\n"
    )
    found = findings(build(tmp_path, body))
    assert len(found) == 1
    assert "'analytics'" in found[0].message


def test_the_libpq_environment_caveat_is_attached(tmp_path: pathlib.Path) -> None:
    assert "PGSSLMODE" in findings(build(tmp_path, one_db(PG)))[0].message


def test_definitely_local_ignores_an_env_dependent_value() -> None:
    assert definitely_local(None) is True
    assert definitely_local(Entry(key="HOST", value=Value.of(""))) is True
    assert definitely_local(Entry(key="HOST", value=Value.of("db.example"))) is False
    tainted = Entry(key="HOST", value=Value.of("", env_dependent=True))
    assert definitely_local(tainted) is False
