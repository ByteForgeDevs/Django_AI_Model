# djaudit

Django-aware static analysis. Audits an existing Django codebase and reports
ranked, evidence-backed findings across settings hardening, injection, DRF
authorization, ORM performance, migration safety and cross-database portability.

**Status: Phase 3 in progress.** 67 rules — 27 settings-hardening (`DJS`), 15 API
authorization and data exposure (`DJA`), 12 injection and untrusted input
(`DJI`), 10 ORM performance (`DJP`), 3 data model design (`DJD`) — on top of a
Django model graph, a DRF route graph and a taint-tracking dataflow pass built
entirely from source.

Measured, in CI, on every commit:

| | Result |
|---|---|
| Recall, on eight planted-defect fixtures | **100%** — 83 expected findings, 0 missed |
| Precision, on the same fixtures | **100%** — 0 false positives, against 121 near-miss shapes that must stay silent |
| Precision, on Healthchecks (653 files) | **100%** — 33 reported, all reviewed |
| Precision, on NetBox (1213 files) | **100%** — 71 reported, all reviewed |
| Precision, on pretix (1225 files) | **100%** — 141 reported, all reviewed |
| Model graph coverage, against each target's own migrations | **12/12**, **144/145**, **103/113** models — every gap attributed |
| Crashes on any target | **0** rule errors |
| Runtime | 1.7s on Healthchecks, 6s on NetBox, 15s on pretix |

Precision is measured against three mature, well-audited open-source Django
projects pinned to a commit SHA and cloned in CI, never vendored. They cannot
measure recall — we have no way to know what we missed in code we did not write
— so recall comes from fixtures with a manifest of expected findings. Every
finding reported on a real target is triaged in `benchmarks/`, with a written
justification, a reviewer and a date; `scripts/check_triage.py` fails the build
if an entry is unreviewed, because scoring your own precision benchmark is
otherwise how a project ends up with 100% and no credibility.

Fifty-one of the 245 verdicts are `accepted_risk`: the finding is accurate and
the project has a reason — a value supplied by the deployment, a guard the
static tier cannot see, a bearer credential the endpoint exists to redeem. That
counts as a true positive here, because the rule correctly reported what it can
see. What it cannot see is written down for every rule in
[`docs/rules/`](docs/rules/README.md).

### The N+1 numbers, stated plainly

The N+1 rules are the ones worth being sceptical about, so here is what we
actually know about them rather than a single percentage.

Across the three targets the family reports **56 N+1 findings**, and every one
of them names a queryset that really is missing a `select_related` or
`prefetch_related`. None was withdrawn on review.

**Two of the 56 overstate their cost, and we count that against ourselves.**
Both are in NetBox's `cables.py`, where a relation is read twice on the same
queryset inside one function. The second read is free: `QuerySet._fetch_all`
returns the cached `_result_cache` instead of re-querying, and the forward
foreign-key descriptor checks `field.get_cached_value(instance)` before it
touches the database — both verified in Django's source, not assumed. The
missing `select_related` is real and fixing it fixes both lines, but the second
finding's "one query per row" is not true where it is written. That is a **3.6%
redundant-report rate** (2/56). It is not netted off the precision figure and it
is not rounded away.

Two things that number does *not* mean. It is not an independent audit: we
triaged our own benchmark, which is why every verdict carries a written
justification, a reviewer and a date, and why `scripts/check_triage.py` fails
the build on an unreviewed entry. And it is not a claim about recall on real
code — we cannot know which N+1s NetBox contains that we walked straight past.
Recall is measured only where we planted the defects ourselves.

Reporting nothing also scores 100% precision, so a second gate asks a different
question against a different oracle: `scripts/graph_coverage.py` replays each
target's own migrations and checks how much of its model layer we actually
found. Every missing field must be attributed to a named cause — NetBox's 61
are 55 inherited from `MPTTModel`, 4 from `AbstractBaseUser` and 2 from
`TagBase`, all ancestors in site-packages that a source-only reader cannot
open. An unexplained gap fails the build. See
[`docs/architecture/model-graph.md`](docs/architecture/model-graph.md).

The `DJP` and `DJI` families rest on a taint-tracking dataflow pass whose limits
are written down rather than discovered:
[`docs/architecture/dataflow.md`](docs/architecture/dataflow.md) states what it
cannot see — no path sensitivity, no cross-module flow, no aliasing through
containers or attributes, one interprocedural hop within a module — with a
verified example of each staying silent.

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
| `2` | The tool could not run — bad path, unreadable baseline, bad options — or ran without covering enough of the project to be trusted |

"Found problems" and "tool broke" are separate codes so a pipeline can tell a
real failure from a broken install.

Incomplete analysis shares code `2` rather than passing quietly. If djaudit
cannot locate a settings module in something that plainly is a Django project —
a hand-rolled settings class applied by some project-specific loader, for
instance — it says so and exits non-zero. A clean report from a run that read
nothing is more dangerous than no report at all, so it is not offered as one.

## Use it from a coding agent

```bash
uv run djaudit mcp
```

`djaudit mcp` serves the analyser to an AI coding agent over the
[Model Context Protocol](https://modelcontextprotocol.io), so the agent can
audit the Django code it just wrote and repair it before you see the diff. You
do not run this yourself — a client launches it.

The point is that a model reviewing its own output uses the faculty that
produced it, so the blind spot applies twice. An independent parser does not
share it. See [`docs/mcp.md`](docs/mcp.md) for client configuration.

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
| `DJA` | API / DRF authorization & data exposure | **15 rules** — [reference](docs/rules/DJA.md) |
| `DJD` | Data model design | **3 rules** — [reference](docs/rules/DJD.md) |
| `DJI` | Injection & untrusted input | Phase 3 |
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
uv run pytest              # 1614 tests
uv run ruff check .
uv run mypy                # strict, on our own code only

# recall, against fixtures with a manifest of expected findings
uv run djaudit eval tests/fixtures/vulnerable_project

# precision, against a real project, scored by the triage file
uv run djaudit benchmark /path/to/netbox --triage benchmarks/netbox.json

# coverage, against that project's own migrations
uv run python scripts/graph_coverage.py /path/to/netbox \
    --name netbox --expect benchmarks/graph/netbox.json
```

Fixtures under `tests/fixtures/` contain deliberately vulnerable code and are
excluded from linting and type checking. Their manifests carry 97
`must_not_report` entries alongside the 56 expected findings, because a rule
that fires on the wrong thing fails a fixture the same way a missing rule does.
`overridden_project` checks that a safe override silences a rule, and
`near_miss_project` is a correct Django project written entirely in the shapes
a sloppy rule would flag — it is the guard against precision decaying as rules
are added, and it found three false positives in `DJA` while being written.

Every page under `docs/rules/` is generated from the rule registry by
`scripts/gen_rule_docs.py`; edit the rule, not the page. CI checks they agree,
along with the plan's own arithmetic (`scripts/check_plan.py`), the
completeness of the triage files (`scripts/check_triage.py`) and the model
graph's coverage of each benchmark target (`scripts/graph_coverage.py`).
