# Writing a rule

A rule is a class with metadata and a `check` method. There is no plugin
loader, no entry point and no configuration step: a rule is registered by
importing the module that defines it, and `djaudit/rules/__init__.py` imports
every module in the package.

This page is checked by `scripts/check_authoring_doc.py`, which executes the
code below against the real API and runs the finished rule over a real project.
An example that stopped working fails the build.

## The shape

Every rule declares a `RuleMeta` and implements `check`, which is a generator.
Returning nothing is normal and common — most rules say nothing about most
projects.

```python
from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Location, Severity, Tier
from djaudit.registry import Rule, RuleMeta, register


@register
class AssertInProductionCode(Rule):
    """``assert`` is removed by ``python -O``, so it cannot enforce anything."""

    meta = RuleMeta(
        id="DJS-900",
        title="assert used to enforce a runtime condition",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Assertions are stripped when Python runs with -O, which some "
            "deployment images set. A permission check written as an assert "
            "then silently passes for everyone."
        ),
        remediation="Raise an explicit exception instead of asserting.",
        references=("https://docs.python.org/3/reference/simple_stmts.html#the-assert-statement",),
        limitations=(
            "Reads source, not the interpreter's flags. A project that never "
            "runs with -O is unaffected, and this rule cannot tell.",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            tree = ctx.parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assert):
                    continue
                yield self.finding(
                    location=ctx.location(path, node),
                    message="This assert disappears under `python -O`.",
                    evidence=(
                        Evidence(
                            kind=EvidenceKind.AST,
                            content=ast.unparse(node),
                            source=str(path),
                        ),
                    ),
                )
```

## What each field is for

| Field | Why it exists |
|---|---|
| `id` | `FAMILY-NNN`. Must start with the family it declares; `register` refuses otherwise. Never reuse one — ids appear in baselines. |
| `title` | The heading in `docs/rules/`. A noun phrase naming the defect, not the fix. |
| `family` | Which page the rule lands on and which `--family` selects it. |
| `severity` | What happens if the finding is real. |
| `confidence` | How sure djaudit is that it is real. Independent of severity. |
| `tier` | `Tier.STATIC` reads source only. `Tier.LIVE` needs the project's environment and only runs under `--live`. |
| `rationale` | Why it matters, for a reader who has not met this defect. Rendered into every finding. |
| `remediation` | The concrete fix. Code in it is the point, not decoration. |
| `references` | Django docs, CVE or OWASP URLs. |
| `fallback` | What to do instead when the rule cannot run at all. |
| `fallback_rules` | Rule ids that cover part of the same ground when this one is unavailable. |
| `limitations` | What the rule cannot see, in the reader's terms. Required, and checked. |

`limitations` is the field most likely to be skipped and the one most worth
writing. A confidence level is a number, and a number does not tell somebody
staring at a finding why it might not apply to them.

## Severity and confidence are not the same axis

`severity` answers "if this is real, how bad is it". `confidence` answers "is it
real". They move independently, and collapsing them produces a ranking nobody
can act on. A `critical` finding at `tentative` is the first thing to read and
the first thing to verify before changing anything. A `low` finding at
`certain` is true and probably fine.

Default output shows `firm` and above, so a `tentative` rule is invisible until
somebody asks for it. That is the correct home for a rule that is right most of
the time.

## Per-finding overrides

`self.finding()` fills severity, confidence, rationale and remediation from
`meta`, and each can be overridden for one finding. Use this when the same
defect grades differently by context — a hardcoded secret in a settings module
that looks production-reachable is worse than the same literal in a test.

```python
class GradedExample(AssertInProductionCode):
    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            yield self.finding(
                location=ctx.location(path, ast.parse("x")),
                message="Hardcoded secret in a production-reachable module.",
                severity=Severity.CRITICAL,
                confidence=Confidence.FIRM,
            )
```

Grade down rather than staying silent. A rule that emits nothing when it is
unsure is a rule nobody can tune.

## Evidence

Every finding should carry machine-generated evidence, never a restatement of
the message. The kinds are `EvidenceKind.AST`, `EvidenceKind.SQL`,
`EvidenceKind.COMMAND_OUTPUT` and `EvidenceKind.CONFIG`. `ast.unparse` on the
offending node is the usual choice for a static rule; a live rule that ran
`sqlmigrate` should carry the SQL it read.

## What the context gives you

`ProjectContext` is built once per run and shared by every rule, so parsing is
paid for once.

- `ctx.python_files` — every Python file discovered under the root.
- `ctx.parse(path)` — the parsed module, or `None` if it did not parse. Cached.
- `ctx.source(path)`, `ctx.lines(path)` — the text, cached.
- `ctx.location(path, node)` — a `Location` with line, column, end line and the
  source snippet, built from the node's own positions.
- `ctx.settings_modules` — the settings modules discovery found, with roles.
- `ctx.model_graph()` — models, fields, relations and `Meta`.
- `ctx.api_surface()` — serializers, viewsets and permission classes.
- `ctx.live_context` — present only under `--live`.

Never import the target, never execute it, and never assume its dependencies
are installed. A static rule must be safe on code nobody has read.

## Registering

`@register` validates the id's shape, refuses a duplicate, and refuses an id
whose prefix disagrees with the declared family. Put the class in a module
under `src/djaudit/rules/`; the package imports every module in it, so there is
no list to update.

## Proving it works

A rule is not finished when it fires. It is finished when something fails if it
stops firing, and something else fails if it fires on correct code.

1. **Add a planted defect to a fixture.** `tests/fixtures/` holds small Django
   projects with an `expected.json` manifest. Add the defect and the expected
   entry, then `djaudit eval tests/fixtures/<project>` scores recall against it.
2. **Add the control next to it.** Every fixture pairs a defect with a
   correctly-written twin, and the manifest forbids any finding in the control
   file. Detecting that a loop mentions a relation is easy and worthless; the
   question is whether the fetch that covers it is present.
3. **Check the control is reachable.** `scripts/fixture_controls_probe.py`
   removes the fix from each control and requires the rule to then report it. A
   control the rule cannot reach passes for the wrong reason, and that is
   indistinguishable from the rule being broken.
4. **Run it on the benchmark corpus.** `djaudit benchmark` over healthchecks,
   netbox and pretix measures precision. These are mature projects, so nearly
   anything reported is a false positive.
5. **Write the limitation you found.** Whatever made you lower the confidence
   belongs in `limitations` in the same commit.

Generated documentation follows automatically: `scripts/gen_rule_docs.py`
rebuilds `docs/rules/` and its index from `RuleMeta`, and CI fails if the
committed copies are stale.

## Where rules live

| Path | Contents |
|---|---|
| `src/djaudit/rules/` | One module per rule or closely-related group. |
| `src/djaudit/rules/_base.py` | Shared bases, e.g. `SettingsRule` for settings lookups. |
| `tests/fixtures/` | Planted-defect projects and their manifests. |
| `docs/rules/` | Generated. Do not edit by hand. |

Related reading: [configuration.md](configuration.md) for turning rules off and
re-ranking them, and [rules/README.md](rules/README.md) for the catalogue.
