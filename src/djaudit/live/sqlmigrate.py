"""The SQL a migration actually emits, read from the target's own Django.

The static tier reads `AlterField(max_length=50)` and has to reason about what
Django will make of it. This asks. `manage.py sqlmigrate` renders the migration
through the real backend, with the real schema editor, at the target's Django
version, and hands back the statements that would run.

**The backend that emitted the SQL is part of the answer.** The same migration
is not the same SQL twice. Measured, on one `AddField(TextField, default="")`:

    postgresql   ALTER TABLE "blog_post" ADD COLUMN "body" text ...
    sqlite3      CREATE TABLE "new__blog_post" (...)
                 INSERT INTO "new__blog_post" SELECT ... FROM "blog_post"
                 DROP TABLE "blog_post"
                 ALTER TABLE "new__blog_post" RENAME TO "blog_post"

SQLite rebuilds the table for almost every schema change. A lock classifier fed
that output would report a catastrophic rewrite for an operation Postgres does
in 55ms on two million rows. So the backend travels with the statements, and
`Emitted.for_postgres` is the gate every Postgres-specific claim must pass.

**It needs a live server, not just a driver.** `sqlmigrate` builds a
`MigrationLoader` around a real connection and reads `django_migrations` before
it renders anything, so a target configured for Postgres with no server
reachable fails with `OperationalError` rather than degrading. That is reported
as a refusal, which is the honest answer: a developer laptop with no database
running cannot be told what its migration will lock.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum, auto
from pathlib import Path
from typing import NamedTuple

from djaudit.live.context import LiveContext
from djaudit.live.interpreter import Interpreter
from djaudit.live.runner import Outcome, run_python

DEFAULT_TIMEOUT = 90.0

BEGIN = "BEGIN;"
COMMIT = "COMMIT;"
UNSUPPORTED = "-- THIS OPERATION CANNOT BE WRITTEN AS SQL"
"""Django's own marker for `RunPython` and friends. Matched literally."""


class Target(NamedTuple):
    """Everything needed to ask a project for SQL, in the shape it arrives.

    Bundled rather than passed as four arguments because a caller holding a
    `LiveContext` already holds all of them, and splitting them apart invites
    the fourth to come from somewhere else -- specifically, a backend that did
    not emit the SQL it is about to be used to interpret.
    """

    interpreter: Interpreter
    manage_py: Path
    backend: str
    database: str | None = None

    @classmethod
    def of(cls, context: LiveContext, manage_py: Path, alias: str = "default") -> Target | None:
        """Read the interpreter and the alias's engine off a live context."""
        engines = dict(context.databases)
        if alias not in engines:
            return None
        return cls(
            interpreter=context.interpreter,
            manage_py=manage_py,
            backend=engines[alias],
            database=None if alias == "default" else alias,
        )


class Statement(NamedTuple):
    """One SQL statement and the migration operation that produced it."""

    sql: str
    operation: str | None = None

    def __str__(self) -> str:
        return self.sql


class Emitted(NamedTuple):
    """The rendered SQL for one migration, and what rendered it."""

    app: str
    name: str
    backend: str
    """The `ENGINE` of the alias that produced this. Never assumed."""

    statements: tuple[Statement, ...] = ()
    atomic: bool = False
    """Whether Django wrapped it in `BEGIN`/`COMMIT`.

    A non-atomic migration that fails halfway leaves the schema half-changed,
    which is a different incident from one that rolls back.
    """

    unsupported: tuple[str, ...] = ()
    """Operations Django declined to render, by name. Usually `RunPython`."""

    @property
    def available(self) -> bool:
        return True

    @property
    def for_postgres(self) -> bool:
        """Whether Postgres lock claims may be made about these statements."""
        return self.backend.endswith("postgresql") or self.backend.endswith("postgis")

    def explain(self) -> str:
        return f"{self.app}.{self.name}: {len(self.statements)} statements via {self.backend}"


