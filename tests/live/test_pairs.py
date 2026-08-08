"""The static tier's predictions, checked against what PostgreSQL does.

Two claims are under test and they are different claims.

The first is that the static tier *separates* each pair. That is checkable
without a database, because it is a claim about our own rules, and those tests
run everywhere.

The second is that it separates them *correctly* -- that the migration we call
a rewrite is the one PostgreSQL rewrites. That needs a real server, and it is
the test that would catch the rules going stale against a new major version.
Without it the first claim is only self-consistency: a rule that had narrowing
and widening backwards would separate the pair just as cleanly.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from djaudit.context import ProjectContext
from djaudit.live.context import LiveContext
from djaudit.models import EvidenceKind, Finding
from djaudit.rules.djm_pending_blocking_lock import BlockingPendingMigration

from .pairs import REWRITE, VARIANTS, build_pairs
from .test_sqlmigrate import interpreter

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")

REWRITING = [name for name, (_, rewrites) in VARIANTS.items() if rewrites]
CHEAP = [name for name, (_, rewrites) in VARIANTS.items() if not rewrites]


def static_findings(root: Path) -> list[Finding]:
    """Every finding a static audit produces, at the default confidence floor."""
    from djaudit.engine import run

    return list(run(root).findings)


def migration_findings(root: Path) -> list[Finding]:
    return [f for f in static_findings(root) if f.rule_id.startswith("DJM")]


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """The fixture without a virtualenv, which the static tier never needs."""
    return build_pairs(tmp_path, "postgresql://u@h:1/d", venv=False)


class TestThePairIsAPair:
    """Both halves have to be real, or the separation is trivial."""

    def test_every_variant_is_named_by_its_cost(self) -> None:
        assert REWRITING and CHEAP, VARIANTS

    def test_the_live_pair_uses_a_rewriting_leaf(self) -> None:
        """A live fixture whose leaf were cheap would assert nothing: the rule
        is silent on it for the right reason and for the wrong one alike."""
        assert VARIANTS[REWRITE][1] is True

    @pytest.mark.parametrize("variant", sorted(VARIANTS))
    def test_each_variant_builds_a_readable_project(self, tmp_path: Path, variant: str) -> None:
        root = build_pairs(tmp_path / variant, "postgresql://u@h:1/d", variant, venv=False)
        assert (root / "shop" / "migrations" / "0003_leaf.py").is_file()
        assert (root / "manage.py").is_file()

    def test_the_safe_half_is_present_in_every_variant(self, built: Path) -> None:
        """The `AddField` is the control. If it stopped being written the
        silence about it would still look like a passing test."""
        text = (built / "shop" / "migrations" / "0002_safe_default.py").read_text()
        assert "AddField" in text
        assert "default='web'" in text


class TestTheGeneratedSettingsCanReachTheDatabase:
    """A local cluster on trust auth cannot notice a dropped password.

    CI is where it would be noticed, and the failure there is a connection
    refused with no hint that the fixture -- not the container -- was wrong.
    """

    def settings(self, tmp_path: Path, dsn: str) -> str:
        root = build_pairs(tmp_path, dsn, venv=False)
        return (root / "proj" / "settings.py").read_text()

    def test_it_carries_every_part_of_the_dsn(self, tmp_path: Path) -> None:
        written = self.settings(tmp_path, "postgresql://bob:s3cret@db.example:6543/shopdb")
        assert "'USER': 'bob'" in written
        assert "'PASSWORD': 's3cret'" in written
        assert "'HOST': 'db.example'" in written
        assert "'PORT': '6543'" in written
        assert "'NAME': 'shopdb'" in written

    def test_a_dsn_without_a_password_writes_an_empty_one(self, tmp_path: Path) -> None:
        """The local shape. It has to stay working, so the assertion above
        cannot be satisfied by making a password mandatory."""
        written = self.settings(tmp_path, "postgresql://djaudit@127.0.0.1:55432/app")
        assert "'PASSWORD': ''" in written

    def test_it_percent_decodes_the_way_libpq_does(self, tmp_path: Path) -> None:
        """`urlparse` does not decode and libpq does, so `psql` connects with a
        password Django is handed as a literal. Measured: it fails
        authentication against a scram-sha-256 cluster and nothing on a
        trust-auth one, which is why this went unnoticed until CI was built.
        """
        written = self.settings(tmp_path, "postgresql://c%20i:p%40ss%20word@h:1/my%20db")
        assert "'PASSWORD': 'p@ss word'" in written
        assert "'USER': 'c i'" in written
        assert "'NAME': 'my db'" in written
        assert "%40" not in written

    def test_the_settings_module_is_valid_python(self, tmp_path: Path) -> None:
        """A quoting mistake in a password would produce a file that imports
        nowhere, and every live test would fail as though the server were down.
        """
        import ast

        ast.parse(self.settings(tmp_path, "postgresql://u:it's@h:1/d"))


class TestTheStaticTierSeparatesThem:
    def test_the_rewriting_leaf_is_reported(self, built: Path) -> None:
        found = migration_findings(built)
        assert [f.rule_id for f in found] == ["DJM-002"], found

    def test_and_it_names_the_column_it_read(self, built: Path) -> None:
        """A finding that said only 'this migration rewrites a table' would
        pass the same assertion while telling a reader nothing to act on."""
        message = migration_findings(built)[0].message
        assert "order.reference" in message
        assert "CharField" in message

    def test_it_lands_on_the_leaf_migration(self, built: Path) -> None:
        assert migration_findings(built)[0].location.file.endswith("0003_leaf.py")

    @pytest.mark.parametrize("variant", sorted(CHEAP))
    def test_a_cheap_leaf_is_not_reported(self, tmp_path: Path, variant: str) -> None:
        root = build_pairs(tmp_path / variant, "postgresql://u@h:1/d", variant, venv=False)
        assert migration_findings(root) == []

    @pytest.mark.parametrize("variant", sorted(REWRITING))
    def test_a_rewriting_leaf_is_reported(self, tmp_path: Path, variant: str) -> None:
        root = build_pairs(tmp_path / variant, "postgresql://u@h:1/d", variant, venv=False)
        assert [f.rule_id for f in migration_findings(root)] == ["DJM-002"]

    def test_the_safe_migration_is_judged_not_skipped(self, tmp_path: Path) -> None:
        """The static tier reports only each app's leaf, so the silence about
        `0002` proves nothing while `0003` sits after it. Promoted to leaf, it
        is still silent -- now because the rule read it and had nothing to say.
        """
        root = build_pairs(tmp_path / "leafless", "postgresql://u@h:1/d", venv=False)
        (root / "shop" / "migrations" / "0003_leaf.py").unlink()
        assert migration_findings(root) == []


@postgres
class TestPostgresAgrees:
    """The half that can fail when PostgreSQL changes rather than when we do."""

    STATEMENTS: ClassVar[dict[str, str]] = {
        "narrow": "ALTER TABLE pairs ALTER COLUMN reference TYPE varchar(50) "
        "USING left(reference, 50)",
        "widen": "ALTER TABLE pairs ALTER COLUMN reference TYPE varchar(400)",
        "to_text": "ALTER TABLE pairs ALTER COLUMN reference TYPE text",
        "to_big": "ALTER TABLE pairs ALTER COLUMN qty TYPE bigint",
    }

    def rewrites(self, dsn: str, statement: str) -> bool:
        """Did the heap get rewritten? `relfilenode` is the fact, not the clock.

        A duration threshold would make this a benchmark of the runner, and it
        would pass on an empty table for both halves of every pair.
        """
        node = "SELECT relfilenode FROM pg_class WHERE relname = 'pairs'"
        before = _psql(dsn, node)
        _psql(dsn, statement)
        return _psql(dsn, node) != before

    @pytest.fixture
    def seeded(self, live_pairs: Path) -> str:
        dsn = (live_pairs / "dsn.txt").read_text()
        _psql(
            dsn,
            "CREATE TABLE pairs (id serial primary key, "
            "reference varchar(200) NOT NULL, qty integer NOT NULL DEFAULT 0)",
        )
        _psql(
            dsn,
            "INSERT INTO pairs (reference) SELECT repeat('x', 40) FROM generate_series(1, 50000)",
        )
        return dsn

    @pytest.mark.parametrize("variant", sorted(VARIANTS))
    def test_the_rewrite_column_of_the_table_is_true(self, seeded: str, variant: str) -> None:
        """`VARIANTS` is a claim about PostgreSQL. This is the claim."""
        assert self.rewrites(seeded, self.STATEMENTS[variant]) is VARIANTS[variant][1]

    def test_adding_a_column_with_a_constant_default_does_not_rewrite(self, seeded: str) -> None:
        """The safe half of the live pair, measured rather than assumed. It is
        the one operation people expect to be expensive and it is not."""
        statement = "ALTER TABLE pairs ADD COLUMN channel varchar(20) NOT NULL DEFAULT 'web'"
        assert self.rewrites(seeded, statement) is False

    def test_the_probe_can_see_a_rewrite_at_all(self, seeded: str) -> None:
        """The presence control for four `is False` assertions above: on a
        connection that never rewrites anything they would all hold."""
        assert self.rewrites(seeded, "ALTER TABLE pairs ALTER COLUMN qty TYPE bigint") is True


@postgres
class TestTheLiveTierSeparatesThemToo:
    def context(self, project: Path) -> ProjectContext:
        from djaudit.discovery import build_context

        ctx = build_context(project)
        return replace(
            ctx,
            live=True,
            live_context=LiveContext(
                interpreter=interpreter(project / ".venv" / "bin" / "python"),
                django_version="6.1",
                settings_module="proj.settings",
                databases=(("default", "django.db.backends.postgresql"),),
            ),
        )

    def findings(self, project: Path) -> list[Finding]:
        return list(BlockingPendingMigration().check(self.context(project)))

    def test_it_reports_the_rewrite(self, live_pairs: Path) -> None:
        messages = [f.message for f in self.findings(live_pairs)]
        assert any("TYPE varchar(50)" in m for m in messages), messages

    def test_it_says_nothing_about_the_constant_default(self, live_pairs: Path) -> None:
        """Both migrations are pending and both take an AccessExclusive lock.
        Only one is worth waking anyone for."""
        messages = [f.message for f in self.findings(live_pairs)]
        assert not any("channel" in m for m in messages), messages

    def test_the_evidence_is_the_sql_django_printed(self, live_pairs: Path) -> None:
        """Not a rendering of our own: the point of the tier is that the
        sentence in the finding is the sentence the database was given."""
        sql = [
            e.content
            for f in self.findings(live_pairs)
            for e in f.evidence
            if e.kind is EvidenceKind.SQL
        ]
        assert any("TYPE varchar(50)" in s for s in sql), sql

    def test_both_tiers_name_the_same_migration(self, live_pairs: Path) -> None:
        """The whole point of the pair. An inference and a measurement, taken
        by different means, landing on the same file."""
        live = {Path(f.location.file).name for f in self.findings(live_pairs)}
        static = {Path(f.location.file).name for f in migration_findings(live_pairs)}
        assert static == {"0003_leaf.py"}
        assert live == static

    def test_an_applied_migration_leaves_only_the_static_finding(self, live_pairs: Path) -> None:
        """Where the tiers are *meant* to disagree. Once it has run, its lock
        is not something anyone can act on, so the live rule drops it -- and
        the static rule cannot know, so it goes on reporting the leaf."""
        subprocess.run(
            [str(live_pairs / ".venv" / "bin" / "python"), "manage.py", "migrate"],
            cwd=live_pairs,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        assert self.findings(live_pairs) == []
        assert [f.rule_id for f in migration_findings(live_pairs)] == ["DJM-002"]


def _psql(dsn: str, sql: str) -> str:
    done = subprocess.run(
        ["psql", dsn, "-tAc", sql], capture_output=True, text=True, timeout=300, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()
