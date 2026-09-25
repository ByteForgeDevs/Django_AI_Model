# The live tier

The static tier reads the target's source with `ast` and never imports it. The
live tier runs the target's own interpreter over the target's own `manage.py`.

That difference is not a matter of degree. **The audited project's code
executes on the auditing machine.** `settings.py` runs. Every module it imports
runs. A `.env` loader runs. Anything a repository's settings module chooses to
do at import time happens, with the file system and network access of whoever
typed the command.

This note is the security model for that, and the map of what the tier is for.
Where it makes a checkable claim — a constant, a name, a count — that claim is
verified by `scripts/check_live_doc.py`, which runs in the same gate as the
tests. A stale security note is worse than an absent one: absent, a reader goes
and looks; stale, they believe it.

## Why execute anything at all

Two rules cannot be answered any other way.

`DJM-010` classifies the SQL a migration actually emits. Django decides that
SQL from the migration graph, the model state, the database backend and the
installed Django version, and the only component that knows all four is Django
itself. Reading `AlterField(max_length=50)` tells you a column changed; only
`sqlmigrate` tells you whether the answer was `ALTER TABLE ... TYPE varchar(50)`
— which rewrites every row under `ACCESS EXCLUSIVE` — or nothing at all.

`DJS-028` audits our own recall. It runs `manage.py check --deploy` and reports
what Django found that we did not. A static settings evaluator resolves what it
can see; a second opinion from the framework is the only thing that can name
what it could not.

Every other rule in the corpus is static. Of 78 implemented rules, **2 declare
`Tier.LIVE`** — and both declare a `fallback` describing what is lost without
it, which is enforced for every live rule by the registry's tests.

## Consent is the flag, not a prompt

The live tier is off unless asked for. `--live` is the request, and it prints
what is about to happen and where before doing it: the interpreter and the
`manage.py`, both as absolute paths, because "target code will be executed" is
a warning label and `/home/you/proj/.venv/bin/python /home/you/proj/manage.py`
is a fact the reader can check.

It is deliberately **not** an interactive confirmation. This runs in CI far
more often than at a terminal, and a tool that blocks on a question nobody can
answer is a tool that gets run with `yes |` in front of it — which converts a
safety feature into a line of boilerplate that everybody copies and nobody
reads.

The notice goes to **stderr**. `--format json`, `--format sarif` and
`--format html` all write a document to stdout, and a security notice that
corrupts the document it is warning you about would be its own small joke.

A live tier that cannot start is not a reason to abandon the audit. `Consent`
carries the failure as data rather than raising, the static tier runs its full
corpus anyway, and the difference is reported as a `Degradation` rather than
being silently absent.

## What the subprocess is allowed

`src/djaudit/live/runner.py` is the only module in djaudit that executes the
code being audited. It is written on the assumption that the target is hostile
or — far more likely, and just as damaging — merely careless.

**No inherited secrets.** The child's environment is built from nothing, not
copied from ours. A CI job's environment holds deployment tokens, registry
credentials and cloud keys, and handing those to a subprocess that runs
arbitrary code from the repository being audited would make djaudit a
credential exfiltration path — a supply-chain vulnerability introduced by a
security tool. **10 variables** cross, in `PASSTHROUGH`, and they are the ones a
process needs to start at all:

`COMSPEC`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `PATH`, `PATHEXT`,
`SYSTEMROOT`, `TMPDIR`, `TZ`.

Worth naming because their absence is the point: `DJANGO_SETTINGS_MODULE` would
let our environment choose the target's settings, `PYTHONPATH` would let our
packages shadow theirs, and `DATABASE_URL` and everything like it must be
passed explicitly by a caller that means it.

`PASSTHROUGH` also excludes every `PG*` variable, which is a decision and not an
oversight. The live tier's job is to observe the target *as configured*; a
`PGDATABASE` leaking from our shell would silently redirect `sqlmigrate` at a
different database and produce SQL for a schema the project has never seen. The
target reads its connection from its own settings, which is the thing under
audit.

**Four variables a caller may not add at all**, in `REFUSED`: `PYTHONPATH`,
`PYTHONHOME`, `PYTHONSTARTUP`, `DJANGO_SETTINGS_MODULE`. The first two would let
djaudit's packages shadow the target's, which defeats the point of finding its
interpreter. `PYTHONSTARTUP` is executed before anything else runs.
`DJANGO_SETTINGS_MODULE` is refused for a subtler reason: a live rule's whole
job is to observe which settings the target resolves, and setting it here would
mean measuring our own answer.

