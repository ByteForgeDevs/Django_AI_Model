# Getting started

This is the path from "I have a Django repository" to "the build fails on new
findings and nobody has uninstalled it". The order matters more than the
commands: the mistake that kills a static analyser on a legacy codebase is
turning it on at full strength on day one, watching it print four thousand
findings, and never running it again.

Every command on this page is checked by `scripts/check_docs_commands.py`,
which asks click whether each subcommand and flag actually exists. A rename
that breaks this page fails the build.

## Install

```
$ uv tool install djaudit
```

`pipx install djaudit` and `pip install djaudit` work too. djaudit analyses
your project's source; it does not import it, and it does not need your
project's dependencies installed. It can run from anywhere.

There is also a container, if you would rather not install anything — see
[container.md](container.md).

## Look before you configure

```
$ djaudit run .
```

The default is deliberately quiet: `medium` severity and above, `firm`
confidence and above. That is not the whole picture, and you should look at the
whole picture once before deciding what to do about it:

```
$ djaudit run . --min-severity info --min-confidence tentative
```

The gap between those two numbers is the tool's own uncertainty, and it is
worth seeing. Findings at `tentative` are usually a value djaudit could not
resolve statically — a setting behind an environment variable, a queryset built
somewhere it could not follow. They are not noise, but they are not evidence
either.

If either run says something like `1 diagnostic`, read it before reading the
findings. A blocking diagnostic means djaudit could not fully read the project,
and a run it could not read is not a clean run. It exits non-zero rather than
reporting success on a project it did not understand.

## Understand one finding before you fix any

```
$ djaudit explain DJP-001
```

Every finding carries a rule id, a `file:line`, machine-generated evidence, and
a remediation. `explain` prints the rule's full reasoning, what it looks for,
and what it deliberately does not claim. The rule reference in
[rules/README.md](rules/README.md) is the same material for every rule at once, generated from
the rules themselves so it cannot drift.

Confidence and severity are separate axes and are not interchangeable. A
`critical` finding at `tentative` is "if this is what it looks like, it is very
bad"; a `low` finding at `certain` is "this is definitely true and mostly does
not matter". Filter on both.

## Adopt: accept today, fail on tomorrow

This is the step that makes the tool survivable on a codebase with history.

```
$ djaudit run . --min-severity info --min-confidence tentative --write-baseline .djaudit-baseline.json
$ git add .djaudit-baseline.json && git commit -m "chore: baseline djaudit"
```

Commit the baseline. From now on:

```
$ djaudit run . --baseline .djaudit-baseline.json
```

reports only what is *new*. The existing four thousand are recorded, not fixed
and not forgotten — the run still tells you how many it is holding, because a
tool that hides findings silently is the thing this tool exists to catch.

Write the baseline at the *widest* thresholds you might ever use, not at your
current ones. A baseline written at `--min-severity high` does not contain the
`medium` findings, so the day you lower the threshold every one of them arrives
as new.

Baselines survive refactoring. Fingerprints are computed from the rule, the
file and a normalised snippet — not the line number — so moving code around, or
adding an import at the top of a file, does not invalidate them. They do not
survive a `FINGERPRINT_VERSION` bump; [versioning.md](versioning.md) says when
that happens and the changelog says so loudly when it has.

## Turn it on in CI

```
$ djaudit run . --baseline .djaudit-baseline.json --format sarif -o djaudit.sarif
```

Upload that to GitHub code scanning and findings appear inline on the pull
request. The packaged Action does this for you, including uploading the SARIF
even when the run fails the job — see [github-action.md](github-action.md).
There is a pre-commit hook too, in [pre-commit.md](pre-commit.md).

Choose what fails the build separately from what gets reported:

```
$ djaudit run . --baseline .djaudit-baseline.json --fail-on high
```

`--fail-on` is about the build; `--min-severity` is about the report. Reporting
everything and failing on `high` is a good default, because it keeps the
`medium` findings visible without blocking anyone.

## Then tune, in this order

Reach for these when the defaults are wrong for your project, and prefer the
earliest one that works.

1. **Suppress one line** you have looked at and decided about:

   ```python
   cursor.execute(query)  # djaudit: ignore DJI-001 -- query is a module constant
   ```

   Narrowest possible scope, and the reason stays next to the code.

2. **Exclude a path** whose findings you never want to hear about — vendored
   code, generated migrations, a test suite that fakes things on purpose:

   ```toml
   [tool.djaudit]
   exclude_paths = ["tests", "*/migrations/*"]
   ```

   Exclusions filter findings by location. The files are still parsed, because
   your models live in files you might exclude and every cross-file rule
   depends on them.

3. **Re-rank a rule** your project genuinely weighs differently:

   ```toml
   [tool.djaudit.severity]
   "DJD-002" = "info"
   ```

   Applied before thresholds, so it changes what is reported and what fails the
   build. It does not change fingerprints, so it will not invalidate your
   baseline.

4. **Turn a rule off** entirely, when it does not apply to your architecture:

   ```toml
   [tool.djaudit]
   ignore = ["DJX-004"]
   ```

All of it is in [configuration.md](configuration.md). Every setting has a
command-line equivalent, and the command line always wins.

## What it will not do

It will not import your code, run your code, connect to your database or reach
the network — not in the default tier. The live tier (`--live`) does run
`manage.py` inside your project's environment, and external tool adapters
(`--external`) shell out to ruff and pip-audit. Both are opt-in flags for that
reason, and neither can be switched on from a configuration file, because a
configuration file arrives with the repository you were asked to analyse.
