# djaudit

Django-aware static analysis. Audits an existing Django codebase and reports
ranked, evidence-backed findings across settings hardening, injection, DRF
authorization, ORM performance, migration safety and cross-database portability.

**Status: Phase 1 complete.** 27 settings-hardening rules (`DJS-001`…`DJS-027`)
on top of the Phase 0 engine — schema, fingerprinting, baseline, three
reporters, CLI and eval harness.

Measured, in CI, on every commit:

| | Result |
|---|---|
| Recall, on five planted-defect fixtures | **100%** — 38 expected findings, 0 missed |
| Precision, on the same fixtures | **100%** — 0 false positives |
| Precision, on Healthchecks (653 files) | **100%** — 10 reported, all confirmed on review |
| Precision, on NetBox (1213 files) | **100%** — 6 reported, all confirmed on review |
| Crashes on either target | **0** rule errors |
| Runtime | under a second on NetBox's 1213 files |

Precision is measured against two mature, well-audited open-source Django
projects pinned to a commit SHA and cloned in CI, never vendored. They cannot
measure recall — we have no way to know what we missed in code we did not write
— so recall comes from fixtures with a manifest of expected findings. Every
finding reported on a real target is triaged in `benchmarks/`, with a written
justification, a reviewer and a date; `scripts/check_triage.py` fails the build
if an entry is unreviewed, because scoring your own precision benchmark is
otherwise how a project ends up with 100% and no credibility.

Ten of the sixteen are `accepted_risk`: the setting really is off, and the
project has a reason — a redirect handled at the proxy, a value supplied by the
deployment. That verdict counts as a true positive here, because the rule
correctly reported what it can see. What it cannot see is written down for
every rule in [`docs/rules/DJS.md`](docs/rules/DJS.md).

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

| Prefix | Family | Status |
|---|---|---|
| `DJS` | Settings & deployment hardening | **27 rules** — [reference](docs/rules/DJS.md) |
| `DJI` | Injection & untrusted input | Phase 3 |
| `DJA` | API / DRF authorization & data exposure | Phase 2 |
| `DJP` | Performance & ORM efficiency | Phase 3 |
| `DJM` | Migration safety | Phase 4 |
| `DJX` | Cross-database portability | Phase 5 |

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
uv run pytest              # 1031 tests
uv run ruff check .
uv run mypy                # strict, on our own code only

# recall, against fixtures with a manifest of expected findings
uv run djaudit eval tests/fixtures/vulnerable_project

# precision, against a real project, scored by the triage file
uv run djaudit benchmark /path/to/netbox --triage benchmarks/netbox.json
```

Fixtures under `tests/fixtures/` contain deliberately vulnerable code and are
excluded from linting and type checking. Two of the five contain no defects at
all: `overridden_project` checks that a safe override silences a rule, and
`near_miss_project` is a correct project written entirely in shapes a sloppy
rule would flag — it is the guard against precision decaying as rules are added.

`docs/rules/DJS.md` is generated from the rule registry by
`scripts/gen_rule_docs.py`; edit the rule, not the page. CI checks the two
agree, along with the plan's own arithmetic (`scripts/check_plan.py`) and the
completeness of the triage files (`scripts/check_triage.py`).
