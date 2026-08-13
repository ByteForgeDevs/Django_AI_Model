# Generating Django apps

`djaudit generate` asks a language model for a Django app, audits the result
with the same 87 rules the rest of the tool uses, hands the findings back to the
model, and audits again. It stops when the app is clean, when it stops
improving, or when it catches the model cheating.

The generator is a language model. The judge is not. That is the entire idea: a
model asked to review its own output reviews it with the faculty that produced
it, so the review agrees with the code. A deterministic analyser does not.

```console
$ djaudit generate "Posts with comments. Drafts are visible only to their \
    author; published posts are readable by any signed-in user." \
    --app blog --into ~/src/mysite --write
iteration 1: 7 finding(s) in blog
iteration 2: 0 finding(s) in blog
clean after 2 iteration(s), 7 finding(s) repaired

wrote 8 file(s) to /home/you/src/mysite/blog
Run `manage.py makemigrations` next. The audit is static: it proves the code
parses and clears the rules, not that it runs.
```

When it refuses, it says what it is refusing and writes nothing:

```console
$ djaudit generate "Posts with comments." --app blog --into ~/src/mysite --write
iteration 1: 7 finding(s) in blog
iteration 2: 0 finding(s) in blog
regressed: classes removed: Comment, CommentSerializer, CommentViewSet, Post,
PostSerializer, PostViewSet; fields removed: Comment.author, Comment.body,
Comment.created, Comment.post, Post.author, Post.body, Post.created,
Post.published, Post.slug, Post.title
$ echo $?
1
```

Iteration 2 scored zero findings there. It was still refused, because zero
findings and an empty app are the same measurement.

## Setting it up

Generation needs a model, so unlike every other command it needs credentials.
Configure them in the target project's `pyproject.toml`:

```toml
[tool.djaudit.llm]
enabled = true
provider = "openai"        # or "anthropic"
model = "gpt-4o"
api_key_env = "OPENAI_API_KEY"
max_calls = 8
```

The key is read from the named environment variable and never written to the
cache, the log, or a finding. djaudit refuses a config file that inlines
something key-shaped, and says which of its own rules that would violate.

`base_url` is optional and points at any OpenAI-compatible endpoint — a
self-hosted model, a gateway, a proxy, or a local server:

```toml
base_url = "http://127.0.0.1:8000/v1"
```

### Against a model on your own machine

No API key, no account, no per-token cost. Pull a coding model and point at it:

```console
$ ollama pull qwen2.5-coder:7b
```

```toml
[tool.djaudit.llm]
enabled    = true
provider   = "openai"                       # the wire format, not the company
model      = "qwen2.5-coder:7b"
base_url   = "http://127.0.0.1:11434/v1"
max_tokens = 6000
```

Note the absent `api_key_env`. djaudit normally refuses to run without one —
that rule is there to stop your source being posted to a remote host by a
misconfiguration nobody noticed — but a model on **loopback** is exempt,
because there is no egress to guard and no local runtime issues a key. The
exemption covers a literal loopback address or the exact name `localhost` and
nothing else: `10.0.0.5` is someone else's machine and still needs a key, and
hostnames are not resolved.

**Expect it to be slow.** Measured on eight CPU cores with no GPU, qwen2.5-coder
7B runs at about **2.6 tokens/second**, so a single answer of a few hundred
lines takes minutes and a full generate-audit-repair loop takes tens of them.
Loopback endpoints get a 900-second timeout for that reason, and `timeout` is
settable if your hardware disagrees. A GPU changes this by one to two orders of
magnitude.

Quality is the other trade. A 7B model writes plausible Django and makes
mistakes a frontier model would not — which is precisely why the audit runs
against its output rather than the model's own opinion of it. Observed on a
real run: correct models, correct fields, and one extra closing bracket.

A reply whose Python does not parse is sent back to be corrected, twice,
before the run is refused. Unparseable code is never written either way; the
retry only decides whether one stray character discards an otherwise correct
app. A suppression comment is *not* retried — that is not an accident, and
asking again only invites a subtler attempt.

Without configuration the command reports why and generates nothing. It does
not fall back to a stub or write an empty app.

## What it will not do

Three things go wrong in any loop that iterates until an auditor is silent.
None of them are prevented by asking the model nicely, so none of them are
prevented by asking the model nicely.

### It cannot write a file you did not ask for

The model is not asked for "the files". It is asked for exactly five named
string fields — `models_py`, `serializers_py`, `views_py`, `urls_py`,
`admin_py` — and the reply is validated against that declaration before any of
it reaches a filesystem call. A reply carrying a sixth field is a
`SchemaViolationError`.

So a model cannot return `settings.py`, or `manage.py`, or
`../../etc/cron.d/anything`. Not because the path is sanitised afterwards —
because there is no field to put it in. The remaining files an app needs
(`__init__.py`, `apps.py`, `migrations/__init__.py`) are boilerplate with no
decisions in them, so djaudit writes them itself.

### It cannot delete the feature to clear the finding

Every finding is in code, so deleting the code clears every finding. An empty
`models.py` passes all 87 rules. A model asked to fix `DJD-002` on a nullable
`CharField` can drop the field; asked to fix `DJA-002` on an unauthenticated
viewset, it can drop the viewset. Both produce a clean audit and neither is a
repair.

