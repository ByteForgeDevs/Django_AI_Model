"""What a statement locks, and for how long. Measured, not cited.

Every entry in this module was obtained by running the statement against
PostgreSQL 18.1 and reading `pg_locks` from the backend that took the lock,
then timing it against a 2,000,000-row, 142 MB table. The measurements are in
`tests/live/test_locks.py`, which is where they can fail.

**Lock mode alone does not predict an outage.** That is the finding this module
is arranged around, and it refutes the obvious design:

    ALTER TABLE big ADD COLUMN c text NOT NULL DEFAULT 'x'   AccessExclusive     60ms
    ALTER TABLE big ALTER COLUMN name TYPE varchar(50)       AccessExclusive   2870ms

The same lock, forty-eight times the outage. Postgres 11 made a non-volatile
default a catalogue change, so `ADD COLUMN` takes the strongest lock in the
system and holds it for as long as it takes to write one row to `pg_attribute`.
Reporting it as dangerous because it takes `ACCESS EXCLUSIVE` would be a false
positive on the single most common migration operation there is.

The reverse error is worse:

    CREATE INDEX idx ON big (num)                            Share            1031ms

`CREATE INDEX` never takes `ACCESS EXCLUSIVE`. A rule that looked for that mode
would miss the statement that blocks every write to the table for a second per
two million rows -- which is the outage people actually have.

So a statement is classified on two axes. `Lock` is what it blocks; `Work` is
how long it holds it. Danger is the product, and neither factor alone is it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import NamedTuple


class Lock(StrEnum):
    """The Postgres lock mode a statement takes on its table.

    Ordered by what they exclude, which is the order that matters to a reader:
    `NONE` blocks nothing, `SHARE_UPDATE_EXCLUSIVE` blocks only schema changes,
    `SHARE` blocks writes, and `ACCESS_EXCLUSIVE` blocks reads as well.

    Every member's `blocks_reads` and `blocks_writes` was measured by holding
    the mode on one connection and attempting a `SELECT` and an `INSERT` on
    another under `lock_timeout`; see `TestWhatTheModesActuallyBlock`.
    """

    NONE = "none"
    """No lock that ordinary traffic can wait on.

    Not a claim that the statement takes no lock at all. `INSERT` takes
    `ROW EXCLUSIVE` and `SELECT` takes `ACCESS SHARE`, and both were measured
    to block neither reads nor writes. `CREATE TABLE` is the literal case:
    there is no prior table to lock.

    `CONCURRENTLY` is deliberately *not* here. It reads as the same thing to a
    user -- it blocks neither reads nor writes -- but it really does take
    `SHARE UPDATE EXCLUSIVE`, and saying `none` made `explain()` emit "takes
    none on t", which is not true of any statement that touches a table.
    """

    SHARE_UPDATE_EXCLUSIVE = "SHARE UPDATE EXCLUSIVE"
    SHARE = "SHARE"
    ACCESS_EXCLUSIVE = "ACCESS EXCLUSIVE"

    @property
    def blocks_writes(self) -> bool:
        return self in (Lock.SHARE, Lock.ACCESS_EXCLUSIVE)

    @property
    def blocks_reads(self) -> bool:
        return self is Lock.ACCESS_EXCLUSIVE


class Work(StrEnum):
    """How much of the table the statement has to touch while it holds the lock."""

    CATALOGUE = "catalogue"
    """A row in a system table. Constant time, whatever the table's size."""

    SCAN = "scan"
    """Every row is read to verify something. Linear, no rewrite."""

    REWRITE = "rewrite"
    """Every row is written to a new heap. Linear, and doubles the disk needed."""


class Verdict(NamedTuple):
    """One statement, classified."""

    lock: Lock
    work: Work
    table: str | None
    statement: str
    why: str

    @property
    def blocking(self) -> bool:
        """Whether concurrent traffic waits, at all, for any length of time."""
        return self.lock.blocks_writes

    @property
    def dangerous(self) -> bool:
        """Whether the wait scales with the table.

        A `CATALOGUE` change under `ACCESS EXCLUSIVE` is not dangerous: it is
        the normal cost of a schema change and is over in milliseconds. What
        makes a deploy fail is a strong lock held for a time proportional to
        the data, which is `SCAN` or `REWRITE` under a lock that blocks.
        """
        return self.blocking and self.work is not Work.CATALOGUE

    def explain(self) -> str:
        held = {
            Work.CATALOGUE: "for the time of a catalogue update",
            Work.SCAN: "while every row is scanned",
            Work.REWRITE: "while every row is rewritten",
        }[self.work]
        where = f" on {self.table}" if self.table else ""
        return f"takes {self.lock.value}{where} {held}"


