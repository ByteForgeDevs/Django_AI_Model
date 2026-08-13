# The LLM layer: what it may do, and what it structurally cannot

This note is the argument behind `src/djaudit/llm/`. The short version is on
the package's first line — *a consumer of findings, never a producer of them* —
and the rest of this file is why that sentence is a shape rather than a promise.

Every number here is measured. Where a claim rests on a test, the test is
named, so a reader can check that the guarantee is enforced rather than
described.

## Why the layer exists at all

The deterministic engine answers "what is wrong here". It does not answer
"which of these findings should I read first", "what will this cost my
team", or "what does the fixed version look like". Those are judgement calls,
and a static rule that tried to make them would be guessing with the authority
of a compiler.

So the split is: rules decide *what is true*, the model layer decides *what is
worth your attention*. The first is checkable; the second is not, which is
exactly why it must be labelled and exactly why it is kept out of the finding
list.

## The structural guarantee

**No code path in `djaudit/llm/` can add a finding to a run or remove one from
it.** Not "does not" — *cannot*, in the sense that there is no function to call.

Three things make that true rather than aspirational:

1. **The engine finishes before the layer starts.** `engine.run()` returns a
   `RunResult`, and every function under `llm/` takes findings as input. There
   is no hook, no callback, no registry a model can enter through.

2. **`ResponseSchema.validate` raises on an undeclared field.** A caller
   declares the shape of the answer before asking. A reply carrying
   `{"is_defect": false}` when nobody asked about `is_defect` is refused
   loudly, rather than having the key dropped — dropping it would keep it out
   of the finding list *and* leave nothing behind, so a provider that started
   returning it would go unnoticed.

3. **Suppression requires a model verdict, and even then only proposes.**
   `suggest` will draft a `# djaudit: ignore` comment, and `fix` will draft a
   patch, but neither writes to disk. The operator applies it, or does not.

`tests/llm/test_hostile_provider.py` attacks all three by driving the layer
with a provider engineered to lie, and requires the finding list to come out
byte-identical.

## The pipeline

```mermaid
flowchart LR
    E[engine.run<br/>deterministic] --> F[findings + evidence]
    F --> T[triage.py<br/>rank]
    F --> X[explain.py<br/>prose]
    F --> I[impact.py<br/>framing]
    F --> FX[fix.py<br/>patch]
    T --> G[group.py<br/>themes]
    T --> S[suggest.py<br/>suppressions]
    FX --> V[verify.py<br/>scratch copy]
    T -.provenance.-> R[reporters]
    F --> R
    P[provider.py<br/>Cached · Metered · Null] -.only these three.-> T
    P -.-> X
    P -.-> I
```

The dotted edges matter: **only `triage`, `explain` and `impact` ever touch a
provider.** `fix`, `edit` and `verify` do not, and cannot — see
*Fixes need no model*, below.

## The provider stack

`Cached(Metered(inner))`, in that order and for a reason. The cache is
outermost so a repeat question never reaches the meter at all. `Metered` also
refuses to charge for a cached answer, but not consulting the budget is cheaper
than consulting it and then forgiving it, and it leaves the cache free to tag
each entry with its finding's fingerprint.

`inner` is a `NullProvider` in every configuration that ships today. **No
third-party API is ever called from tests or from CI**, and there is no
implemented adapter that could be. `NullProvider` *declines*, which is a return
value rather than an exception, because a stack that silently does nothing is
indistinguishable from one that is broken.

## What the model is never permitted to do

This list is binding. Each entry names what enforces it.

| The model may not | Enforced by |
|---|---|
| Create a finding | No API accepts one; `test_hostile_provider.py` |
| Delete or hide a finding | `suggest` only drafts text; nothing writes to disk |
| Change a severity, confidence or fingerprint | Findings are frozen before the layer sees them |
| Return a field nobody asked for | `ResponseSchema.validate` raises |
| Author a code change | `FIXERS` values come from rule remediation text; `tests/llm/test_fixes_need_no_model.py` |
| Be consulted without the operator opting in | `LLMConfig.usable`; off unless both `--llm` and `[tool.djaudit.llm]` agree |
| Spend without a ceiling | `Budget(max_tokens, max_calls)`, checked before each call |
| Have its output mistaken for a rule's | `provenance.py`; every result carries `authorship` |
| Cause the target's code to be executed | The test command is never auto-detected; `verify.py` |

