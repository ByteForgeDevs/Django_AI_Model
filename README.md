# djaudit

Django-aware static analysis. Audits an existing Django codebase and reports
ranked, evidence-backed findings across settings hardening, injection, DRF
authorization, ORM performance, migration safety and cross-database portability.

**Status: Phases 0–8 complete.** 87 rules — 28 settings-hardening (`DJS`), 15 API
authorization and data exposure (`DJA`), 12 injection and untrusted input
(`DJI`), 10 ORM performance (`DJP`), 10 migration safety (`DJM`), 9
cross-database portability (`DJX`), 3 data model design (`DJD`) — on top of a
Django model graph, a DRF route graph, a migration graph and a taint-tracking
dataflow pass built entirely from source.

Measured, in CI, on every commit:

| | Result |
|---|---|
| Recall, on eleven planted-defect fixtures | **100%** — 104 expected findings, 0 missed |
| Precision, on the same fixtures | **100%** — 0 false positives, against 169 near-miss shapes that must stay silent |
| Precision, on Healthchecks (653 files) | **100%** — 41 reported, all reviewed |
| Precision, on NetBox (1213 files) | **100%** — 76 reported, all reviewed |
| Precision, on pretix (1225 files) | **100%** — 151 reported, all reviewed |
| Model graph coverage, against each target's own migrations | **12/12**, **144/145**, **103/113** models — every gap attributed |
| Crashes on any target | **0** rule errors |
| Runtime | 3.8s on Healthchecks, 20s on NetBox, 25s on pretix |

Precision is measured against three mature, well-audited open-source Django
projects pinned to a commit SHA and cloned in CI, never vendored. They cannot
measure recall — we have no way to know what we missed in code we did not write
— so recall comes from fixtures with a manifest of expected findings. Every
finding reported on a real target is triaged in `benchmarks/`, with a written
justification, a reviewer and a date; `scripts/check_triage.py` fails the build
if an entry is unreviewed, because scoring your own precision benchmark is
otherwise how a project ends up with 100% and no credibility.

Sixty-three of the 268 verdicts are `accepted_risk`: the finding is accurate and
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

**Detection is not a model and never calls one.** Every finding is produced by
deterministic analysis of your source, and every finding carries evidence you
can check yourself. Anything expressible as an AST rule is written as an AST
rule: cheap, testable, and incapable of hallucinating.

There is now an LLM layer, but strictly as a *consumer* of the finding schema —
triage, explanation, patch proposal (`djaudit triage`, `explain`, `fix`) — and a
generator that is judged by the rules rather than trusted (`djaudit generate`).
It is off unless you configure it, and no model can create, withdraw or reword a
finding. `djaudit run` never opens a socket.

Every statement the tool emits is labelled with what authored it, so a
model-written sentence can never be mistaken for a rule-written one. See
[`docs/architecture/llm-layer.md`](docs/architecture/llm-layer.md).

## Run it from a local checkout