class Refused(NamedTuple):
    """Why no SQL could be obtained. Carried, not raised."""

    app: str
    name: str
    why: str

    @property
    def available(self) -> bool:
        return False

    def explain(self) -> str:
        return f"{self.app}.{self.name}: {self.why}"


class _Banner(Enum):
    """Where the parser is inside Django's three-line operation banner."""

    OUTSIDE = auto()
    OPEN = auto()
    """An opening `--` has been seen; the next comment is the description."""

    CLOSING = auto()
    """The description has been read; the next `--` closes the banner."""


def _clean(line: str) -> str:
    return line.strip()


def parse(output: str) -> tuple[tuple[Statement, ...], bool, tuple[str, ...]]:
    """Split `sqlmigrate` output into statements, keeping the operation headings.

    Django prints each operation as a *three*-line `--` banner -- an opening
    rule, the description, a closing rule -- and then its SQL. The heading is
    what makes a finding readable: "Alter field title on post" is the thing the
    author wrote, and the `ALTER TABLE` is the consequence.

    The three lines are why this is a state machine rather than a flag. A flag
    cannot tell the closing rule from the next opening one, so the first
    comment after a banner is read as a new heading -- and Django emits one
    routinely: `-- (no-op)` after `AlterModelOptions`, and the author's own
    leading comment after `RunSQL`. Measured against real output, the flag
    version attributed `SELECT 1;` to `a leading comment` instead of to
    `Raw SQL operation`.

    Statements are joined across lines and split on a trailing semicolon rather
    than on every `;`, because a semicolon inside a string literal or a
    `RunSQL` body is not a statement boundary.
    """
    statements: list[Statement] = []
    unsupported: list[str] = []
    atomic = False
    operation: str | None = None
    pending: list[str] = []
    banner = _Banner.OUTSIDE

    for raw in output.splitlines():
        line = _clean(raw)
        if not line:
            continue
        if line == BEGIN:
            atomic = True
            continue
        if line == COMMIT:
            continue
        if line == UNSUPPORTED:
            if operation is not None:
                unsupported.append(operation)
            continue
        if line == "--":
            banner = _Banner.OPEN if banner is _Banner.OUTSIDE else _Banner.OUTSIDE
            continue
        if line.startswith("--"):
            if banner is _Banner.OPEN:
                operation = line[2:].strip()
                banner = _Banner.CLOSING
            continue
        banner = _Banner.OUTSIDE
        pending.append(line)
        if line.endswith(";"):
            statements.append(Statement(" ".join(pending), operation))
            pending = []

    if pending:
        statements.append(Statement(" ".join(pending), operation))
    return tuple(statements), atomic, tuple(unsupported)


def _refusal(outcome: Outcome) -> str:
    """The target's own last word, which is usually the actionable one.

    `sqlmigrate` reports its own failures as `CommandError: ...` and Django
    reports connection failures as an `OperationalError` traceback; in both
    cases the final line is the sentence worth repeating.
    """
    tail = [line for line in outcome.stderr.splitlines() if line.strip()]
    return tail[-1].strip() if tail else outcome.describe()


def emit(
    target: Target,
    app: str,
    name: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    extra_environment: Mapping[str, str] | None = None,
) -> Emitted | Refused:
    """Render one migration to SQL through the target's own backend.

    The backend is carried on `target` rather than discovered here because the
    caller already asked `LiveContext`, and re-deriving it would be a second
    answer to a question that already has one -- and the whole point of
    recording it is that it must be the engine that produced these statements.
    """
    arguments = [target.manage_py.name, "sqlmigrate", app, name]
    if target.database is not None:
        arguments += ["--database", target.database]

    outcome = run_python(
        target.interpreter,
        arguments,
        cwd=target.manage_py.parent,
        timeout=timeout,
        extra_environment=extra_environment,
    )
    if not outcome.ok:
        return Refused(app, name, _refusal(outcome))

    statements, atomic, unsupported = parse(outcome.stdout)
    return Emitted(
        app=app,
        name=name,
        backend=target.backend,
        statements=statements,
        atomic=atomic,
        unsupported=unsupported,
    )
