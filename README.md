# djaudit

Django-aware static analysis. Audits an existing Django codebase and reports
ranked, evidence-backed findings across settings hardening, injection, DRF
authorization, ORM performance, migration safety and cross-database portability.

**Status: Phase 0.** The engine, schema, fingerprinting, baseline, reporters and
CLI are in place with one rule (`DJS-001`) proving the pipeline end to end. The
rule catalogue is being built out next.

## What it is, and what it is not

This is not a trained model and does not call an LLM. Every finding is produced
by deterministic analysis of your source, and every finding carries evidence you
can check yourself.

An LLM layer is planned, but as a *consumer* of the finding schema — triage,
explanation, patch generation — not as the thing doing the detection. Anything
expressible as an AST rule is written as an AST rule: cheap, testable, and
incapable of hallucinating.

## Install

```bash
uv sync
uv run djaudit --help
```

## Use

```bash
uv run djaudit run /path/to/django/project

# CI: SARIF for GitHub code scanning
uv run djaudit run . --format sarif --output djaudit.sarif

# Adopting on an existing codebase: accept today's findings, fail only on new ones
uv run djaudit run . --write-baseline .djaudit-baseline.json
uv run djaudit run . --baseline .djaudit-baseline.json

uv run djaudit rules
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Nothing at or above `--fail-on` |
| `1` | Findings at or above `--fail-on` |
| `2` | The tool could not run — bad path, unreadable baseline, bad options |

"Found problems" and "tool broke" are separate codes so a pipeline can tell a
real failure from a broken install.

## Severity and confidence are separate axes

Severity is how much damage the finding does. Confidence is how sure we are it
is real. Collapsing the two is how static analysis tools earn a reputation for
noise, so they stay independent and both are filterable.

| Confidence | Meaning |
|---|---|
| `certain` | Provable from source or tool output |
| `firm` | Strong signal, one unverified assumption |
| `tentative` | Worth a human look; **excluded from CI gates by default** |

## Suppression

```python
DEBUG = True  # noqa: DJS-001
DEBUG = True  # djaudit: ignore[DJS-001] staging box, tracked in PROJ-412
```

A bare `# noqa` is deliberately ignored — those are usually left for some other
linter, and honouring them would silently hide security findings.

## Rule families

| Prefix | Family |
|---|---|
| `DJS` | Settings & deployment hardening |
| `DJI` | Injection & untrusted input |
| `DJA` | API / DRF authorization & data exposure |
| `DJP` | Performance & ORM efficiency |
| `DJM` | Migration safety |
| `DJX` | Cross-database portability |

## Analysis tiers

**Static** parses source with `ast` and never imports or executes the target, so
it is safe to point at untrusted code and works without the target's
dependencies installed.

**Live** runs inside the target's virtualenv for stronger evidence — real
settings resolution, `manage.py check --deploy`, `sqlmigrate` output, `EXPLAIN`.
Rules declare which tier they need and the tool degrades gracefully without it.

## Supported

Django 6.0 and 5.2 LTS · Python 3.12+ · PostgreSQL (psycopg3), with SQLite
understood as a source of dev/prod divergence.

## Development

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

Fixtures under `tests/fixtures/` contain deliberately vulnerable code and are
excluded from linting and type checking.
