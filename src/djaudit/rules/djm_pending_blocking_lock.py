"""`DJM-010` -- a pending migration whose real SQL both blocks and scales.

This is the rule the live tier exists for, and it is not the rule this step was
specified as. The plan asked for "a migration acquiring `ACCESS EXCLUSIVE`".
Measured against a PostgreSQL 18.1 server on a 2,000,000-row table, that
specification is wrong in both directions at once:

===============================================  ==================  ========
statement                                        lock                duration
===============================================  ==================  ========
``ADD COLUMN c text NOT NULL DEFAULT 'x'``       ACCESS EXCLUSIVE     60ms
``ALTER COLUMN TYPE varchar(50)``                ACCESS EXCLUSIVE   2870ms
``CREATE INDEX``                                 SHARE              1031ms
===============================================  ==================  ========

The first two take the identical lock and differ by **48x**, because since
Postgres 11 a constant default is stored once rather than written to every row.
The third takes a *weaker* lock and is the outage people actually have. A rule
keyed on lock mode would report the harmless statement and stay silent on the
damaging one.

So the finding is **blocking and scaling**: the statement holds a lock that
stops reads or writes, *and* does work proportional to the number of rows.
Either alone is survivable. A catalogue change under `ACCESS EXCLUSIVE` is over
in milliseconds; a full scan under a lock that blocks nothing is just slow.
Together they are how a deploy takes a table offline for minutes.

**What this adds over the static `DJM` rules** is not a new pattern to match. It
is three things none of them can have:

* **The SQL, not a prediction of it.** `DJM-004` reasons that an `AlterField`
  narrowing a `CharField` will rewrite the table. This one reads
  `ALTER TABLE ... TYPE varchar(50)` out of the target's own Django.
* **Certainty about scope.** The static family reports the leaf of each app's
  history, a heuristic that over-reports shipped work and misses the second of
  two migrations. This reads `django_migrations` and knows.
* **The backend.** Every static `DJM` rule assumes Postgres and says so in its
  limitations. This one refuses to speak unless the alias that emitted the SQL
  really is Postgres, because the same migration renders as four statements
  there and ten on SQLite -- and read through a Postgres classifier, the SQLite
  rendering hides the dangerous operation inside a table rebuild that looks
  harmless.

It therefore reports the migration, not the operation class, and cites the
emitted statement as evidence. Where the operation can be identified exactly it
points at that line; where it cannot, it points at the file and says so rather
than guessing.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from djaudit.context import ProjectContext
from djaudit.migrations.nodes import MigrationNode
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)
from djaudit.registry import Rule, RuleMeta, register

if TYPE_CHECKING:
    from djaudit.live.sqlmigrate import Emitted, Statement, Target
    from djaudit.live.tables import Size, Sizes
    from djaudit.live.tables import Unknown as TablesUnknown

BUDGET = 40
"""How many pending migrations to render before giving up on the rest.