## Fixes need no model

`djaudit fix` writes four settings and no others: `DJS-001`, `DJS-008`,
`DJS-011`, `DJS-018`. Those are the only rules of 27 whose correct value is
decided by the rule rather than by the project — `DEBUG = False` is not a
judgement call.

Every fixer must write a value its own rule's `remediation` text names
verbatim. That is the **agreement gate**, and it is necessary but not
sufficient: `DJS-007` names `31536000` outright and would pass it, yet the same
remediation says to ramp HSTS up rather than jump, because `max-age` is sticky.
It is excluded deliberately, and `test_hsts_is_deliberately_not_fixable` pins
the reasoning so a later reader does not "fix the gap".

That no model is involved is enforced three ways, in
`tests/llm/test_fixes_need_no_model.py`: a transitive walk of the import graph
from `fix`, `edit` and `verify`; the CLI's provider factory replaced with one
that raises; and the socket layer closed while the whole path runs. The socket
closure is the strong one, because it assumes nothing about *how* a model would
be reached.

## Failure modes

These are the ways this layer can be wrong. They are documented because the
alternative is users discovering them by being misled.

### The corpus prior is small

`CORPUS_PRIOR` settles findings from 7 rules over 121 human-reviewed findings,
and only where the review was unanimous across at least
`MINIMUM_OBSERVATIONS = 5` findings. That is a genuine record, and it is three
projects' worth. A rule not in the table is not settled, and the honest answer
for it is `abstained` — which is the default, offline, for everything the table
does not cover.

**Consequence:** the corpus generalises from Healthchecks, NetBox and pretix.
A codebase unlike all three may be settled wrongly. `scripts/triage_baselines.py`
exists to keep the prior honest: it fails if any reading-free baseline (always
true-positive, always accepted-risk, coin-flip) clears 75% in both directions,
which would mean the prior is not carrying information.

### Verification is static unless told otherwise

`--verify` establishes four things: the patch applies, the result parses, the
targeted finding is gone, and no new finding appeared. That is a real claim and
it is **not** "safe to merge" — nothing has been executed.

The test command is **never auto-detected**. Detecting `manage.py test` and
running it would mean executing the audited project, which the static tier
exists not to do: importing a settings module runs whatever that module runs.
`Level.STATIC` and `Level.TESTED` are separate values so a static pass can
never be printed as a tested one.

**Consequence:** a `--verify` pass with no `--test-command` says nothing about
runtime behaviour. A patch can be structurally perfect and still break the
application.

### Explanations cannot see the code

`explain` never reads the target's source. It works from the finding and its
evidence, which is what keeps it from re-describing code the engine already
summarised, and what keeps a redacted snippet redacted.

**Consequence:** an explanation cannot notice project-specific context that
makes a finding irrelevant. It explains the *rule*, grounded in this
occurrence.

### Impact framing counts; it does not estimate

`impact` templates contain **no digits**. Only numbers counted from the run may
appear. There is no cost model, no breach-probability figure, no
industry-average anything — those would be invented, and an invented number in
a security report is worse than no number.

**Consequence:** the framing tells you who is affected and what class of cost
applies. It will not tell you the cost.

### Secrets can leak through a diff's context

A patch's *context* lines are code the fixer did not change and never looked
at. A live `SECRET_KEY` two lines above a `DEBUG` fix was republished in full
by an early version. `safe_context` narrows the context window rather than
masking, because a masked line makes the patch unappliable.

**Consequence:** the window narrows from 3 lines to as few as 1 near a secret.
The patch stays valid; the diff is harder to read. That trade is deliberate.

