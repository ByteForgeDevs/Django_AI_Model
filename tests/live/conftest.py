"""A real Django project, with its own virtualenv and its own database.

Both live test modules need one, and they need it *isolated*. The first version
pointed every project at the same database, and `django_migrations` is a table:
one module's `migrate` decided what another module read back as pending. The
symptom was a test that passed alone and failed in a suite, which is the worst
shape a test can fail in.

So each project gets a database of its own, created and dropped around it.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")


def _admin(sql: str) -> subprocess.CompletedProcess[str]:
    """Run one statement against the DSN's own database.

    `CREATE DATABASE` cannot run inside a transaction, and `psql -c` does not
    wrap a single statement in one, so this is the simple route.
    """
    return subprocess.run(
        ["psql", DSN, "-tAc", sql],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _isolated(tmp_path: Path, build: Callable[[Path, str], Path]) -> Iterator[Path]:
    """Build a project against a database nothing else touches."""
    name = f"djaudit_{uuid.uuid4().hex[:12]}"
    created = _admin(f'CREATE DATABASE "{name}"')
    assert created.returncode == 0, created.stderr

    parsed = urlparse(DSN)
    dsn = urlunparse(parsed._replace(path=f"/{name}"))
    try:
        project = build(tmp_path, dsn)
        # Recorded so a test can reach the same database directly. Rebuilding
        # it from settings.py would mean parsing our own fixture back out of
        # the file it wrote, and the two would drift.
        (project / "dsn.txt").write_text(dsn)
        yield project
    finally:
        # Django's connection is gone with the subprocess, but a failed test
        # can leave one; the drop is forced so teardown cannot itself fail.
        _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def live_project(tmp_path: Path) -> Iterator[Path]:
    """A Django project wired to a database nothing else touches."""
    from .test_sqlmigrate import build_project

    yield from _isolated(tmp_path, build_project)


@pytest.fixture
def live_pairs(tmp_path: Path) -> Iterator[Path]:
    """A project whose pending migrations differ only in what they cost."""
    from .pairs import build_pairs

    yield from _isolated(tmp_path, build_pairs)
