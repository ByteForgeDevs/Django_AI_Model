#!/usr/bin/env python
"""Render one page per rule family under docs/rules/ from the registry.

Rule documentation written by hand goes stale, and stale security
documentation is worse than none: it describes a rule that no longer behaves
that way, and the reader has no way to tell. Every field in the page already
exists on the rule -- title, grade, rationale, remediation, references and
limitations -- so the page is generated and CI fails if the committed copy is
not what the code currently says.

Only the family blurb is written by a human, because it is the one thing no
rule knows: what the family is *for*. Everything else is transcribed. A family
with rules and no blurb is an error rather than a page with a gap, so adding a
family cannot quietly ship an unexplained page.

``--check`` verifies without writing, which is what CI runs.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from djaudit import registry
from djaudit.models import Family
from djaudit.registry import Rule

DOCS = Path(__file__).resolve().parent.parent / "docs" / "rules"


@dataclass(frozen=True)
class Blurb:
    """The part of a family page that the rules cannot supply."""

    heading: str
    """What follows the family id in the page title."""

    summary: str
    """One paragraph on what the family covers. ``{count}`` is substituted."""

    scope: str
    """What the family reads, and what it therefore cannot see."""


BLURBS: dict[Family, Blurb] = {
    Family.DJS: Blurb(
        heading="settings and deployment hardening",
        summary=(
            "{count} rules covering the settings that decide whether a Django deployment is\n"
            "safe to expose: what it admits about itself, what it puts on the wire, what it\n"
            "keeps out of the repository, and what it leaves switched on that should not be."
        ),
        scope=(
            "Every rule here is **static**. Nothing is imported and nothing is executed, so\n"
            "the analysis works on a checkout with none of the target's dependencies\n"
            "installed — and it never sees the deployment. That boundary is why each rule\n"
            "carries a *limitations* section, and why confidence is a first-class field\n"
            "rather than a footnote."
        ),
    ),
    Family.DJA: Blurb(
        heading="API authorization and data exposure",
        summary=(
            "{count} rules on the two questions a REST API answers on every request: who is\n"
            "allowed to ask, and what comes back. They read Django REST Framework's own\n"
            "vocabulary — routers, viewsets, `permission_classes`, `get_queryset`,\n"
            "serializer `Meta`, filter backends, throttles — and follow it from the URL\n"
            "that reaches a view through to the model field that leaves in the response."
        ),
        scope=(
            "Every rule here is **static**, and that boundary bites harder than it does for\n"
            "`DJS`, because authorization is the part of a codebase most often decided at\n"
            "runtime. A permission named in settings may be replaced per view; a queryset\n"
            "may be narrowed by a mixin three classes up; `validate()` may reject exactly\n"
            "the request the serializer appears to accept. Where the answer lives somewhere\n"
            "the source does not say, these rules lower confidence rather than guess —\n"
            "several ship `tentative` by design and stay invisible at the default threshold\n"
            "until asked for."
        ),
    ),
    Family.DJD: Blurb(
        heading="data model and schema design",
        summary=(
            "{count} rules on the model layer itself — what a column is allowed to hold, what\n"
            "happens to a row when the row it points at is deleted, and what order a query\n"
            "returns when nobody asked for one. These are not vulnerabilities. They are the\n"
            "decisions that are cheap to change while the table is empty and expensive\n"
            "afterwards."
        ),
        scope=(
            "Every rule here is **static** and reads the model graph: field declarations and\n"
            "their arguments, `Meta`, and inheritance within the project. It does not read\n"
            "migrations, so it cannot tell a column that has always been nullable from one\n"
            "made nullable last week, and it does not read the database, so it cannot say\n"
            "how many rows actually hold the value it is describing."
        ),
    ),
    Family.DJI: Blurb(
        heading="injection and untrusted input",
        summary=(
            "{count} rules on the boundary between text the project wrote and text the\n"
            "client sent. Every one of them is about the same mistake in a different\n"
            "costume: a value that should have travelled beside a command ends up inside\n"
            "it, and something that was meant to be data is read as syntax."
        ),
        scope=(
            "Every rule here is **static**, and each one reports *reach* rather than shape.\n"
            "Composing a SQL string is not a defect — a table name cannot be a query\n"
            "parameter, so interpolating one is sometimes the only way to write the query.\n"
            "What these rules look for is a spliced value that can be traced back to\n"
            "something Django filled from the request.\n"
            "\n"
            "That tracing is a three-valued analysis, and the third value is the point.\n"
            "Besides *safe* and *tainted* there is *unknown*: a helper's own parameter\n"
            "holds whatever its caller passed, and this analysis does not read callers.\n"
            "Unknown is not reported. It is the most common shape of a real defect of this\n"
            "kind and also the most common shape of perfectly correct code, and nothing in\n"
            "one function's text distinguishes them — so a rule that reported it would be\n"
            "reporting its own ignorance, once per helper."
        ),
    ),
    Family.DJP: Blurb(
        heading="performance and ORM efficiency",
        summary=(
            "{count} rules on the queries a Django project makes without meaning to. The ORM\n"
            "makes the expensive thing and the cheap thing look identical: `book.author.name`\n"
            "is an attribute access whether the author arrived with the book or costs its own\n"
            "round trip, and the source gives no indication which. Every rule here reports a\n"
            "*query count*, not a slow query -- a slow query shows up in monitoring with its\n"
            "SQL attached, while a thousand fast ones show up as a view that got slower for\n"
            "no visible reason."
        ),
        scope=(
            "Every rule here is **static**, and each one needs two things to agree: the model\n"
            "graph, to know that an attribute crosses a relation rather than reading a column\n"
            "already in the row, and local dataflow, to know that the name being read is a row\n"
            "of a queryset and which model that queryset holds. Where either is unavailable\n"
            "the rule stays silent rather than guessing -- with the model graph emptied, this\n"
            "family reports nothing at all on any of the three benchmark projects. The\n"
            "analysis is confined to a single function, so a queryset built in one function\n"
            "and iterated in another is not followed: what is reported here is a floor and\n"
            "never a total."
        ),
    ),
    Family.DJX: Blurb(
        heading="cross-database portability",
        summary=(
            "{count} rules on the gap between the database a developer runs and the one\n"
            "that serves requests. Nothing here is a vulnerability and nothing here fails\n"
            "at import time. These are the defects that pass the whole test suite and then\n"
            "fail on production data, because the test suite ran against the other engine."
        ),
        scope=(
            "This family reports only what it can **read**. An `ENGINE` written as a literal\n"
            "string is evidence; one computed at runtime, or set through a dict imported\n"
            "from elsewhere, is not, and the difference decides whether a rule fires. Of the\n"
            "three benchmark projects only Healthchecks states its engines outright --\n"
            'SQLite by default, Postgres and MySQL behind `os.getenv("DB")` -- and it is the\n'
            "only one this family reports on. pretix concatenates its backend name from a\n"
            "config file and NetBox assigns `ENGINE` through a later `.update()`, so both\n"
            "resolve as unreadable.\n"
            "\n"
            "That restraint is deliberate. Asking *could* this project be on SQLite, rather\n"
            "than *is* it, answers yes for every project whose settings resist analysis, and\n"
            "would turn two Postgres-only codebases into a page of portability findings\n"
            "about a database neither of them runs."
        ),
    ),
    Family.DJM: Blurb(
        heading="migration safety",
        summary=(
            "{count} rules on what a migration does to a running database at the moment it\n"
            "is applied: a lock it holds, a table it rewrites, a statement that aborts part\n"
            "way through. These are the defects that pass every test and fail only on\n"
            "production data, because the table is empty in CI and the lock nobody waits on\n"
            "is free."
        ),
        scope=(
            "This family reports only on migrations that have **not yet been applied**, and\n"
            "that restriction is what makes it usable rather than a preference. A migration\n"
            "sitting in a project's history demonstrably ran, so a finding against it is not\n"
            "merely unactionable -- it is false. Reporting across the 875 migrations in the\n"
            "three benchmark projects would have produced roughly 1600 findings with a\n"
            "true-positive rate of zero.\n"
            "\n"
            "The live tier answers this exactly, by reading `django_migrations`. The static\n"
            "tier cannot, so it reports the leaf of each app's history -- the tip, where a\n"
            "migration being written now lands. That is a heuristic and every rule says so in\n"
            "its limitations: it over-reports a leaf that shipped long ago, and misses the\n"
            "first of two migrations added together. Because recall cannot be measured on a\n"
            "shipped project for the reason above, it is measured against\n"
            "`tests/fixtures/migration_project` instead, where each defect has a\n"
            "correctly-written twin that must stay silent."
        ),
    ),
}

HEADER = """<!--
Generated by scripts/gen_rule_docs.py. Do not edit by hand.