_RELATION = r'"[^"]+"(?:\."[^"]+")*|\S+'
"""One relation reference: quoted, optionally schema-qualified, or a bare word."""


def _unqualify(raw: str) -> str:
    """The relation's own name, with any schema and quoting removed.

    `ALTER TABLE "app"."post"` acts on `post` in schema `app`, and reporting the
    schema as the table names something that is not a table at all.
    """
    if raw.startswith('"'):
        return raw.rsplit('"."', maxsplit=1)[-1].strip('"')
    return raw.rstrip("(;,").rsplit(".", 1)[-1]


def _table(statement: str) -> str | None:
    """The relation the statement acts on, unquoted.

    A quoted identifier is returned exactly as Postgres would read it. Only an
    *unquoted* capture is trimmed, because there the regex ran to the next
    whitespace and may have swallowed the punctuation that followed the name --
    `DROP TABLE blog_post;` captures `blog_post;`, which matches no real table.
    Trimming a quoted name would instead corrupt one: Postgres permits `;` and
    `(` inside quotes, and Django quotes every identifier it emits.
    """
    for pattern in (
        rf"\bALTER\s+TABLE\s+(?:ONLY\s+)?({_RELATION})",
        r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
        # The index name is optional: `CREATE INDEX ON t (c)` is valid Postgres
        # and lets the server choose one. Requiring a name made the whole
        # pattern fail, and the finding reported its table as unknown.
        rf"(?:IF\s+NOT\s+EXISTS\s+)?(?:(?!ON\b)\S+\s+)?ON\s+({_RELATION})",
        rf"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?({_RELATION})",
        rf"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_RELATION})",
        rf"\bTRUNCATE\s+(?:TABLE\s+)?({_RELATION})",
    ):
        found = re.search(pattern, statement, re.IGNORECASE)
        if found:
            return _unqualify(found.group(1))
    return None


VOLATILE = "nextval|random|clock_timestamp|timeofday|gen_random_uuid|uuid_generate_v[0-9]"
"""Defaults that force a table rewrite, by Postgres's own volatility class.

Read from `pg_proc.provolatile` rather than from intuition, because intuition
gets this one wrong. `now()` looks like the most volatile thing in SQL and is
classified STABLE -- it returns the transaction's start time, which is fixed
for the duration -- so Postgres stores it once and does not rewrite. Measured
on two million rows: `DEFAULT now()` took 56ms and left `relfilenode` alone,
while `DEFAULT clock_timestamp()` took 3216ms and changed it. An earlier draft
of this rule listed `now`, which would have reported a rewrite that does not
happen on one of the most common columns anybody adds.
"""


