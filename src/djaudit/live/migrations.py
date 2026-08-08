"""Which migrations have not run yet, read from the database rather than guessed.

The `DJM` family reports on what a migration will do when it runs, so its
central question is which migrations still have a "when". The static tier
answers it with a heuristic -- the leaf of each app's history -- and says so in
every rule's `limitations`. This module answers it exactly.

`showmigrations --plan` is the source, not `sqlmigrate`, because it reports the
whole project in one process. Asking per migration would be one subprocess per
migration, and the corpus holds 875 of them.

The format is Django's own, taken from `showmigrations.py` rather than from
observation alone::

    self.stdout.write("[X]  %s.%s%s" % (node.key[0], node.key[1], deps))
    self.stdout.write("[ ]  %s.%s%s" % (node.key[0], node.key[1], deps))

`deps` is a ` ... (app.name, ...)` suffix that only appears at verbosity 2. The
command is run at verbosity 1 so it cannot, and the parser strips it anyway,
because a caller passing `extra_environment` has no way to know that a setting
somewhere does not raise the default.

**Unapplied order is preserved.** The plan is a topological order, and it is the
order the migrations will actually run in; a set would throw away the answer to
"which of these locks the table first".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from djaudit.live.runner import Outcome, run_python
from djaudit.live.sqlmigrate import Target, _refusal

DEFAULT_TIMEOUT = 60.0

EMPTY = "(no migrations)"
"""What Django writes for a project that has no migrations at all.

Used to tell that state apart from unreadable output. Both parse to nothing, and
answering `Unknown` to both would report a real, complete, entirely-migrated
answer as a failure to look.

It is deliberately *not* consulted while parsing lines: anything that does not
open with `[X]` or `[ ]` is skipped there already, so a check for it would be
dead -- and dead code is indistinguishable from an untested branch.
"""


class Plan(NamedTuple):
    """The migration graph, split by whether each one has run."""

    applied: frozenset[tuple[str, str]]
    unapplied: tuple[tuple[str, str], ...]
    """In the order they will run, which is the order they will take locks."""

    @property
    def available(self) -> bool:
        return True

    @property
    def total(self) -> int:
        return len(self.applied) + len(self.unapplied)

    def explain(self) -> str:
        return f"{len(self.unapplied)} of {self.total} migrations have not been applied"


class Unknown(NamedTuple):
    """The database could not be asked. Carried, not raised."""

    why: str

    @property
    def available(self) -> bool:
        return False

    @property
    def unapplied(self) -> tuple[tuple[str, str], ...]:
        """Nothing, so a caller that ignores `available` reports nothing.

        The failure mode of the alternative is reporting every migration in the
        project as pending, which is both wrong and loud.
        """
        return ()

    def explain(self) -> str:
        return f"migration state is unknown: {self.why}"


def parse_plan(output: str) -> tuple[frozenset[tuple[str, str]], tuple[tuple[str, str], ...]]:
    """Split `showmigrations --plan` output into applied and unapplied keys."""
    applied: set[tuple[str, str]] = set()
    unapplied: list[tuple[str, str]] = []

    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("[X]"):
            done = True
        elif line.startswith("[ ]"):
            done = False
        else:
            continue
        # The verbosity-2 dependency suffix, defended against rather than
        # relied upon being absent.
        label = line[3:].split(" ... ")[0].strip()
        # App labels cannot contain a dot, so the first one is the separator.
        app, _, name = label.partition(".")
        if not app or not name:
            continue
        if done:
            applied.add((app, name))
        else:
            unapplied.append((app, name))

    return frozenset(applied), tuple(unapplied)


def read_plan(
    target: Target,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    extra_environment: Mapping[str, str] | None = None,
) -> Plan | Unknown:
    """Ask the target which of its migrations have run.

    Reaches the database, so it fails exactly where `sqlmigrate` fails, and for
    the same reason: `MigrationLoader` reads `django_migrations` before it can
    say anything. A failure is reported rather than defaulted, because the
    default would be a claim about a database nobody could reach.
    """
    arguments = [target.manage_py.name, "showmigrations", "--plan", "--verbosity", "1"]
    if target.database is not None:
        arguments += ["--database", target.database]

    outcome: Outcome = run_python(
        target.interpreter,
        arguments,
        cwd=target.manage_py.parent,
        timeout=timeout,
        extra_environment=extra_environment,
    )
    if not outcome.ok:
        return Unknown(_refusal(outcome))

    applied, unapplied = parse_plan(outcome.stdout)
    if not applied and not unapplied:
        if EMPTY in outcome.stdout:
            return Plan(frozenset(), ())
        return Unknown("`showmigrations --plan` named no migrations")
    return Plan(applied, unapplied)