Every field below lives on the rule itself, so the way to change this page is
to change the rule and run the script. CI checks the two agree.
-->

# `{family}` — {heading}

{summary}

{scope}

## How to read a grade

**Severity** is what happens if the finding is real. **Confidence** is how sure
we are that it is. They move independently: an unencrypted database session is
serious whether or not we can prove it happens, and a setting we read exactly
may still be overridden by an environment we cannot see.

| Confidence | Meaning |
|---|---|
| `certain` | Read directly, and nothing outside the repo changes what it means. |
| `firm` | Read directly, on one ordinary assumption — usually that this module ships. |
| `tentative` | The value, or its consequence, depends on something the source lacks. |

The CLI shows `low` severity and `firm` confidence and above by default, so a
`tentative` finding is deliberately invisible until asked for:

```console
$ djaudit run . --min-severity info --min-confidence tentative
```

## Rules

| Rule | Title | Severity | Confidence |
|---|---|---|---|
{index}
"""

SECTION = """
---

### {id} — {title}

**Severity** {severity} · **Confidence** {confidence} · **Tier** {tier}

**What it means.** {rationale}

**How to fix it.** {remediation}

**What this rule cannot see.**

{limitations}

**References**

{references}
"""


def slug(rule_id: str, title: str) -> str:
    """Reproduce GitHub's heading anchor for ``### {id} — {title}``.

    GitHub lowercases, drops punctuation other than hyphen and underscore, and
    turns each remaining space into a hyphen. The em dash disappears and the
    spaces on either side of it survive, which is why every anchor here has a
    double hyphen after the rule id.
    """
    text = f"{rule_id} — {title}".lower()
    kept = "".join(c if (c.isalnum() or c in "-_ ") else "" for c in text)
    return kept.replace(" ", "-")


def documented_families() -> dict[Family, list[type[Rule]]]:
    """Families that have rules, keyed in the taxonomy's own order."""
    registry._load_builtin_rules()
    found: dict[Family, list[type[Rule]]] = {}
    for rule in sorted(registry.all_rules(), key=lambda r: r.meta.id):
        found.setdefault(rule.meta.family, []).append(rule)
    return {family: found[family] for family in Family if family in found}