# Ordered: the first pattern that matches wins, so the specific forms that
# escape a general rule -- CONCURRENTLY, NOT VALID -- are listed above it.
RULES: tuple[tuple[str, Lock, Work, str], ...] = (
    (
        r"^CREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY\b",
        Lock.SHARE_UPDATE_EXCLUSIVE,
        Work.SCAN,
        "CONCURRENTLY builds the index without blocking reads or writes",
    ),
    (
        r"^DROP\s+INDEX\s+CONCURRENTLY\b",
        Lock.SHARE_UPDATE_EXCLUSIVE,
        Work.CATALOGUE,
        "CONCURRENTLY drops the index without blocking reads or writes",
    ),
    (
        r"^CREATE\s+(UNIQUE\s+)?INDEX\b",
        Lock.SHARE,
        Work.SCAN,
        "building an index in place blocks every write to the table until it finishes",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bVALIDATE\s+CONSTRAINT\b",
        Lock.SHARE_UPDATE_EXCLUSIVE,
        Work.SCAN,
        "validation scans the table but blocks neither reads nor writes",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bADD\s+CONSTRAINT\b.*\bNOT\s+VALID\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "NOT VALID records the constraint without checking existing rows",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bADD\s+CONSTRAINT\b.*\b(UNIQUE|PRIMARY\s+KEY)\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.REWRITE,
        "a unique constraint builds an index in place under the strongest lock",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bADD\s+CONSTRAINT\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.SCAN,
        "every existing row is checked before the constraint is accepted",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bALTER\s+COLUMN\b.*\bTYPE\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.REWRITE,
        "changing a column's type rewrites every row of the table",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bALTER\s+COLUMN\b.*\bSET\s+NOT\s+NULL\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.SCAN,
        "every row is scanned to prove no NULL is present",
    ),
    (
        rf"^ALTER\s+TABLE\s+.*\bADD\s+COLUMN\b.*\bDEFAULT\b.*\b({VOLATILE})\s*\(",
        Lock.ACCESS_EXCLUSIVE,
        Work.REWRITE,
        "a VOLATILE default is evaluated per row, so the whole table is rewritten",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bADD\s+COLUMN\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "since Postgres 11 a constant default is stored once, not written per row",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bDROP\s+COLUMN\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "the column is marked dropped; the data is reclaimed later by vacuum",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bALTER\s+COLUMN\b.*\b(DROP|SET)\s+(NOT\s+NULL|DEFAULT)\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "a catalogue change, held only as long as the transaction",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bDROP\s+CONSTRAINT\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "a catalogue change, held only as long as the transaction",
    ),
    (
        r"^ALTER\s+(TABLE|INDEX)\s+.*\bRENAME\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "renaming is a catalogue change, but the name is gone for readers of the old one",
    ),
    (
        r"^ALTER\s+TABLE\s+.*\bSET\s*\(",
        Lock.SHARE_UPDATE_EXCLUSIVE,
        Work.CATALOGUE,
        "a storage parameter change blocks only other schema changes",
    ),
    (
        r"^DROP\s+INDEX\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "dropping an index takes the strongest lock, briefly",
    ),
    (
        r"^DROP\s+TABLE\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "the table is gone; anything still querying it fails",
    ),
    (
        r"^TRUNCATE\b",
        Lock.ACCESS_EXCLUSIVE,
        Work.CATALOGUE,
        "truncation takes the strongest lock and discards every row",
    ),
    (
        r"^CREATE\s+TABLE\b",
        Lock.NONE,
        Work.CATALOGUE,
        "a new table has no existing traffic to block",
    ),
    (
        r"^(INSERT|UPDATE|DELETE|SELECT|SET|COMMENT)\b",
        Lock.NONE,
        Work.CATALOGUE,
        "not a schema change, so it takes no lock a schema change would take",
    ),
)

_COMPILED = tuple(
    (re.compile(p, re.IGNORECASE | re.DOTALL), lk, wk, why) for p, lk, wk, why in RULES
)

UNKNOWN = "this statement is not one djaudit has measured"


def classify(statement: str) -> Verdict:
    """Classify one statement. Never guesses: an unmatched statement is `NONE`.

    An unrecognised statement is reported as blocking nothing, rather than as
    possibly blocking everything. This module's output escalates a finding's
    confidence to `certain`, and a guess is not a thing to be certain about --
    the static rules still see the operation and still say what they saw.
    """
    text = " ".join(statement.split())
    for pattern, lock, work, why in _COMPILED:
        if pattern.search(text):
            return Verdict(lock=lock, work=work, table=_table(text), statement=statement, why=why)
    return Verdict(Lock.NONE, Work.CATALOGUE, _table(text), statement, UNKNOWN)


def worst(statements: tuple[str, ...] | list[str]) -> Verdict | None:
    """The statement that hurts most, by danger then by lock strength."""
    verdicts = [classify(text) for text in statements]
    if not verdicts:
        return None
    order = {Lock.NONE: 0, Lock.SHARE_UPDATE_EXCLUSIVE: 1, Lock.SHARE: 2, Lock.ACCESS_EXCLUSIVE: 3}
    effort = {Work.CATALOGUE: 0, Work.SCAN: 1, Work.REWRITE: 2}
    return max(verdicts, key=lambda v: (v.dangerous, effort[v.work], order[v.lock]))
