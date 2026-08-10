"""A project that can reach SQLite and PostgreSQL at once, and the same code.

Every `DJX` rule is a claim that *two engines disagree*. Nothing in this suite
has ever made them disagree. The static tests prove we report the construct;
the corpus triage proves the construct is really in Healthchecks; neither
touches a database, so the belief in the middle -- that SQLite and PostgreSQL
answer the same Django expression differently -- has been carried on reading
alone since the family was written.

This is `pairs.py`'s reasoning applied to portability. There, a `DJM` rule
predicts what PostgreSQL will do with a migration, and `TestPostgresAgrees`
makes PostgreSQL do it. Here a `DJX` rule predicts that two engines part
company, so the fixture runs both and reads the answers back.

**The models are Healthchecks', not invented.** Healthchecks is the corpus
project that genuinely ships both engines -- `hc/settings.py` defaults to
SQLite at 209 and offers PostgreSQL at 223 -- and it is the target this step
exists to validate against, so the columns here are the columns its flagged
lines actually read:

| Healthchecks | declaration | flagged at |
|---|---|---|
| `Project.api_key` | `CharField(max_length=128, blank=True)` | `accounts/models.py:411` |
| `Project.api_key_readonly` | `CharField(max_length=128, blank=True)` | `accounts/models.py:417` |
| `Check.tags` | `CharField(max_length=500, blank=True)` | `api/views.py:750` |
| `Check` + `select_for_update()` | -- | `api/models.py:510`, `api/views.py:515` |

The expressions the probe runs are Healthchecks' own, transcribed from those
lines. A fixture that asked a *similar* question would be evidence about the
fixture.

Both databases are reached from one settings module through two aliases, so
the divergence cannot be an artifact of two differently-configured projects.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

#: Transcribed from `hc/accounts/models.py` and `hc/api/models.py`.
MODELS = """from django.db import models


class Project(models.Model):
    api_key = models.CharField(max_length=128, blank=True)
    api_key_readonly = models.CharField(max_length=128, blank=True)


class Check(models.Model):
    code = models.UUIDField(null=True, editable=False)
    tags = models.CharField(max_length=500, blank=True)
"""

PROBE = r'''"""Run one named expression against one database alias, and report.