Everything below is the development path — no published package, no container,
just the repository. Python 3.12+ and [uv](https://docs.astral.sh/uv/) are the
only prerequisites.

```bash
git clone https://github.com/ByteForgeDevs/Django_AI_Model.git
# or, for a private repo you already have access to:  gh repo clone ByteForgeDevs/Django_AI_Model
cd Django_AI_Model
uv sync
```

`uv sync` creates `.venv/` and installs the project with its dev dependencies.
Nothing else needs installing — djaudit reads your project's source and never
imports it, so your Django project's own dependencies are irrelevant to it.

### Confirm it works, without leaving the repo

The repository ships deliberately vulnerable Django projects under
`tests/fixtures/`, so you can see real output before pointing it at anything of
your own:

```bash
uv run djaudit run tests/fixtures/vulnerable_project
```

You should get 24 findings — 3 critical, 6 high, 11 medium, 4 low — plus `5
below threshold`, in about 0.3s. **It exits `1`, and that is correct:** findings
at or above `--fail-on` are a non-zero exit so CI can gate on them. `2` means
the tool itself could not run.

To check that against the fixture's manifest of expected findings rather than
eyeballing it:

```bash
uv run djaudit eval tests/fixtures/vulnerable_project
```

That prints `precision 100.00% recall 100.00% f1 100.00% tp=29 fp=0 fn=0` and
`evaluation passed`. If it does not, the checkout is broken and nothing below
will mean anything.

### Point it at your own project

```bash
uv run djaudit run /path/to/your/django/project
```

Give it the directory containing `manage.py`. It finds the settings module
itself; if it cannot, it says so and exits `2` rather than reporting a
reassuring zero.

Useful from here:

```bash
# see every rule, with severity, confidence and tier
uv run djaudit rules

# the whole picture, including what the default threshold hides
uv run djaudit run /path/to/project --min-severity info --min-confidence tentative

# only the things worth stopping a deploy for
uv run djaudit run /path/to/project --fail-on high

# machine-readable
uv run djaudit run /path/to/project --format json --output findings.json
```

Every finding prints a `fingerprint`. Pass one back to `explain` to get the
full rationale, the remediation, and what else in that file is wrong for the
same reason:

```bash
uv run djaudit explain 31f91131 tests/fixtures/vulnerable_project
```

A prefix is enough as long as it is unique. `explain` takes a *fingerprint*,
not a rule ID — for what a rule does in general, see
[`docs/rules/`](docs/rules/README.md).

### Put `djaudit` on your PATH

If typing `uv run` from inside the checkout gets old:

```bash
uv tool install .
djaudit run /path/to/your/project
```

That installs the built artifact rather than running from source, so re-run it
after pulling changes — an installed copy goes stale silently the moment a
command is added.

### Generating an app (needs credentials)

`djaudit generate` is the only command that talks to a vendor. It needs a
`tool.djaudit.llm` table in the **target project's** `pyproject.toml`:

```toml
[tool.djaudit.llm]
enabled = true
provider = "openai"          # or "anthropic"
model = "gpt-4o"
api_key_env = "OPENAI_API_KEY"
max_calls = 8
```

The key is read from the environment variable you name — djaudit refuses a
config that inlines something key-shaped. Add `base_url` to point at any
OpenAI-compatible endpoint instead (a gateway, a proxy, a self-hosted model):

```toml
base_url = "http://127.0.0.1:8000/v1"
```

Then, from the checkout:

```bash
export OPENAI_API_KEY=...

# dry run: generates and audits, writes nothing
uv run djaudit generate "orders placed by customers, with line items" \
    --app shop --into /path/to/your/project

# same thing, but keep the result
uv run djaudit generate "orders placed by customers, with line items" \
    --app shop --into /path/to/your/project --write
```

`--dry-run` is the default, so the first invocation on a real project cannot
damage it. Without configuration the command explains why and generates
nothing — it does not fall back to a stub or write an empty app.

`--into` must be an **existing Django project** — a directory with a
`manage.py`. That is not a formality: the generated app is audited inside a
scratch copy of that project, because half the rules need its settings module
and model graph to say anything at all. Pointing it at an empty directory exits
`2` and tells you so.

Run `manage.py makemigrations` afterwards. The audit is static: it proves the
code parses and clears 87 rules, not that it runs.

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

## Have it write the app in the first place

```bash
djaudit generate "orders placed by customers, with line items" \
  --app shop --into ~/src/mysite --write
```

`djaudit generate` asks a model for a Django app, audits the result with the
same 87 rules, hands the findings back, and audits again. It needs credentials;
nothing else in djaudit does.

The loop has three degenerate optima and closes each one structurally rather
than by asking the model nicely. It cannot write a file you did not ask for,
because the response schema declares exactly five and rejects a sixth — so
`settings.py` and `../../etc/cron.d/anything` have nowhere to go. It cannot
delete the feature to clear the finding, because djaudit compares what each
version declares and refuses an iteration that declares less. It cannot write
`# djaudit: ignore`.

Nothing is written until a run is accepted, and `--dry-run` is the default. See
[`docs/generate.md`](docs/generate.md), including what "clean" does and does not
prove.

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
| `DJS` | Settings & deployment hardening | **28 rules** — [reference](docs/rules/DJS.md) |
| `DJA` | API / DRF authorization & data exposure | **15 rules** — [reference](docs/rules/DJA.md) |
| `DJI` | Injection & untrusted input | **12 rules** — [reference](docs/rules/DJI.md) |
| `DJP` | Performance & ORM efficiency | **10 rules** — [reference](docs/rules/DJP.md) |
| `DJM` | Migration safety | **10 rules** — [reference](docs/rules/DJM.md) |
| `DJX` | Cross-database portability | **9 rules** — [reference](docs/rules/DJX.md) |
| `DJD` | Data model design | **3 rules** — [reference](docs/rules/DJD.md) |

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
uv run pytest              # 5670 tests
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
excluded from linting and type checking. Their manifests carry 169
`must_not_report` entries alongside the 104 expected findings, because a rule
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