def render(family: Family, rules: list[type[Rule]]) -> str:
    blurb = BLURBS.get(family)
    if blurb is None:
        raise SystemExit(
            f"RULE DOCS: {family.value} has {len(rules)} rules and no blurb in "
            "scripts/gen_rule_docs.py. A page cannot say what a family is for "
            "unless somebody writes it."
        )

    index = "\n".join(
        f"| [`{r.meta.id}`](#{slug(r.meta.id, r.meta.title)}) "
        f"| {r.meta.title} | {r.meta.severity.value} | {r.meta.confidence.value} |"
        for r in rules
    )

    body = "".join(
        SECTION.format(
            id=r.meta.id,
            title=r.meta.title,
            severity=r.meta.severity.value,
            confidence=r.meta.confidence.value,
            tier=r.meta.tier.value,
            rationale=r.meta.rationale,
            remediation=r.meta.remediation,
            limitations="\n".join(f"- {item}" for item in r.meta.limitations),
            references="\n".join(f"- <{url}>" for url in r.meta.references),
        )
        for r in rules
    )

    return (
        HEADER.format(
            family=family.value,
            heading=blurb.heading,
            summary=blurb.summary.format(count=len(rules)),
            scope=blurb.scope,
            index=index,
        )
        + body
    )


INDEX_HEADER = """<!--
Generated by scripts/gen_rule_docs.py. Do not edit by hand.
-->

# Rule reference

{total} rules across {families} families. Every field here lives on the rule
itself; the way to change this page is to change the rule.

Findings name a rule id, so the task this page exists for is looking one up.
`djaudit explain DJP-001` prints the same material in the terminal, and
[../configuration.md](../configuration.md) covers turning rules off, re-ranking
them and excluding paths.

**Severity** is what happens if the finding is real. **Confidence** is how sure
djaudit is that it is. They are independent: a `critical` finding at
`tentative` is worth reading first and proving before acting on, and a `low`
finding at `certain` is true and probably fine.

**Tier** is `static` for a rule that only reads source -- never importing,
never executing, safe on code you have not read -- and `live` for one that
needs the project's own environment and is only available under `--live`.

## Families

| Family | Rules | Subject |
|---|---|---|
{family_rows}

## Every rule

| Rule | Title | Severity | Confidence | Tier |
|---|---|---|---|---|
{rows}
"""