**A hard timeout, enforced by killing the process group.** `DEFAULT_TIMEOUT` is
**30.0 seconds**. `subprocess.run`'s own `timeout` kills only the process it
started, and Django's `manage.py` spawns children — the autoreloader is the
obvious one. A child that outlives its parent inherits our pipes, so the read
after the kill blocks on a pipe nobody will ever close. The child gets its own
process group via `start_new_session=True` and the whole group is signalled, so
the timeout is a timeout rather than a suggestion.

**Output captured, bounded, never interleaved.** `OUTPUT_LIMIT` is **1 MiB**
per stream. A target that writes a gigabyte to stdout should not exhaust our
memory, and one that writes ANSI escapes should not be able to rewrite our
terminal. Truncation is recorded rather than hidden.

**stdin is closed.** A `manage.py` command that asks a question gets EOF rather
than blocking forever against a terminal that is not there. This is the failure
that most often looks like a hang, because it *is* one.

## Finding the right interpreter

Running the target under the wrong Python is not a degraded answer, it is a
wrong one: `check --deploy` under the wrong Django reports the wrong checks, and
`sqlmigrate` under the wrong backend emits the wrong SQL.

`src/djaudit/live/interpreter.py` identifies an environment by `pyvenv.cfg`,
which is what PEP 405 defines a virtual environment as, rather than by a
directory called `venv`. **Nothing in that module executes anything** —
detection is filesystem-only, so it is safe against a repository nobody has
vetted.

The failure mode that matters is the one that does not look like a failure.
djaudit normally runs from a virtualenv itself, so `$VIRTUAL_ENV` is usually set
and usually points at *ours*. Following it would run the target's `manage.py`
against our Django, our settings and our packages, and produce findings that
look completely ordinary. `$VIRTUAL_ENV` is therefore believed only when it
points inside the target, and any candidate resolving to `sys.prefix` is
rejected outright.

## What is trusted, and what is not

The target's **code** is untrusted; the target's **output** is evidence.

That distinction is the whole design. We do not parse the target's source and
believe our reading of it; we run Django and read what Django emitted, then
classify that text against a table of measurements. The classification is ours
and is checked against a real server. The SQL is theirs and is quoted verbatim
into the finding's evidence, so a reader can disagree with our conclusion
without having to trust it.

`src/djaudit/live/locks.py` holds **21 classification rules** mapping emitted
SQL to a `Lock` and a `Work`. Every one is verified against a real PostgreSQL by
`tests/live/test_locks.py`, which runs the statement, reads `pg_locks` from the
backend that took the lock, and fails if the table has gone stale. A coverage
gate in the same file fails by name if a rule is added without a measurement.

## Running the live tests

`DJAUDIT_TEST_POSTGRES` is a libpq DSN. When it is unset every test in
`tests/live` that needs a server skips itself and says so. CI sets it to a
`postgres:18` service container.

The CI password deliberately contains an `@` and a space. `urlparse` does not
percent-decode and libpq does, so a fixture that hands the raw value to Django
authenticates `psql` happily and fails every live test with what looks like a
server that is down. A plain password would never exercise that path.

Because skipping is the normal, quiet behaviour, the CI job would be decoration
without `scripts/live_gate.py`: a typo in the env block turns the job green
while running nothing, and pytest reports that as `0 failed` in the same words
as a full run. The gate reads the JUnit XML and fails on any skip, on an empty
collection, and on a collection too small to be the suite — because **zero
skipped is also true of a suite that never ran**.

## Limits

- The deployment check runs on every live audit, including when no `DJS` rule
  fired. It is not covered by the timing gate, which is static-only.
- `DJS-028` has no corpus triage entries, because the benchmarks run
  static-only against pinned checkouts with no database.
- `run_command`'s second `communicate(timeout=...)` after a terminate reuses the
  full timeout, so a pathological target can take up to twice the budget before
  the runner gives up.
- The live tier is PostgreSQL-first. `sqlmigrate` runs against whatever backend
  the target's settings name, but the lock table describes PostgreSQL, and
  nothing in it has been measured against MySQL.
