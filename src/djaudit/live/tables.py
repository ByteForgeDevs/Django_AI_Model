"""How big the tables actually are, so a lock's cost can be estimated.

`DJM-010` classifies a statement as blocking and scaling, but "scaling" is only
half an answer: an `ALTER COLUMN TYPE` rewrites every row, and on a table with
fifty rows that is not an incident. Reporting both at the same severity is the
limitation this module removes.

**Measured, on PostgreSQL 18.1, rewriting a `varchar(200)` column to
`varchar(50)`:**

| rows | heap size | rewrite |
|---|---|---|
| 1,000 | 96 kB | 12 ms |
| 10,000 | 912 kB | 22 ms |
| 100,000 | 8.9 MB | 135 ms |
| 1,000,000 | 88.8 MB | 1,422 ms |

Above ten thousand rows the cost is linear in bytes and the constant is stable:
15.2 ms/MB at 100k, 16.0 ms/MB at 1M. The thresholds in `estimate` are that
constant read backwards, not round numbers chosen because they look tidy.

**Size, not row count, is the signal.** Two reasons, both measured. Postgres
reports `reltuples = -1` for a table that has never been analysed -- not zero,
because zero would be a claim -- and on a freshly-restored database that is
almost every table, exactly when someone is most likely to be running
migrations. `pg_relation_size` is exact from the first row inserted. And bytes
are what a rewrite actually moves, so a wide row and a narrow one are priced
correctly without knowing anything about the schema.

**Nothing is downgraded on missing information.** An unreadable database, an
unrecognised table, a size nobody can measure: each leaves the finding's
declared severity alone. Raising severity needs evidence; lowering it needs
*more*, because a quiet finding is one nobody acts on.

The query is a constant. It names no table, takes its three literals as bound
parameters, and filters in Python afterwards -- a tool that reports SQL
injection should not build SQL by interpolation, even against its own catalogue
and even where the identifiers came from the database in the first place.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple

from djaudit.live.runner import run_python
from djaudit.live.sqlmigrate import _refusal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from djaudit.live.sqlmigrate import Target

DEFAULT_TIMEOUT = 60.0

MARKER = "DJAUDIT_SIZES "
"""Prefix identifying our line in the command's output.

Django's `shell -c` writes `N objects imported automatically` to stdout before
anything the script prints, and a project's own settings module may print
whatever it likes. Measured against the real command, not assumed.
"""

MEGABYTE = 1024 * 1024

NOTICEABLE = 8 * MEGABYTE
"""Roughly 130 ms of held lock, at the measured 16 ms/MB.

Below this a blocking rewrite finishes inside the time a single slow request
would have taken anyway.
"""

SUSTAINED = 64 * MEGABYTE
"""Roughly one second. Past here the lock outlasts an ordinary request timeout,
so the failure stops being latency and becomes errors."""

QUERY = """
SELECT c.relname, c.reltuples::bigint, pg_relation_size(c.oid)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = %s AND n.nspname NOT IN (%s, %s)
"""

SCRIPT = f"""
import json
from django.db import connections
with connections[{{alias!r}}].cursor() as cursor:
    cursor.execute({QUERY!r}, ["r", "pg_catalog", "information_schema"])
    rows = cursor.fetchall()
print({MARKER!r} + json.dumps([[r[0], int(r[1]), int(r[2])] for r in rows]))
"""


class Size(NamedTuple):
    """One table, as the database currently describes it."""

    table: str
    stored: int
    """Heap bytes. Exact, and available before anything has been analysed."""

    rows: int | None
    """Live tuples, or `None` when Postgres has never analysed the table.

    `reltuples` is `-1` in that case rather than `0`. Reading that as an empty
    table would be the worst available error: it is the state of every table in
    a freshly restored database, which is when migrations are most likely to be
    running against real data.
    """

    @property
    def milliseconds(self) -> int:
        """What a full rewrite of this table would cost, at 16 ms/MB.

        An estimate from one machine and one column shape, so it belongs in a
        message as an order of magnitude and never in a comparison.
        """
        return round(self.stored / MEGABYTE * 16)

    def describe(self) -> str:
        counted = f"{self.rows:,} rows" if self.rows is not None else "row count not analysed"
        return f"{self.stored / MEGABYTE:.1f} MB, {counted}"


class Sizes(NamedTuple):
    """Every ordinary table in the target's database."""

    by_table: Mapping[str, Size]

    @property
    def available(self) -> bool:
        return True

    def get(self, table: str | None) -> Size | None:
        return self.by_table.get(table) if table is not None else None

    def explain(self) -> str:
        return f"{len(self.by_table)} tables measured"


class Unknown(NamedTuple):
    """The database could not be measured. Carried, not raised."""

    why: str

    @property
    def available(self) -> bool:
        return False

    def get(self, table: str | None) -> None:
        """Always nothing, so a caller that ignores `available` declines to
        rescale rather than rescaling against a size it never read."""
        return None

    def explain(self) -> str:
        return f"table sizes are unknown: {self.why}"


def parse_sizes(output: str) -> dict[str, Size]:
    """Read the marked line out of the command's output."""
    for line in output.splitlines():
        if not line.startswith(MARKER):
            continue
        try:
            rows = json.loads(line[len(MARKER) :])
        except json.JSONDecodeError:
            return {}
        return {
            str(name): Size(str(name), int(stored), None if int(tuples) < 0 else int(tuples))
            for name, tuples, stored in rows
        }
    return {}


def read_sizes(
    target: Target,
    *,
    alias: str = "default",
    timeout: float = DEFAULT_TIMEOUT,
    extra_environment: Mapping[str, str] | None = None,
) -> Sizes | Unknown:
    """Ask the target's database how big its tables are.

    Runs in the target's interpreter because that is where the database driver
    is: our own environment deliberately has no `psycopg`, and installing one
    would mean measuring a connection the project never described.
    """
    outcome = run_python(
        target.interpreter,
        [target.manage_py.name, "shell", "-c", SCRIPT.format(alias=alias)],
        cwd=target.manage_py.parent,
        timeout=timeout,
        extra_environment=extra_environment,
    )
    if not outcome.ok:
        return Unknown(_refusal(outcome))
    sizes = parse_sizes(outcome.stdout)
    if not sizes:
        return Unknown("the size query returned nothing")
    return Sizes(sizes)


def estimate(size: Size | None) -> str | None:
    """A sentence about what this table's size means, or nothing to say."""
    if size is None:
        return None
    if size.stored >= SUSTAINED:
        return (
            f"The table holds {size.describe()}, so the lock is held for "
            f"roughly {size.milliseconds / 1000:.0f}s -- long enough to exhaust "
            "connection pools and time requests out."
        )
    if size.stored >= NOTICEABLE:
        return (
            f"The table holds {size.describe()}, so the lock is held for "
            f"roughly {size.milliseconds}ms."
        )
    return (
        f"The table holds {size.describe()}, small enough that the lock is "
        "measured in milliseconds."
    )