### Ranged edits are unavailable 0.21% of the time

Across 3,159 files and 86,783 assignment values in the benchmark corpus,
slicing a value by its `ast` position and re-parsing it reproduced the tree
exactly **86,601 times (99.790%)**. All 182 failures share one shape: a
parenthesised multi-line expression whose enclosing parens `ast` excludes.

That failure is detectable from inside — parse the slice, compare `ast.dump` —
so `verified_span` returns `None` rather than a wrong range. The edit is not
"99.79% safe"; it is safe, and *available* 99.79% of the time.

**Consequence:** a small number of otherwise-fixable findings will be refused
with a reason rather than patched. That is the correct trade.

### Grouping can hide a disagreement

`group` collapses findings of one rule in one file into a theme. When the
verdicts within a theme disagree, the theme reports `None` rather than a
majority — but a reader skimming themes still sees fewer rows than there are
findings.

**Consequence:** themes are a reading aid, not a work list. The ranked view is
the work list.

## Running a model on your own machine

The layer talks to any server that speaks the OpenAI chat-completions shape,
which includes ollama, llama.cpp's server, vLLM and LM Studio. Point `base_url`
at it and leave the key out:

```toml
[tool.djaudit.llm]
enabled   = true
provider  = "openai"
model     = "qwen2.5-coder:7b"
base_url  = "http://127.0.0.1:11434/v1"
max_tokens = 6000
```

Three things differ from a vendor, and each was a defect before it was a
documented difference.

**No credential is required, but only on loopback.** `LLMConfig.usable`
normally refuses to run without a configured key. That rule exists to stop
source code being posted to a remote host by a misconfiguration nobody
noticed — it is about egress, not about authentication. A model on loopback
has no egress to guard and no local runtime issues a key, so applying the rule
literally locked the tool out of every model a user can run for free while
leaving the threat it guards against untouched. The exemption is deliberately
narrow: a literal loopback address or the exact name `localhost`. A private
address like `10.0.0.5` is someone else's machine and still needs a key, and
hostnames are **not** resolved, because what DNS answers here need not be what
it answers at request time.

**The token cap is spelled differently.** OpenAI renamed `max_tokens` to
`max_completion_tokens`; local servers implement the original name and ignore
the new one *without complaining*. Measured against ollama 0.6: a cap of 5 sent
as `max_completion_tokens` returned 647 tokens and `finish_reason: stop`, while
the same cap as `max_tokens` returned 5 and `length`. The wrong name is not an
error, it is a budget that silently does nothing. `Endpoint.token_field` picks
the spelling from the address.

**It is much slower, and the default timeout assumed otherwise.** qwen2.5-coder
7B on eight CPU cores with no GPU was measured at **2.6 tokens/second** — 703
tokens in 268 seconds. The vendor-shaped default of 120 seconds times out
mid-answer and then retries twice, turning one slow success into a six-minute
failure. Loopback endpoints therefore get 900 seconds instead, and `timeout`
is settable in the config table for anyone whose hardware disagrees.

Structured output does work: ollama honoured `response_format: json_schema`
with `strict: true` and returned schema-valid JSON.

## What has not been checked

Stated plainly, because the sections above would otherwise imply more coverage
than exists:

- `ResponseSchema.as_json_schema()` has now been exercised against a **live**
  OpenAI-compatible endpoint (ollama), which accepted the strict json_schema
  request and answered within it. It has still never been sent to OpenAI or
  Anthropic themselves; for those two it is checked against their published
  documents only.
- The corpus prior has been measured on three projects. Three is enough to
  refute "any rule can be settled" and not enough to claim generality.
- A real provider has now been driven end to end, but a *local* one. Its
  failure modes are not a vendor's: no rate limits, no 429s, no quota
  exhaustion, no server-side content filtering. Those paths are still modelled
  by `NullProvider` and the hostile provider rather than observed.
- The generated code has been audited by the 87 rules, which is a statement
  about the defects those rules cover and not about whether the app is good.
