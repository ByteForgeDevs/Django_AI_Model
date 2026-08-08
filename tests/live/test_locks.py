"""Verifying the lock table against a real Postgres, not against its author.

`locks.py` is a table of claims about what PostgreSQL does. A table of claims
is worth nothing unless something can contradict it, so when a server is
reachable these tests take each statement, run it, and read `pg_locks` from the
backend that took the lock. When no server is reachable they are skipped, and
the shape checks below still run -- but the classifications themselves are then
unverified, which is why the skip says so.

`DJAUDIT_TEST_POSTGRES` is a libpq DSN. CI sets it to the service container.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator

import pytest

from djaudit.live.locks import RULES, UNKNOWN, VOLATILE, Lock, Verdict, Work, classify, worst

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

OBSERVED = {
    "AccessExclusiveLock": Lock.ACCESS_EXCLUSIVE,
    "ShareLock": Lock.SHARE,
    "ShareUpdateExclusiveLock": Lock.SHARE_UPDATE_EXCLUSIVE,
}

SETUP = """DROP TABLE IF EXISTS t CASCADE;
CREATE TABLE t (id serial primary key, name varchar(200), num int, dead int);
INSERT INTO t (name, num, dead) SELECT 'n'||g, g, g FROM generate_series(1,50) g;
CREATE INDEX i1 ON t (name);
ALTER TABLE t ADD CONSTRAINT ck2 CHECK (num > 0) NOT VALID;"""

# Statement, and the strongest lock it was observed to take on `t`.
MEASURED: tuple[tuple[str, Lock], ...] = (
    ("ALTER TABLE t ADD COLUMN c1 integer", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t ADD COLUMN c2 text NOT NULL DEFAULT 'x'", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t ALTER COLUMN name TYPE varchar(50)", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t ALTER COLUMN name SET NOT NULL", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t ALTER COLUMN name DROP NOT NULL", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t ALTER COLUMN num SET DEFAULT 0", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t DROP COLUMN dead", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t RENAME COLUMN name TO name2", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t RENAME TO t_renamed", Lock.ACCESS_EXCLUSIVE),
    ("CREATE INDEX i9 ON t (num)", Lock.SHARE),
    ("DROP INDEX i1", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t ADD CONSTRAINT ck CHECK (num > 0)", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t ADD CONSTRAINT ck3 CHECK (num > 0) NOT VALID", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t VALIDATE CONSTRAINT ck2", Lock.SHARE_UPDATE_EXCLUSIVE),
    ("ALTER TABLE t ADD CONSTRAINT uq UNIQUE (name)", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t DROP CONSTRAINT ck2", Lock.ACCESS_EXCLUSIVE),
    ("ALTER TABLE t SET (fillfactor = 90)", Lock.SHARE_UPDATE_EXCLUSIVE),
    ("CREATE TABLE t2 (id serial primary key)", Lock.NONE),
)

postgres = pytest.mark.skipif(
    not DSN,
    reason="DJAUDIT_TEST_POSTGRES is unset, so every lock claim here is unverified",
)


def psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", DSN, "-tAc", sql],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def observe(statement: str) -> Lock:
    """Run the statement and read back the lock its own backend holds."""
    psql(SETUP)
    result = psql(
        f"BEGIN;\n{statement};\n"
        "SELECT 'LOCK=' || coalesce(string_agg(DISTINCT mode, '+' ORDER BY mode), 'none') "
        "FROM pg_locks WHERE relation IN "
        "(SELECT oid FROM pg_class WHERE relname IN ('t','t_renamed')) "
        "AND pid = pg_backend_pid();\nROLLBACK;"
    )
    found = re.search(r"LOCK=(\S+)", result.stdout)
    assert found, f"no lock read back for {statement!r}: {result.stderr}"
    modes = [OBSERVED[m] for m in found.group(1).split("+") if m in OBSERVED]
    strength = {Lock.SHARE_UPDATE_EXCLUSIVE: 1, Lock.SHARE: 2, Lock.ACCESS_EXCLUSIVE: 3}
    return max(modes, key=lambda m: strength[m], default=Lock.NONE)


class TestAgainstARealPostgres:
    """The only tests here that can refute the table."""

    @postgres
    @pytest.mark.parametrize(("statement", "expected"), MEASURED, ids=lambda v: str(v)[:44])
    def test_the_classification_matches_what_postgres_does(
        self, statement: str, expected: Lock
    ) -> None:
        assert observe(statement) == expected, "the recorded measurement is stale"
        assert classify(statement).lock == expected, "the classifier disagrees with the server"

    @postgres
    def test_the_observer_can_see_a_difference(self) -> None:
        """The control. Every assertion above is an equality, and an observer
        that returned the same value for everything would satisfy all of them."""
        assert observe("CREATE INDEX i9 ON t (num)") != observe("ALTER TABLE t DROP COLUMN dead")

    @postgres
    def test_concurrently_takes_nothing_that_blocks(self) -> None:
        """Cannot run inside a transaction, so it is watched from outside."""
        psql(SETUP)
        watcher = subprocess.Popen(
            [
                "psql",
                DSN,
                "-tAc",
                "SELECT pg_sleep(0.05); SELECT 'LOCK=' || coalesce(string_agg(mode, '+'), 'none') "
                "FROM pg_locks WHERE relation = 't'::regclass "
                "AND mode IN ('ShareLock', 'AccessExclusiveLock')",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        psql("CREATE INDEX CONCURRENTLY i_conc ON t (num)")
        out, _ = watcher.communicate(timeout=60)
        assert "LOCK=none" in out
        assert classify("CREATE INDEX CONCURRENTLY i_conc ON t (num)").lock is Lock.NONE


class TestTheTwoAxesAreBothNeeded:
    """The design claim, stated as tests. Either axis alone misclassifies a
    statement people actually deploy."""

    def test_the_same_lock_is_not_the_same_danger(self) -> None:
        """Measured at 60ms and 2870ms on two million rows, under one lock."""
        cheap = classify("ALTER TABLE big ADD COLUMN c text NOT NULL DEFAULT 'x'")
        ruinous = classify("ALTER TABLE big ALTER COLUMN name TYPE varchar(50)")
        assert cheap.lock is ruinous.lock is Lock.ACCESS_EXCLUSIVE
        assert not cheap.dangerous
        assert ruinous.dangerous

    def test_the_worst_lock_is_not_the_worst_statement(self) -> None:
        """`CREATE INDEX` never takes ACCESS EXCLUSIVE and is the outage people
        actually have: 1031ms of blocked writes per two million rows."""
        index = classify("CREATE INDEX idx ON big (num)")
        assert index.lock is Lock.SHARE
        assert index.dangerous
        assert index.lock.blocks_writes
        assert not index.lock.blocks_reads

    def test_a_catalogue_change_under_the_strongest_lock_is_not_dangerous(self) -> None:
        verdict = classify("ALTER TABLE big DROP COLUMN num")
        assert verdict.lock is Lock.ACCESS_EXCLUSIVE
        assert verdict.blocking
        assert not verdict.dangerous


class TestTheEscapeHatches:
    """The forms that exist precisely to avoid the lock, and must be recognised
    as such -- otherwise djaudit recommends a fix and then flags the fix."""

    def test_concurrently_is_not_flagged(self) -> None:
        assert not classify("CREATE INDEX CONCURRENTLY idx ON big (num)").blocking

    def test_a_plain_index_build_is(self) -> None:
        assert classify("CREATE INDEX idx ON big (num)").blocking

    def test_not_valid_is_not_flagged(self) -> None:
        verdict = classify("ALTER TABLE big ADD CONSTRAINT ck CHECK (num > 0) NOT VALID")
        assert verdict.work is Work.CATALOGUE
        assert not verdict.dangerous

    def test_the_same_constraint_without_it_is(self) -> None:
        verdict = classify("ALTER TABLE big ADD CONSTRAINT ck CHECK (num > 0)")
        assert verdict.work is Work.SCAN
        assert verdict.dangerous

    def test_validate_afterwards_blocks_nothing(self) -> None:
        verdict = classify("ALTER TABLE big VALIDATE CONSTRAINT ck")
        assert verdict.lock is Lock.SHARE_UPDATE_EXCLUSIVE
        assert verdict.work is Work.SCAN
        assert not verdict.blocking

    def test_the_two_step_remediation_is_strictly_better(self) -> None:
        """The remediation djaudit recommends, checked end to end rather than
        asserted: neither half of it may be dangerous."""
        one_step = classify("ALTER TABLE big ADD CONSTRAINT ck CHECK (num > 0)")
        two_step = [
            classify("ALTER TABLE big ADD CONSTRAINT ck CHECK (num > 0) NOT VALID"),
            classify("ALTER TABLE big VALIDATE CONSTRAINT ck"),
        ]
        assert one_step.dangerous
        assert not any(step.dangerous for step in two_step)


class TestAVolatileDefaultIsDifferent:
    def test_a_constant_default_is_a_catalogue_change(self) -> None:
        assert classify("ALTER TABLE t ADD COLUMN c text DEFAULT 'x'").work is Work.CATALOGUE

    def test_a_volatile_default_rewrites(self) -> None:
        """Measured: `gen_random_uuid()` took 5694ms on two million rows and
        changed `relfilenode`, which is the heap being rewritten."""
        assert classify("ALTER TABLE t ADD COLUMN c uuid DEFAULT gen_random_uuid()").work is (
            Work.REWRITE
        )

    def test_now_is_stable_and_does_not_rewrite(self) -> None:
        """The one intuition gets backwards. `now()` returns the transaction's
        start time and is classified STABLE, so Postgres stores it once: 56ms
        and no `relfilenode` change on the same two million rows, against
        3216ms for `clock_timestamp()`. An earlier draft of the rule listed
        `now` and would have reported a rewrite that does not happen."""
        assert classify("ALTER TABLE t ADD COLUMN c timestamptz DEFAULT now()").work is (
            Work.CATALOGUE
        )

    def test_clock_timestamp_is_the_volatile_one(self) -> None:
        assert classify(
            "ALTER TABLE t ADD COLUMN c timestamptz DEFAULT clock_timestamp()"
        ).work is (Work.REWRITE)

    @postgres
    def test_the_volatility_classes_are_still_what_postgres_says(self) -> None:
        """The claim is about `pg_proc.provolatile`, so it is read from there.
        Postgres could reclassify a function and this table would go stale."""
        result = psql(
            "SELECT proname || '=' || provolatile::text FROM pg_proc WHERE proname IN "
            "('now', 'clock_timestamp', 'random', 'gen_random_uuid', 'nextval')"
        )
        assert result.returncode == 0, f"the query itself failed: {result.stderr}"
        classes = dict(pair.split("=") for pair in result.stdout.split())
        assert len(classes) == 5, f"expected five functions, read {classes}"
        assert classes["now"] == "s", "now() is no longer STABLE; the rule must change"
        for name in ("clock_timestamp", "random", "gen_random_uuid", "nextval"):
            assert classes[name] == "v", f"{name}() is no longer VOLATILE"


class TestItRefusesToGuess:
    def test_an_unrecognised_statement_claims_nothing(self) -> None:
        """This module escalates confidence to `certain`, and a guess is not a
        thing to be certain about."""
        verdict = classify("CLUSTER t USING i1")
        assert verdict.lock is Lock.NONE
        assert verdict.why == UNKNOWN
        assert not verdict.dangerous

    def test_the_control_a_recognised_statement_says_why(self) -> None:
        assert classify("DROP TABLE t").why != UNKNOWN


class TestReadingAStatement:
    def test_the_table_is_extracted_unquoted(self) -> None:
        assert classify('ALTER TABLE "blog_post" ADD COLUMN "body" text;').table == "blog_post"

    def test_an_index_names_the_table_not_the_index(self) -> None:
        assert classify('CREATE INDEX "i" ON "blog_post" ("title");').table == "blog_post"

    def test_a_statement_split_over_lines_is_still_read(self) -> None:
        assert classify('ALTER TABLE\n  "blog_post"\n  DROP COLUMN "x";').lock is (
            Lock.ACCESS_EXCLUSIVE
        )

    def test_lower_case_is_still_read(self) -> None:
        assert classify("alter table big alter column name type text").work is Work.REWRITE

    def test_a_statement_with_no_table_is_not_invented(self) -> None:
        assert classify("SET statement_timeout = 0").table is None


class TestTheWorstOfMany:
    def test_it_picks_the_rewrite_over_the_catalogue_change(self) -> None:
        verdict = worst(
            [
                'ALTER TABLE "t" ADD COLUMN "a" integer;',
                'ALTER TABLE "t" ALTER COLUMN "b" TYPE text;',
                'ALTER TABLE "t" DROP COLUMN "c";',
            ]
        )
        assert verdict is not None
        assert verdict.work is Work.REWRITE

    def test_it_picks_a_blocking_index_over_a_cheap_strong_lock(self) -> None:
        """The ordering that a lock-strength-only sort would get backwards."""
        verdict = worst(['ALTER TABLE "t" DROP COLUMN "c";', 'CREATE INDEX "i" ON "t" ("a");'])
        assert verdict is not None
        assert verdict.lock is Lock.SHARE

    def test_nothing_to_judge(self) -> None:
        assert worst([]) is None

    def test_all_safe_stays_safe(self) -> None:
        verdict = worst(['CREATE TABLE "t2" (id integer);', 'ALTER TABLE "t" DROP COLUMN "c";'])
        assert verdict is not None
        assert not verdict.dangerous


class TestTheSentenceAReaderGets:
    def test_it_names_the_lock_the_table_and_the_duration(self) -> None:
        assert classify('ALTER TABLE "big" ALTER COLUMN "n" TYPE text;').explain() == (
            "takes ACCESS EXCLUSIVE on big while every row is rewritten"
        )

    def test_a_scan_reads_differently_from_a_rewrite(self) -> None:
        assert classify('CREATE INDEX "i" ON "big" ("n");').explain() == (
            "takes SHARE on big while every row is scanned"
        )

    def test_a_catalogue_change_says_so(self) -> None:
        assert classify('ALTER TABLE "big" DROP COLUMN "n";').explain() == (
            "takes ACCESS EXCLUSIVE on big for the time of a catalogue update"
        )

    def test_no_table_leaves_no_dangling_preposition(self) -> None:
        assert classify("SET statement_timeout = 0").explain() == (
            "takes none for the time of a catalogue update"
        )


class TestWhatAReaderIsShown:
    """The enum values are rendered into findings, so they are text a person
    reads, not internal tags. Asserted literally rather than against the
    constants, because a test that builds its expectation from the thing it is
    checking cannot see a wrong value."""

    def test_the_lock_names_are_the_ones_postgres_uses(self) -> None:
        assert str(Lock.NONE) == "none"
        assert str(Lock.SHARE_UPDATE_EXCLUSIVE) == "SHARE UPDATE EXCLUSIVE"
        assert str(Lock.SHARE) == "SHARE"
        assert str(Lock.ACCESS_EXCLUSIVE) == "ACCESS EXCLUSIVE"

    def test_the_work_names_say_what_the_statement_does(self) -> None:
        assert str(Work.CATALOGUE) == "catalogue"
        assert str(Work.SCAN) == "scan"
        assert str(Work.REWRITE) == "rewrite"

    def test_an_unmeasured_statement_says_so_in_words(self) -> None:
        """It goes in front of a reader as the reason, so it has to read as
        one. "" and "unknown" are both worse than admitting the gap."""
        verdict = classify("VACUUM FULL blog_post;")
        assert verdict.why == "this statement is not one djaudit has measured"
        assert verdict.why == UNKNOWN


class TestTheTableItself:
    def test_every_rule_explains_itself(self) -> None:
        for pattern, _, _, why in RULES:
            assert len(why) >= 40, f"{pattern}: {why!r} is not an explanation"

    def test_concurrently_is_ordered_before_the_general_index_rule(self) -> None:
        """First match wins, so an escape hatch listed after the rule it escapes
        would never be reached."""
        patterns = [pattern for pattern, _, _, _ in RULES]
        general = next(i for i, p in enumerate(patterns) if p == r"^CREATE\s+(UNIQUE\s+)?INDEX\b")
        concurrent = next(i for i, p in enumerate(patterns) if "CONCURRENTLY" in p)
        assert concurrent < general

    def test_not_valid_is_ordered_before_add_constraint(self) -> None:
        patterns = [pattern for pattern, _, _, _ in RULES]
        general = next(i for i, p in enumerate(patterns) if p.endswith(r"\bADD\s+CONSTRAINT\b"))
        exempt = next(i for i, p in enumerate(patterns) if "NOT\\s+VALID" in p)
        assert exempt < general

    def test_the_volatile_list_is_used_by_the_rule(self) -> None:
        """The list is a constant the rule interpolates, so a rule that stopped
        using it would leave this passing while classifying nothing."""
        assert any(VOLATILE in pattern for pattern, _, _, _ in RULES)

    def test_a_trailing_semicolon_is_not_part_of_the_table_name(self) -> None:
        """Every statement `sqlmigrate` emits ends in one, and an unquoted name
        is the whole rest of the token. `blog_post;` matches no real table, so
        a run would report a finding against a table nobody can look up."""
        assert classify("DROP TABLE blog_post;").table == "blog_post"

    def test_nor_is_an_opening_bracket(self) -> None:
        assert classify("CREATE TABLE blog_post( id integer);").table == "blog_post"

    def test_a_quoted_name_keeps_its_contents(self) -> None:
        """The control, and it caught a real defect: the trim used to run
        after the quotes came off, so a quoted identifier ending in `(` or `;`
        lost the character. Django quotes every identifier it emits, so the
        quoted branch is the one that runs on real output."""
        assert classify('DROP TABLE "weird;name(";').table == "weird;name("

    def test_a_quoted_name_from_real_output_is_read_whole(self) -> None:
        assert classify('DROP TABLE "blog_post";').table == "blog_post"

    def test_a_trailing_comma_is_not_part_of_it_either(self) -> None:
        assert classify("TRUNCATE blog_post, other;").table == "blog_post"

    def test_a_verdict_is_immutable(self) -> None:
        verdict = classify("DROP TABLE t")
        with pytest.raises(AttributeError):
            verdict.lock = Lock.NONE  # type: ignore[misc]


def _verdicts() -> Iterator[Verdict]:
    for statement, _ in MEASURED:
        yield classify(statement)


class TestTheAxesAgree:
    def test_nothing_blocking_is_lock_none(self) -> None:
        for verdict in _verdicts():
            assert verdict.blocking == verdict.lock.blocks_writes

    def test_dangerous_implies_blocking(self) -> None:
        for verdict in _verdicts():
            assert not verdict.dangerous or verdict.blocking