def render_index(families: dict[Family, list[type[Rule]]]) -> str:
    """One table of every rule, because a finding names an id and nothing else."""
    family_rows = "\n".join(
        f"| [`{family.value}`]({family.value}.md) | {len(rules)} | {BLURBS[family].heading} |"
        for family, rules in families.items()
    )
    rows = []
    for family, rules in families.items():
        for rule in rules:
            meta = rule.meta
            anchor = f"{family.value}.md#{slug(meta.id, meta.title)}"
            rows.append(
                f"| [`{meta.id}`]({anchor}) | {meta.title} | {meta.severity.value} "
                f"| {meta.confidence.value} | {meta.tier.value} |"
            )
    return INDEX_HEADER.format(
        total=sum(len(rules) for rules in families.values()),
        families=len(families),
        family_rows=family_rows,
        rows="\n".join(rows),
    )


def pages() -> dict[Path, str]:
    families = documented_families()
    rendered = {
        DOCS / f"{family.value}.md": render(family, rules) for family, rules in families.items()
    }
    rendered[DOCS / "README.md"] = render_index(families)
    _check_anchors(rendered)
    return rendered


def _check_anchors(rendered: dict[Path, str]) -> None:
    """Every anchor the index emits has to land on a heading that exists.

    `slug` reimplements GitHub's anchor rules, so it is a guess about another
    system's behaviour that nothing else would notice going wrong -- a broken
    fragment still resolves as a link to the page. Both sides are generated
    here in the same run, so they can simply be compared.
    """
    index = rendered[DOCS / "README.md"]
    for line in index.splitlines():
        if ".md#" not in line:
            continue
        target = line.split("](", 1)[1].split(")", 1)[0]
        page, _, anchor = target.partition("#")
        body = rendered.get(DOCS / page)
        if body is None:
            raise SystemExit(f"RULE DOCS: the index links to {page}, which is not generated")
        headings = set()
        for heading in _headings(body):
            rule_id, sep, title = heading.partition(" — ")
            if not sep:
                raise SystemExit(
                    f"RULE DOCS: heading {heading!r} in {page} is not "
                    "'{id} — {title}', which is the shape slug() anchors assume"
                )
            headings.add(slug(rule_id, title))
        if anchor not in headings:
            raise SystemExit(
                f"RULE DOCS: the index anchor {target} does not match any heading in {page}"
            )


def _headings(body: str) -> list[str]:
    return [line[4:].strip() for line in body.splitlines() if line.startswith("### ")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args()

    rendered = pages()

    if args.check:
        stale = [
            path
            for path, text in rendered.items()
            if (path.read_text(encoding="utf-8") if path.exists() else "") != text
        ]
        # A page for a family that no longer has rules is as wrong as a stale
        # one, and deleting the last rule of a family is exactly when nobody
        # thinks to look in docs/.
        orphaned = sorted(p for p in DOCS.glob("*.md") if p not in rendered)
        for path in stale:
            print(
                f"RULE DOCS STALE: {path.relative_to(DOCS.parent.parent)} does not match "
                "the registry; run scripts/gen_rule_docs.py",
                file=sys.stderr,
            )
        for path in orphaned:
            print(
                f"RULE DOCS ORPHANED: {path.relative_to(DOCS.parent.parent)} documents a "
                "family with no rules",
                file=sys.stderr,
            )
        if stale or orphaned:
            return 1

        families = {p: t for p, t in rendered.items() if p.stem != "README"}
        counted = ", ".join(f"{p.stem} {t.count('\n### ')}" for p, t in families.items())
        total = sum(text.count("\n### ") for text in families.values())
        indexed = rendered[DOCS / "README.md"].count(".md#")
        print(f"rule docs current: {total} rules documented ({counted}), {indexed} indexed")
        return 0

    DOCS.mkdir(parents=True, exist_ok=True)
    for path, text in rendered.items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(DOCS.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