djaudit extracts what each version of the app *declares* — model, serializer,
view and admin class names, and each model's fields — and refuses an iteration
that declares less. The refusal names the casualty:

```
regressed: classes removed: Order; fields removed: Order.reference
```

Names, not counts: a model that deletes `Order` and adds `OrderAudit` keeps the
count and has still lost the feature. A rename reads as a deletion, deliberately
— a generator that renames your models mid-repair is doing something you should
see.

### It cannot silence the rule

`# djaudit: ignore` is the second degenerate optimum. The prompt says not to,
and the loop checks, because a prompt is a request.

## When it stops

| Outcome | Meaning | Written? |
|---|---|---|
| `clean` | No findings at or above your threshold | yes |
| `stalled` | An iteration cleared nothing, so another call would not either | yes |
| `exhausted` | The iteration ceiling was reached with findings outstanding | yes |
| `regressed` | An iteration removed a feature to silence a finding | **no** |
| `suppressed` | The model wrote a suppression comment instead of a fix | **no** |
| `unparseable` | The model returned code that is not valid Python | **no** |
| `declined` | No model was available, or it refused | **no** |

`stalled` and `exhausted` are written because findings a model cannot fix are
still a working app a human can. `regressed` and `suppressed` are not, because
accepting one means writing code you asked for and did not get.

Only one of the three stopping conditions is a counter. A counter alone stops a
runaway loop and tells you nothing; convergence and regression are the two that
carry information.

## Nothing is written until you say so

Every iteration materialises into a disposable copy of your project, made by the
same `scratch_copy` the fix verifier has used since Phase 6. A run that fails
leaves your project byte-identical. `--dry-run` is the default; `--write` is
what commits an accepted run.

The app is audited **inside a copy of the real project**, not in isolation, and
that is the point: half the rules need the settings module, the installed apps
and the model graph to say anything at all. Findings outside the generated app
are withheld from the model, which was asked for one app and cannot fix your
settings module.

## What "clean" does and does not prove

Clean means: no findings at or above your threshold, from 87 deterministic
rules, in a copy of your real project.

It does not mean the app works. It does not mean the app does what you asked.
It does not mean there are no security defects — only that there are none of the
kinds djaudit knows about. Nothing here runs the generated code: the loop never
imports it and never imports your settings, for the same reason the static tier
never does. There is no `--check`, and adding one would mean executing code a
model just wrote.

Run the generated app's tests. Read the diff. This is a tool that removes a
class of defect, not one that removes the need to look.

## Measured

`scripts/generation_probe.py` against `benchmarks/generation.json`:

```
app         naive  final  cleared  outcome
--------------------------------------------------------
billing         7      0        7  clean
blog            7      0        7  clean
support         6      0        6  clean
--------------------------------------------------------
3 apps, 20 findings before, 20 cleared, 0 after
unaudited defect density: 6.7 findings per app
control: repair-by-deletion refused on 3 of 3 apps
```

**Read the provenance before believing the number.** No vendor API was called to
produce that corpus. `tests/fixtures/generation/*/naive/` is Django written by a
language model with no auditor in the room; `*/repaired/` is the same model's
second pass with the finding list in front of it. Both are recorded as fixtures
and replayed through the real loop, the real engine and the real refusals.

That measures two things honestly — the defect density of unaudited LLM Django,
and whether the loop's mechanics carry an app from that state to zero findings
without losing a feature. It does not measure whether a particular vendor's
model produces the repaired version on demand. That needs a key and a bill, and
the gate does not pretend otherwise.

The defects in the naive corpus are the ordinary ones: `fields = "__all__"` on
every serializer, viewsets with no `permission_classes`, querysets that return
every row regardless of who asked, a `CharField(null=True)`, and one
`%`-formatted SQL string spliced from `request.query_params`. Nothing exotic.
That is what makes 6.7 findings per app worth reporting.

The control is the part that matters. Every app is also driven against a gutted
version — the empty files that clear every finding by deleting the feature — and
the gate fails if the loop accepts one. Disabling the regression check makes all
three apps report `clean` and get written, and the gate turns red. Without that
control, a loop that always returned `clean` would score 100%.

## How this was verified

Every claim above is checked by `tests/test_generate.py` and
`scripts/generation_probe.py`, which drive the real loop with recorded replies.
Those never open a socket, so they cannot prove the transport.

So the command was also run end to end against a local OpenAI-compatible server
replaying the corpus: real config parsing, a real socket, a real
`Authorization` header, a real `response_format: json_schema` request, real
schema validation, a real audit, and a real write. Both paths were exercised —
the repair that lands, and the gutted reply that is refused with nothing
written.

That run found a defect no unit test had: `base_url` was documented and passed
through to the provider, but the config parser never populated it. The request
went to `api.openai.com` instead of the endpoint the file named. It failed
silently in the one direction that matters — pointing djaudit at a private
model would have sent your source to a public vendor — and it is fixed, with
tests, in `tests/llm/test_config.py`.

## Related

- [Getting started](getting-started.md) — auditing an existing project
- [Configuration](configuration.md) — the full `[tool.djaudit]` table
- [Serving djaudit over MCP](mcp.md) — the same rules, driven by a coding agent