Both halves of every pair run in this same process against the same rows, so a
difference in the answers is a difference between the engines and not between
two runs.
"""

import json
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")
django.setup()

from django.db import connections, transaction  # noqa: E402

from hc.models import Check, Project  # noqa: E402

ALIAS = sys.argv[1]


def seed():
    Project.objects.using(ALIAS).all().delete()
    Check.objects.using(ALIAS).all().delete()
    Project.objects.using(ALIAS).create(api_key="ABCDEFGHij", api_key_readonly="ABCDEFGHij")
    Check.objects.using(ALIAS).create(tags="PROD staging")


def statement(queryset):
    """The SQL Django would send, without sending it."""
    return str(queryset.query)


def emitted_for_update():
    """`select_for_update` compiles only inside a transaction, so open one."""
    with transaction.atomic(using=ALIAS):
        query = Check.objects.using(ALIAS).select_for_update().filter(code=None)
        compiler = query.query.get_compiler(ALIAS)
        return compiler.as_sql()[0]


seed()
answers = {
    # hc/accounts/models.py:411 -- the key is stored mixed-case, the search is
    # for the lower-case spelling of the same eight characters.
    "startswith_other_case": Project.objects.using(ALIAS)
    .filter(api_key__startswith="abcdefgh")
    .count(),
    # hc/accounts/models.py:417, the read-only twin.
    "readonly_startswith_other_case": Project.objects.using(ALIAS)
    .filter(api_key_readonly__startswith="abcdefgh")
    .count(),
    # hc/api/views.py:750 -- a badge for tag `prod` against a check tagged `PROD`.
    "contains_other_case": Check.objects.using(ALIAS).filter(tags__contains="prod").count(),
    # The controls: same shape, same rows, spelled as stored.
    "startswith_same_case": Project.objects.using(ALIAS)
    .filter(api_key__startswith="ABCDEFGH")
    .count(),
    "contains_same_case": Check.objects.using(ALIAS).filter(tags__contains="PROD").count(),
    # hc/api/models.py:510 and hc/api/views.py:515.
    "for_update_sql": emitted_for_update(),
    "vendor": connections[ALIAS].vendor,
}
print(json.dumps(answers))
'''


def build_divergence(root: Path, dsn: str) -> Path:
    """A project reaching PostgreSQL at `default` and SQLite at `sqlite`."""
    parsed = urlparse(dsn)
    (root / "proj").mkdir(parents=True)
    (root / "hc" / "migrations").mkdir(parents=True)
    (root / "proj" / "__init__.py").write_text("")
    (root / "proj" / "settings.py").write_text(
        "SECRET_KEY = 'x'\n"
        "INSTALLED_APPS = ['hc']\n"
        "_PG = {'ENGINE': 'django.db.backends.postgresql',\n"
        # libpq percent-decodes a URI and `urlparse` does not. See `pairs.py`.
        f"    'NAME': {unquote((parsed.path or '/postgres')[1:])!r},\n"
        f"    'USER': {unquote(parsed.username or '')!r},\n"
        f"    'PASSWORD': {unquote(parsed.password or '')!r},\n"
        f"    'HOST': {(parsed.hostname or '')!r},\n"
        f"    'PORT': {str(parsed.port or '')!r}}}\n"
        "_LITE = {'ENGINE': 'django.db.backends.sqlite3',\n"
        f"    'NAME': {str(root / 'dev.sqlite3')!r}}}\n"
        "DATABASES = {'default': _PG, 'sqlite': _LITE}\n"
        "DATABASE_ROUTERS = []\n"
        "USE_TZ = True\n"
    )
    (root / "hc" / "__init__.py").write_text("")
    (root / "hc" / "models.py").write_text(MODELS)
    (root / "hc" / "migrations" / "__init__.py").write_text("")
    (root / "hc" / "migrations" / "0001_initial.py").write_text(
        "from django.db import migrations, models\n\n\n"
        "class Migration(migrations.Migration):\n"
        "    initial = True\n"
        "    dependencies = []\n"
        "    operations = [\n"
        "        migrations.CreateModel(name='Project', fields=[\n"
        "            ('id', models.AutoField(primary_key=True, serialize=False)),\n"
        "            ('api_key', models.CharField(max_length=128, blank=True)),\n"
        "            ('api_key_readonly',\n"
        "                models.CharField(max_length=128, blank=True))]),\n"
        "        migrations.CreateModel(name='Check', fields=[\n"
        "            ('id', models.AutoField(primary_key=True, serialize=False)),\n"
        "            ('code', models.UUIDField(null=True, editable=False)),\n"
        "            ('tags', models.CharField(max_length=500, blank=True))])]\n"
    )
    (root / "probe.py").write_text(PROBE)
    (root / "manage.py").write_text(
        "#!/usr/bin/env python\nimport os, sys\n\n"
        "if __name__ == '__main__':\n"
        "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proj.settings')\n"
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n"
    )

    venv = root / ".venv"
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to build the target's virtualenv"
    subprocess.run([uv, "venv", str(venv), "-q"], check=True, timeout=300)
    subprocess.run(
        [uv, "pip", "install", "-q", "--python", str(venv / "bin" / "python"), "django", "psycopg"],
        check=True,
        timeout=900,
    )
    python = venv / "bin" / "python"
    for alias in ("default", "sqlite"):
        migrated = subprocess.run(
            [str(python), "manage.py", "migrate", "--database", alias, "-v", "0"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert migrated.returncode == 0, f"{alias}: {migrated.stderr}"
    return root


def ask(root: Path, alias: str) -> dict[str, object]:
    """What that engine answers. Raises rather than returning a partial dict."""
    outcome = subprocess.run(
        [str(root / ".venv" / "bin" / "python"), "probe.py", alias],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert outcome.returncode == 0, f"{alias}: {outcome.stderr}"
    parsed: dict[str, object] = json.loads(outcome.stdout)
    return parsed
