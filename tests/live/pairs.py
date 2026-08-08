"""Migrations that look alike and cost differently, in pairs.

Every `DJM` rule in the static tier is an *inference*. It reads the operations
an author wrote and predicts what PostgreSQL will do with them, from a table of
beliefs about which field changes rewrite a table. Beliefs go stale. A new
major version relaxes a conversion, someone adds a field type, and the rule
goes on predicting confidently from a table nobody rechecked.

So the fixture is built in pairs: two migrations whose operations look equally
alarming, where the cost is not the same. If the static tier ever stops
separating a pair, it has stopped reading the difference that matters rather
than merely losing a test.

**Measured on PostgreSQL 18.1, 2,000,000 rows, a 161 MB table.** Whether a
statement rewrote the table is not inferred from how long it took: it is read
from `pg_class.relfilenode`, which changes when and only when the heap is
rewritten.

| operation | duration | relfilenode |
|---|---|---|
| `ADD COLUMN varchar NOT NULL DEFAULT 'web'` | 3.2 ms | unchanged |
| `ALTER COLUMN TYPE varchar(400)` (widening) | 3.1 ms | unchanged |
| `ALTER COLUMN TYPE text` | 3.9 ms | unchanged |
| `ALTER COLUMN TYPE bigint` | 3,287 ms | **changed** |
| `ALTER COLUMN TYPE varchar(50)` (narrowing) | 4,447 ms | **changed** |

Every row of that table takes the strongest lock PostgreSQL has, and the lock
is not the story. Three of the five hold it for three milliseconds, because
they only write a catalogue row. A rule that reported on lock mode alone would
report all five identically, and a reader who is told that widening a
`CharField` will take their site down learns to ignore the tool. That is the
argument for the second axis, and this is where it is checked.

`AddField` is a measurement in its own right. **Django resolves a callable
default before it writes the SQL**: `default=uuid.uuid4` is emitted as
`DEFAULT '93f945b8...'::uuid`, a literal, not a function call. So an `AddField`
never emits a volatile default however the model was written -- which is why
the volatility patterns in `live/locks.py` are aimed at hand-written `RunSQL`,
and why they would be dead code if they were aimed here.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

SETTINGS = """\
SECRET_KEY = 'x'
INSTALLED_APPS = ['shop']
DATABASES = {{'default': {{
    'ENGINE': 'django.db.backends.postgresql',
    'NAME': {name!r},
    'USER': {user!r},
    'HOST': {host!r},
    'PORT': {port!r},
}}}}
USE_TZ = True
"""

MANAGE = """\
#!/usr/bin/env python
import os
import sys

if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proj.settings')
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
"""

MODELS = """\
from django.db import models


class Order(models.Model):
    reference = models.CharField(max_length=200)
"""

INITIAL = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name='Order',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('reference', models.CharField(max_length=200)),
            ],
        )
    ]
"""

SAFE = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shop', '0001_initial')]
    operations = [
        migrations.AddField(
            'order',
            'channel',
            models.CharField(max_length=20, default='web'),
        )
    ]
"""

LEAF = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shop', '0002_safe_default')]
    operations = [
{operations}
    ]
"""

#: name -> (operations, does PostgreSQL rewrite the table)
VARIANTS: dict[str, tuple[str, bool]] = {
    "narrow": (
        "        migrations.AlterField(\n"
        "            'order', 'reference', models.CharField(max_length=50)),",
        True,
    ),
    "widen": (
        "        migrations.AlterField(\n"
        "            'order', 'reference', models.CharField(max_length=400)),",
        False,
    ),
    "to_text": (
        "        migrations.AlterField('order', 'reference', models.TextField()),",
        False,
    ),
    "to_big": (
        "        migrations.AddField('order', 'qty', models.IntegerField(default=0)),\n"
        "        migrations.AlterField(\n"
        "            'order', 'qty', models.BigIntegerField(default=0)),",
        True,
    ),
}

REWRITE = "narrow"
"""The variant the live pair uses. Named so a test cannot drift from it."""


def build_pairs(root: Path, dsn: str, variant: str = REWRITE, *, venv: bool = True) -> Path:
    """A project whose pending migrations differ only in what they cost."""
    import shutil
    import subprocess

    parsed = urlparse(dsn)
    (root / "proj").mkdir(parents=True, exist_ok=True)
    (root / "shop" / "migrations").mkdir(parents=True, exist_ok=True)
    (root / "proj" / "__init__.py").write_text("")
    (root / "proj" / "settings.py").write_text(
        SETTINGS.format(
            name=(parsed.path or "/postgres")[1:],
            user=parsed.username or "",
            host=parsed.hostname or "",
            port=str(parsed.port or ""),
        )
    )
    (root / "shop" / "__init__.py").write_text("")
    (root / "shop" / "models.py").write_text(MODELS)
    (root / "shop" / "migrations" / "__init__.py").write_text("")
    (root / "shop" / "migrations" / "0001_initial.py").write_text(INITIAL)
    (root / "shop" / "migrations" / "0002_safe_default.py").write_text(SAFE)
    (root / "shop" / "migrations" / "0003_leaf.py").write_text(
        LEAF.format(operations=VARIANTS[variant][0])
    )
    (root / "manage.py").write_text(MANAGE)

    if venv:
        target = root / ".venv"
        uv = shutil.which("uv")
        assert uv is not None, "uv is required to build the target's virtualenv"
        subprocess.run([uv, "venv", str(target), "-q"], check=True, timeout=300)
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "-q",
                "--python",
                str(target / "bin" / "python"),
                "django",
                "psycopg",
            ],
            check=True,
            timeout=900,
        )
    return root