Each one is a subprocess against a live database. A project mid-rebase can have
a hundred pending, and a linter that takes four minutes is a linter nobody runs.
Truncation is reported as a diagnostic rather than passed over in silence.
"""


def _headings(statements: Sequence[Statement]) -> list[str]:
    """The operation headings, in order, without repeats.

    Django prints one banner per operation, including for operations that emit
    no SQL at all, so this is the migration's operation list as its own Django
    enumerated it.
    """
    seen: list[str] = []
    for statement in statements:
        if statement.operation is not None and statement.operation not in seen:
            seen.append(statement.operation)
    return seen


@register
class BlockingPendingMigration(Rule):
    """A pending migration whose emitted SQL blocks a table while it scales."""

    meta = RuleMeta(
        id="DJM-010",
        title="Pending migration takes a blocking lock for the length of the table",
        family=Family.DJM,
        tier=Tier.LIVE,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        rationale=(
            "The statement holds a lock that stops reads or writes and does "
            "work proportional to the row count, so the table is unavailable "
            "for as long as the table is large. It is instant in CI against an "
            "empty database and minutes long in production against the same "
            "schema, which is why it survives review. This is read from the "
            "SQL the project's own Django emits, not inferred from the "
            "operation, so the statement quoted below is the statement that "
            "will run."
        ),
        remediation=(
            "Split the operation so the blocking part does no scanning. Build "
            "indexes with `AddIndexConcurrently`, which takes `SHARE UPDATE "
            "EXCLUSIVE` and blocks neither reads nor writes. Add constraints "
            "as `NOT VALID` and validate them in a second migration, which "
            "takes the same weak lock -- measured at 59ms against 286ms for "
            "the validating form. Where a column type genuinely has to change, "
            "add the new column, backfill it in batches outside a "
            "transaction, and swap it, rather than letting `ALTER COLUMN TYPE` "
            "rewrite the table under a lock that blocks reads."
        ),
        references=(
            "https://www.postgresql.org/docs/current/explicit-locking.html",
            "https://www.postgresql.org/docs/current/sql-altertable.html",
            "https://docs.djangoproject.com/en/stable/ref/contrib/postgres/operations/",
        ),
        limitations=(
            "The lock and work classification is measured against PostgreSQL "
            "and applies to no other backend, so the rule declines to report "
            "unless the alias that emitted the SQL is Postgres or PostGIS.",
            "A statement the classifier has not measured is treated as taking "
            "no lock rather than as taking the strongest one, because this "
            "rule reports at `certain` and a guess is not a thing to be "
            "certain about. The static rules still see the operation.",
            "Severity is scaled by the table's size on disk, read from "
            "`pg_class`, using a rate of 16 ms per megabyte measured on one "
            "machine rewriting one column shape. It is an order of magnitude, "
            "not a promise about your hardware.",
            "A table the size query did not return, and a database it could "
            "not reach, both leave severity exactly as declared. Absent "
            "information never makes a finding quieter.",
            f"At most {BUDGET} pending migrations are rendered per run, in "
            "the order they would run, because each one is a subprocess "
            "against the live database. Any beyond that are not examined.",
            "Only migrations belonging to the project's own apps are reported. "
            "A pending migration from an installed package is real, but it "
            "cannot be cited as a line in the project's source and cannot be "
            "edited there, so it is left to the package's own release notes.",
        ),
        fallback=(
            "Without the live tier the emitted SQL is never read, so findings "
            "rest on what the operation is expected to produce rather than on "
            "what it does, and on the leaf of each app's history rather than "
            "on which migrations are actually pending."
        ),
        fallback_rules=("DJM-003", "DJM-004", "DJM-006"),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        # Imported here, not at module scope. Every rule module is imported on
        # every run, and this one is the only door onto `subprocess`: hoisting
        # these put 24ms and the whole process-spawning stack into audits that
        # never asked for the live tier. Measured, both ways.
        from djaudit.live.migrations import read_plan  # noqa: PLC0415

        target = self._target(ctx)
        if target is None:
            return

        plan = read_plan(target)
        if not plan.available:
            # Deleting this guard changes no outcome, and that is deliberate:
            # `Unknown.unapplied` is empty, so a caller that ignores
            # `available` under-reports instead of naming every migration in
            # the project as pending. Two independent reasons to stay silent
            # about a database nobody could reach. Mutation testing reports it
            # as a survivor; it is equivalent, not untested.
            return

        # Only the project's own migrations: a pending migration from an
        # installed package is real, but it has no line in this repository to
        # point at and no edit the reader could make there.
        nodes = {node.key: node for node in ctx.migration_graph.plan()}
        mine = [key for key in plan.unapplied if key in nodes]
        if not mine:
            return

        # Read once, not per migration: it is a subprocess against the live
        # database, and every finding in this run asks it the same question.
        # Deferred until there is something to report so a fully-migrated
        # project pays nothing for it.
        sizes = self._sizes(target)
        for key in mine[:BUDGET]:
            emitted = self._emit(target, *key)
            if emitted is not None:
                yield from self._judge(ctx, emitted, nodes[key], sizes)

    def _sizes(self, target: Target) -> Sizes | TablesUnknown:
        from djaudit.live.tables import read_sizes  # noqa: PLC0415

        return read_sizes(target)

    def _severity(self, size: Size | None) -> Severity:
        """How bad this is, given how much data the lock is held across.

        Only ever *lowers* the declared severity, and only on a measurement.
        An unreadable database, an unrecognised table or a size nobody could
        take leaves the finding exactly as the rule declared it -- raising
        severity should take evidence, and lowering it should take more,
        because a finding nobody reads is the failure mode that matters here.

        The boundaries are the measured 16 ms/MB read backwards: 64 MB is about
        a second, past which the lock outlives an ordinary request timeout, and
        8 MB is about 130 ms, inside what one slow request costs anyway.
        """
        from djaudit.live.tables import NOTICEABLE, SUSTAINED  # noqa: PLC0415

        if size is None or size.stored >= SUSTAINED:
            return self.meta.severity
        return Severity.MEDIUM if size.stored >= NOTICEABLE else Severity.LOW

    def _target(self, ctx: ProjectContext) -> Target | None:
        """The interpreter and backend to ask, or nothing at all.

        Built only from the context that was disclosed and consented to. A rule
        that found its own interpreter would be running an environment the user
        was never told about.
        """
        from djaudit.live.sqlmigrate import Target  # noqa: PLC0415

        if ctx.live_context is None or ctx.manage_py is None:
            return None
        return Target.of(ctx.live_context, ctx.manage_py)

    def _emit(self, target: Target, app: str, name: str) -> Emitted | None:
        """Render one migration, or nothing if it could not be rendered.

        A refusal is not a finding. `sqlmigrate` fails for reasons that are the
        project's business -- an unreachable replica, a migration whose module
        does not import -- and inventing a lock claim from a failure is exactly
        the fabrication this family is built to avoid.
        """
        from djaudit.live.sqlmigrate import Emitted, emit  # noqa: PLC0415

        result = emit(target, app, name)
        if not isinstance(result, Emitted):
            return None
        # The classification is Postgres's. Applied to SQLite's rendering it
        # reads a table rebuild as routine and misses the rewrite entirely.
        if not result.for_postgres:
            return None
        return result

    def _judge(
        self,
        ctx: ProjectContext,
        emitted: Emitted,
        node: MigrationNode,
        sizes: Sizes | TablesUnknown | None = None,
    ) -> Iterator[Finding]:
        from djaudit.live.locks import classify, worst  # noqa: PLC0415
        from djaudit.live.tables import estimate  # noqa: PLC0415

        dangerous = [
            statement for statement in emitted.statements if classify(statement.sql).dangerous
        ]
        # One finding per migration, on its worst statement: a migration that
        # rewrites a table twice is one deploy incident, not two. The ordering
        # is `locks.worst`'s rather than a second copy of it, because two
        # rankings of the same three axes would eventually disagree.
        #
        # `worst` answers `None` for an empty list, which is also the answer for
        # a migration with nothing dangerous in it, so this is the only guard
        # needed. There used to be an `if not dangerous: return` above it; the
        # two were redundant, and the pair made whichever ran second unreachable
        # -- it carried a `no cover` pragma saying so. Dead code and an untested
        # branch are indistinguishable from the outside.
        verdict = worst([statement.sql for statement in dangerous])
        if verdict is None:
            return
        statement = next(s for s in dangerous if s.sql == verdict.statement)
        size = sizes.get(verdict.table) if sizes is not None else None
        scale = estimate(size)
        yield self.finding(
            location=self._locate(ctx, emitted, statement, node),
            severity=self._severity(size),
            message=(
                f"`{emitted.app}.{emitted.name}` is not applied yet and emits "
                f"`{_summarise(statement.sql)}`, which {verdict.explain()}."
                + (f" Operation: {statement.operation}." if statement.operation else "")
                + (f" {scale}" if scale else "")
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.SQL,
                    content=statement.sql,
                    source=f"manage.py sqlmigrate {emitted.app} {emitted.name}",
                ),
                Evidence(
                    kind=EvidenceKind.COMMAND_OUTPUT,
                    content=(
                        f"lock={verdict.lock.value} work={verdict.work.value} "
                        f"table={verdict.table or 'unknown'} backend={emitted.backend} "
                        f"atomic={emitted.atomic} "
                        f"size={size.describe() if size else 'not measured'}"
                    ),
                    source=f"manage.py sqlmigrate {emitted.app} {emitted.name}",
                ),
            ),
        )

    def _locate(
        self,
        ctx: ProjectContext,
        emitted: Emitted,
        statement: Statement,
        node: MigrationNode,
    ) -> Location:
        """Point at the operation that produced the statement, when that is knowable.

        Django prints one banner per operation, in order, so the index of the
        heading is the index of the operation. That correspondence is checked
        rather than assumed: if the counts disagree -- a Django version that
        prints differently, a migration we parsed partially -- the finding
        points at the file instead of at a line chosen by an assumption.
        """
        headings = _headings(emitted.statements)
        if (
            statement.operation is not None
            and len(headings) == len(node.operations)
            and statement.operation in headings
        ):
            operation = node.operations[headings.index(statement.operation)]
            if operation.node is not None:
                return ctx.location(node.path, operation.node)
            return Location(
                file=ctx.rel(node.path),
                line=operation.lineno,
                column=1,
                end_line=operation.end_lineno,
                snippet=ctx.snippet(node.path, operation.lineno, operation.end_lineno),
            )
        return Location(file=ctx.rel(node.path), line=1, column=1)


def _summarise(sql: str, limit: int = 90) -> str:
    """The statement, short enough to read in a one-line message."""
    text = " ".join(sql.split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
