"""DJX -- the same code, two databases.

`DJX-001` is the meta-finding this family is built on. On its own it reports
nothing exploitable: a project that runs SQLite locally and Postgres in
production is a normal, defensible arrangement, and plenty of teams do it
deliberately. What it establishes is that every other `DJX` rule is *relevant*,
because from here on "it works on my machine" is a statement about a different
database engine than the one serving users.

It deliberately does not subclass `SettingsRule`. That base skips modules whose
role does not reach production, which is right for a hardening rule -- a
development module is *supposed* to have `DEBUG = True` -- and exactly wrong
here. The development module is not an exception to this finding, it is one
half of it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import TYPE_CHECKING

from djaudit.context import ProjectContext
from djaudit.engines import Divergence, EngineChoice, Vendor, module_name
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)
from djaudit.registry import Rule, RuleMeta, register
from djaudit.rules._injection import Frame, own_calls

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.dataflow.scopes import Scope
    from djaudit.graph.nodes import ConstraintNode, FieldNode, ModelNode

_DATABASES_DOCS = "https://docs.djangoproject.com/en/stable/ref/settings/#databases"
_BACKENDS_DOCS = "https://docs.djangoproject.com/en/stable/ref/databases/"


def _describe(default: EngineChoice, choice: EngineChoice) -> str:
    """One alternative, said the way it is reached.

    A divergence inside one module is reached by setting something, and a
    divergence across two is reached by deploying the other module. Saying
    "with nothing set" about both -- which is literally true of each within its
    own file -- would read as a contradiction.
    """
    if choice.module.dotted != default.module.dotted:
        return f"{choice.engine} in {choice.module.dotted}"
    return f"{choice.engine} {choice.selected_by()}"


def _series(parts: list[str], conjunction: str) -> str:
    """`a`, `b` and `c` -- a list said the way a sentence says one."""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


def _joined(default: EngineChoice, choices: tuple[EngineChoice, ...]) -> str:
    """`a`, `b` or `c` -- the alternatives, as prose."""
    return _series([_describe(default, c) for c in choices], "or")


class DivergenceRule(Rule):
    """Base for a rule that only applies when two backends are evidenced.

    The gate is :meth:`Divergence.reaches` rather than
    :meth:`Divergence.could_reach`: this family's claim is that a divergence is
    a thing you can point at, and "we could not read the engine" is not that.
    NetBox and pretix both compute their `ENGINE`, so a possibility-based gate
    would fire every rule below on two projects that have never run SQLite --
    which is how a family earns its way into a permanent ignore list.

    The consequence is deliberate and worth stating: on the benchmark corpus
    only Healthchecks passes this gate, so recall for the whole family is
    measured on `tests/fixtures/portability_project` and nowhere else.
    """

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        found = ctx.divergence
        if not (found.reaches(Vendor.SQLITE) and found.reaches(Vendor.POSTGRESQL)):
            return
        yield from self.diverging(ctx, found)

    def diverging(self, ctx: ProjectContext, found: Divergence) -> Iterator[Finding]:
        raise NotImplementedError


@register
class EngineDivergence(Rule):
    """The project runs one database in development and another in production.

    Reported once per project rather than once per branch. Healthchecks
    reaches three engines from a single alias, and three findings saying the
    same thing would be three times the noise for one decision.
    """

    meta = RuleMeta(
        id="DJX-001",
        title="development and production run different database engines",
        family=Family.DJX,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "SQLite and Postgres disagree about more than speed. SQLite's `LIKE` folds "
            "ASCII case and Postgres' does not, so `contains`, `startswith` and "
            "`endswith` match different rows on each; the two sort text under "
            "different collations, so `order_by` on a name column returns a different "
            "order -- measured, SQLite answers Apple, Banana, apple and Postgres "
            "answers apple, Apple, Banana; SQLite does not enforce `max_length`, so a "
            "value that truncates in one and raises in the other passes every local "
            "test; `distinct('field')`, `ArrayField` and the JSON containment "
            "operators exist only on Postgres. None of these fail at import time and "
            "none are visible in a diff. They fail in production, against real data, "
            "on code that passed the whole test suite -- because the test suite ran "
            "against the other database. That is what makes this worth reporting even "
            "though nothing here is a vulnerability: it converts 'works locally' from "
            "evidence into a coincidence."
        ),
        remediation=(
            "Run the same engine everywhere -- a container or a managed development "
            "instance costs less than one production-only bug. Where that is not "
            "possible, run the test suite against the production engine in CI so the "
            "divergence is exercised before a deploy rather than after, and treat the "
            "rest of the DJX findings on this project as live rather than theoretical."
        ),
        references=(_DATABASES_DOCS, _BACKENDS_DOCS),
        limitations=(
            "Only engines written as literal strings are read. A project that "
            "computes its ENGINE, or sets it through a dict imported from elsewhere, "
            "reports as unreadable and is not flagged here even if it does diverge.",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        found = ctx.divergence
        if not found.diverges:
            return
        default, alternatives = found.default, found.alternatives
        if default is None:
            # Every branch is opt-in, so no engine is the one a developer gets
            # by doing nothing, and there is no development half to contrast.
            # `alternatives` is non-empty whenever this is not: a divergence is
            # two known vendors on one alias, so a default with a vendor cannot
            # be the only one and a default without one differs from both.
            return
        yield self._report(ctx, found, default, alternatives)

    def _report(
        self,
        ctx: ProjectContext,
        found: Divergence,
        default: EngineChoice,
        alternatives: tuple[EngineChoice, ...],
    ) -> Finding:
        module = default.module
        where = module_name(module)
        # No constructed shape reaches the fallback: an alias whose value has
        # no node of its own is one djaudit resolved rather than read, and its
        # ENGINE is then unreadable too, so it never survives to here with a
        # vendor. The type still admits None, so the fallback stays.
        node = default.node or default.definition.node
        return self.finding(
            location=ctx.location(default.definition.module, node),
            message=(
                f"the {found.alias!r} database in {where} is {default.engine} "
                f"{default.selected_by()} and {_joined(default, alternatives)}, so "
                f"the engine a developer runs is not the engine that serves "
                f"requests -- every query in this project is written against one "
                f"and executed against the other"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    content=ctx.snippet(default.definition.module, node.lineno, node.end_lineno),
                    source=ctx.rel(default.definition.module),
                ),
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content="\n".join(
                        f"{c.describe()} -- reached {c.selected_by()}" for c in found.relevant
                    ),
                    source="djaudit database engine model",
                ),
            ),
            properties={
                "settings_module": module.dotted,
                "settings_role": module.role.value,
                "alias": found.alias or "",
                "vendors": " ".join(sorted(found.vendors)),
            },
        )


class QueryRule(DivergenceRule):
    """Base for a `DJX` rule about a call written on a queryset.

    Matching on the method name alone would report `set().distinct()` and any
    project's own helper of the same name, so a candidate is kept only when the
    queryset tracker recognises the chain it sits on. The textual prefilter is
    not an optimisation detail: without it every file in the project is parsed
    and scoped to answer a question most files cannot contain the answer to,
    which is the cost that put `DJP-005` over its budget.
    """

    WORDS: tuple[str, ...] = ()

    def wanted(self, node: ast.Call) -> bool:
        """Whether this call is the shape the rule reports, ignoring context."""
        raise NotImplementedError

    def report(
        self, ctx: ProjectContext, path: Path, call: ast.Call, model: str | None
    ) -> Finding | None:
        """The finding, or ``None`` once the model says there is not one.

        ``wanted`` is syntax and runs on every call; this runs only on calls
        the tracker agreed are querysets, and is where a rule that needs to
        know *what* is being queried gets to change its mind.
        """
        raise NotImplementedError

    def diverging(self, ctx: ProjectContext, found: Divergence) -> Iterator[Finding]:
        # The textual prefilter costs nothing to be wrong about -- a file it
        # lets through is still checked properly -- so mutating it away leaves
        # every finding identical and only the clock moves. It moves a lot:
        # `.distinct(` appears in 21 of pretix's 1225 files, and parsing and
        # scoping just those takes 526ms against 11.4s for all of them.
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or not any(word in source for word in self.WORDS):
                continue
            if ctx.parse(path) is None:
                continue
            root = ctx.scopes(path)
            if root is None:
                continue
            yield from self.inspect(ctx, path, root)

    def inspect(self, ctx: ProjectContext, path: Path, scope: Scope) -> Iterator[Finding]:
        here = [call for call in own_calls(scope) if self.wanted(call)]
        if here:
            frame = Frame(ctx=ctx, path=path, scope=scope, chains=ctx.def_use(scope))
            behind = frame.queryset_models
            for call in here:
                if id(call) in behind:
                    finding = self.report(ctx, path, call, behind[id(call)])
                    if finding is not None:
                        yield finding
        for child in scope.children:
            yield from self.inspect(ctx, path, child)


@register
class DistinctOnFields(QueryRule):
    """`distinct("field")` is Postgres-only and SQLite cannot run it at all.

    Not a behavioural difference but a hard failure: Django checks
    `can_distinct_on_fields`, which is `False` on SQLite, and raises
    `NotSupportedError` when the query is evaluated. A developer running SQLite
    meets this the first time the code path executes, which is why it is worth
    reporting statically rather than leaving to a test that may not cover it.
    """

    meta = RuleMeta(
        id="DJX-003",
        title="DISTINCT ON is used in a project that also runs SQLite",
        family=Family.DJX,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "`distinct(*fields)` compiles to Postgres' `DISTINCT ON`. Django gates it "
            "on the `can_distinct_on_fields` feature flag, which SQLite sets to False, "
            "and raises `NotSupportedError` when the queryset is evaluated rather than "
            "when it is written. In a project that runs both engines the query works in "
            "production and fails outright in development, so it is the rare divergence "
            "that is louder on the developer's machine than in production -- and it "
            "still fails, on a code path a test may never reach."
        ),
        remediation=(
            "Express the same intent portably, with a subquery selecting the row you "
            "want per group and an `__in` filter against it, or accept the dependency "
            "deliberately and run Postgres in development too."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#distinct",
            "https://docs.djangoproject.com/en/stable/ref/databases/",
        ),
        limitations=(
            "Only reported when the project is evidenced to reach both SQLite and "
            "Postgres. NetBox writes three genuine `distinct('field')` calls and is "
            "not reported, because NetBox runs Postgres and only Postgres -- there is "
            "no divergence there to report.",
        ),
    )

    WORDS = (".distinct(",)

    def wanted(self, node: ast.Call) -> bool:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "distinct":
            return False
        # `distinct()` with no field is portable and extremely common; only the
        # field form compiles to DISTINCT ON.
        return any(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args)

    def report(self, ctx: ProjectContext, path: Path, call: ast.Call, model: str | None) -> Finding:
        fields = [
            a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
        named = ", ".join(repr(f) for f in fields)
        return self.finding(
            location=ctx.location(path, call),
            message=(
                f"distinct({named}) compiles to Postgres' DISTINCT ON, which SQLite "
                f"cannot execute -- this query raises NotSupportedError on the engine "
                f"this project runs in development"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    content=ctx.snippet(path, call.lineno, call.end_lineno),
                    source=ctx.rel(path),
                ),
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content="django.db.backends.sqlite3: can_distinct_on_fields = False",
                    source="django feature flags",
                ),
            ),
            properties={"fields": " ".join(fields)},
        )


LOOKUP_METHODS = frozenset({"filter", "exclude", "get", "get_or_create", "update_or_create"})
"""Queryset methods whose keyword arguments are field lookups.

`annotate` and `alias` are deliberately absent: their keywords name the
annotation, not a field, so `contains=...` there is a variable name.
"""

CONTAINMENT = ("__contains", "__contained_by")
"""The two JSON lookups SQLite has no operator for.

`has_key`, `has_keys` and `has_any_keys` are *not* here, and that is the whole
point of the pair: they are supported on both backends, so the portable way to
ask a containment question exists and the remediation is not "give up".
"""


def containment(keyword: str) -> str | None:
    """The containment suffix `keyword` ends in, or None.

    Matched once and returned, rather than tested here and re-tested where the
    path is stripped. Two independent spellings of the same suffix is how a
    rule ends up reporting `data__contains` and stripping the wrong number of
    characters off it.
    """
    for suffix in CONTAINMENT:
        if keyword.endswith(suffix):
            return suffix
    return None


def field_at(
    ctx: ProjectContext, model: str | None, path: str, *, shorten: bool = False
) -> tuple[str, FieldNode] | None:
    """The field a lookup path lands on, with the path that reached it.

    `shorten` is for lookups written against something *inside* a column.
    `data__tags__contains` names a key, not a field, so the path has to be cut
    back a segment at a time before the model graph recognises anything. Only a
    caller that then checks the field's type should ask for it: without that
    check, shortening is a licence to attribute a lookup to whatever column
    happens to share its first segment.
    """
    if model is None:
        return None
    fields = ctx.model_graph.reachable_fields(model)
    parts = path.split("__")
    while parts:
        reached = "__".join(parts)
        found = fields.get(reached)
        if found is not None:
            return reached, found
        if not shorten:
            return None
        parts.pop()
    return None


def json_field(ctx: ProjectContext, model: str | None, path: str) -> str | None:
    """The lookup path `path` reduced to a JSONField on `model`, or None."""
    found = field_at(ctx, model, path, shorten=True)
    if found is None:
        return None
    reached, node = found
    return reached if node.kind == "JSONField" else None


@register
class JsonContainment(QueryRule):
    """`__contains` on a JSONField is Postgres-only, and SQLite raises.

    The same shape as `DJX-003` and a different failure to explain: this one is
    invisible in the keyword. `name__contains` on a `CharField` is an ordinary
    substring match that works everywhere; `data__contains` on a `JSONField` is
    a containment test SQLite has no operator for, and Django raises rather
    than approximating it. Telling them apart needs the model, which is why
    this rule is the reason `QueryRule.report` is given one.
    """

    meta = RuleMeta(
        id="DJX-002",
        title="a JSONField containment lookup is used in a project that also runs SQLite",
        family=Family.DJX,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "Django gates the JSONField `contains` and `contained_by` lookups on the "
            "`supports_json_field_contains` feature flag, which SQLite sets to False. "
            "Evaluating such a queryset raises `NotSupportedError` on SQLite and "
            "returns rows on Postgres, so the same code path is a working feature in "
            "production and a crash in development. Unlike the case-insensitivity "
            "differences in this family it cannot be missed once reached -- but it is "
            "only reached when the code path runs, which a test suite may never do."
        ),
        remediation=(
            "Ask the question with `has_key`, `has_keys` or `has_any_keys`, which both "
            "backends support, or filter on the key directly with "
            "`data__key=value`. Where genuine containment is required, accept the "
            "dependency deliberately and run Postgres in development too."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/topics/db/queries/#containment-and-key-lookups",
            "https://docs.djangoproject.com/en/stable/ref/databases/",
        ),
        limitations=(
            "The field has to be resolvable: a lookup on a queryset whose model we "
            "could not name, or on a field reached through more relations than the "
            "model graph walks, is not reported. `__contains` on a "
            "`django.contrib.postgres` ArrayField is also Postgres-only and is not "
            "reported here -- DJX-005 reports the field itself, which covers every "
            "query written against it rather than one lookup at a time.",
        ),
    )

    WORDS = CONTAINMENT

    def wanted(self, node: ast.Call) -> bool:
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in LOOKUP_METHODS:
            return False
        # `kw.arg` is None for `**kwargs`, which has no keyword to read.
        return any(kw.arg is not None and containment(kw.arg) is not None for kw in node.keywords)

    def report(
        self, ctx: ProjectContext, path: Path, call: ast.Call, model: str | None
    ) -> Finding | None:
        hits: list[tuple[str, str]] = []
        for kw in call.keywords:
            suffix = containment(kw.arg) if kw.arg is not None else None
            if kw.arg is None or suffix is None:
                continue
            field = json_field(ctx, model, kw.arg[: -len(suffix)])
            if field is not None:
                hits.append((kw.arg, field))
        if not hits:
            return None
        named = _series([repr(kw) for kw, _ in hits], "and")
        columns = sorted({field for _, field in hits})
        return self.finding(
            location=ctx.location(path, call),
            message=(
                f"{named} asks Postgres for JSON containment, which SQLite has no "
                f"operator for -- this query raises NotSupportedError on the engine "
                f"this project runs in development"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    content=ctx.snippet(path, call.lineno, call.end_lineno),
                    source=ctx.rel(path),
                ),
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content="django.db.backends.sqlite3: supports_json_field_contains = False",
                    source="django feature flags",
                ),
            ),
            properties={"lookups": " ".join(kw for kw, _ in hits), "fields": " ".join(columns)},
        )


CASE_SENSITIVE = ("__contains", "__startswith", "__endswith")
"""The three lookups that compile to LIKE and mean to be case-sensitive.

The `i` prefixed spellings are absent on purpose, and not by oversight: they
ask for case-insensitivity explicitly and get it on both backends, so they are
the remediation rather than the defect. `exact` and `regex` are absent because
they were measured not to differ.
"""

TEXT_FIELDS = frozenset(
    {"CharField", "TextField", "EmailField", "SlugField", "URLField", "FilePathField"}
)
"""Django's string columns, where LIKE is what these lookups compile to.

`JSONField` is deliberately not here: `__contains` on one is containment, not a
substring match, and it does not merely differ between the backends -- SQLite
raises. That is `DJX-002`.
"""


@register
class CaseSensitiveTextLookup(QueryRule):
    """`__contains` on text quietly matches more rows on SQLite than on Postgres.

    The quietest divergence in the family, and the reason the family exists.
    Nothing raises, nothing is logged, and the query returns rows on both
    backends -- just not the same rows. A developer whose local search finds
    the record they were looking for has no way to know production will not.
    """

    meta = RuleMeta(
        id="DJX-004",
        title="a case-sensitive text lookup is used in a project that also runs SQLite",
        family=Family.DJX,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "SQLite's LIKE folds case for ASCII characters and Postgres' does not, "
            "which Django records as `has_case_insensitive_like`. `contains`, "
            "`startswith` and `endswith` all compile to LIKE, so each of them is "
            "case-insensitive in development and case-sensitive in production. "
            "Measured on both engines: a row holding 'Hello' is returned by "
            "`text__contains='hello'` on SQLite and not on Postgres. Nothing raises "
            "and nothing is logged -- the query simply answers a different question "
            "on each backend, which is why it survives a test suite that only ever "
            "runs one of them."
        ),
        remediation=(
            "Decide which behaviour was meant and write it down. `icontains`, "
            "`istartswith` and `iendswith` are case-insensitive on both backends; "
            "for a genuinely case-sensitive match, normalise the column and the "
            "term instead of relying on the operator, or run Postgres in "
            "development so the two agree."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#contains",
            "https://docs.djangoproject.com/en/stable/ref/databases/#substring-matching-and-case-sensitivity",
        ),
        limitations=(
            "Reported as firm rather than certain because whether the difference is "
            "observable depends on the data: a column that only ever holds one case "
            "answers identically on both engines. The folding is also ASCII-only -- "
            "measured, SQLite does not match 'ecole' against 'ECOLE' when the E "
            "carries an accent -- so a column holding non-ASCII text diverges less "
            "than this finding implies. Only Django's own string fields are "
            "recognised; a lookup on a custom field that subclasses CharField is "
            "not reported, because we cannot know what the subclass changed.",
        ),
    )

    WORDS = CASE_SENSITIVE

    def wanted(self, node: ast.Call) -> bool:
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in LOOKUP_METHODS:
            return False
        return any(kw.arg is not None and kw.arg.endswith(CASE_SENSITIVE) for kw in node.keywords)

    def report(
        self, ctx: ProjectContext, path: Path, call: ast.Call, model: str | None
    ) -> Finding | None:
        hits: list[tuple[str, str]] = []
        for kw in call.keywords:
            if kw.arg is None or not kw.arg.endswith(CASE_SENSITIVE):
                continue
            suffix = next(s for s in CASE_SENSITIVE if kw.arg.endswith(s))
            found = field_at(ctx, model, kw.arg[: -len(suffix)])
            if found is not None and found[1].kind in TEXT_FIELDS:
                hits.append((kw.arg, found[0]))
        if not hits:
            return None
        named = _series([repr(kw) for kw, _ in hits], "and")
        columns = sorted({column for _, column in hits})
        return self.finding(
            location=ctx.location(path, call),
            message=(
                f"{named} compiles to LIKE, which folds case on SQLite and not on "
                f"Postgres -- this query matches more rows in development than it "
                f"does in production, and nothing reports the difference"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    content=ctx.snippet(path, call.lineno, call.end_lineno),
                    source=ctx.rel(path),
                ),
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content="django.db.backends.sqlite3: has_case_insensitive_like = True",
                    source="django feature flags",
                ),
            ),
            properties={"lookups": " ".join(kw for kw, _ in hits), "fields": " ".join(columns)},
        )


POSTGRES_PACKAGE = "django.contrib.postgres."
"""Every field under this package is Postgres-only by construction.

Matching the package rather than a list of class names is deliberate. A list
goes stale the first time Django adds a field and reports nothing while looking
like it still works; the package boundary is Django's own statement about which
fields need Postgres, and it cannot drift out of date.
"""

POSTGRES_ONLY_EFFECT = {
    "ArrayField": (
        "SQLite cannot create the column at all: the type renders as `varchar(n)[]` "
        'and `migrate` fails with `near "[]": syntax error` before the table exists'
    ),
    "HStoreField": (
        "SQLite accepts `hstore` as a column type -- it accepts any word as a column "
        "type -- so the table is created and the failure moves to the first write, "
        "which raises `type 'dict' is not supported`"
    ),
    "IntegerRangeField": (
        "SQLite creates the column, because `int4range` is just a word to it, and the "
        "first write fails on the range literal"
    ),
    "BigIntegerRangeField": (
        "SQLite creates the column, because `int8range` is just a word to it, and the "
        "first write fails on the range literal"
    ),
    "DecimalRangeField": (
        "SQLite creates the column, because `numrange` is just a word to it, and the "
        "first write fails on the range literal"
    ),
    "DateRangeField": (
        "SQLite creates the column, because `daterange` is just a word to it, and the "
        "first write fails on the range literal"
    ),
    "DateTimeRangeField": (
        "SQLite creates the column, because `tstzrange` is just a word to it, and the "
        "first write fails on the range literal"
    ),
    "SearchVectorField": (
        "SQLite creates the column and even stores NULL in it, so nothing fails until "
        "something searches: `SearchVector` needs `to_tsvector`, which SQLite does not "
        "have, and the `@@` operator is a syntax error there"
    ),
}
"""What SQLite actually does with each type, measured rather than assumed.

The three outcomes are genuinely different and a developer needs to know which
one they are in: `migrate` fails, the first write fails, or nothing fails until
a query runs. Types absent from this table are still reported -- the package is
what decides that -- but described in general terms, because describing a
failure we have not measured is how a rule starts inventing.
"""


@register
class PostgresOnlyField(DivergenceRule):
    """A `django.contrib.postgres` field in a project that also runs SQLite.

    The bluntest rule in the family. `DJX-004` reports a query that answers
    differently; this reports a schema that one of the two engines cannot hold
    at all. Whatever the project believes about running SQLite in development,
    a model with an `ArrayField` cannot be migrated there.
    """

    meta = RuleMeta(
        id="DJX-005",
        title="a Postgres-only field type is declared in a project that also runs SQLite",
        family=Family.DJX,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "`django.contrib.postgres` exists to expose types Postgres has and other "
            "backends do not, and Django does not stop you pointing one at SQLite -- "
            "it renders the Postgres type verbatim and lets the database complain. "
            "Measured against a real SQLite: an `ArrayField` renders `varchar(16)[]` "
            "and `migrate` fails outright; an `HStoreField` renders `hstore`, which "
            "SQLite accepts as a column type because it accepts any word, so the table "
            "is created and the first write fails instead; a `SearchVectorField` gets "
            "as far as storing NULL and fails only when something searches it. All "
            "three end in a broken developer environment, and the last two end in one "
            "that looks healthy until it is used."
        ),
        remediation=(
            "Either stop running SQLite -- these types are a deliberate dependency on "
            "Postgres and the honest response is to depend on it everywhere -- or "
            "replace the field with a portable one: `JSONField` covers `ArrayField` "
            "and `HStoreField` for most uses, a range becomes two nullable columns "
            "with a `CheckConstraint`, and full-text search becomes a search service "
            "or a portable `icontains` fallback."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/contrib/postgres/fields/",
            _BACKENDS_DOCS,
        ),
        limitations=(
            "Reported per field, not per model, because the replacement differs by "
            "type and a single finding naming four fields would have four different "
            "fixes. A field imported under an alias is still resolved, but a model "
            "built by a factory function rather than declared is not seen at all. "
            "Fields outside `django.contrib.postgres` that nonetheless require "
            "Postgres -- a third-party package's own vector or citext field -- are not "
            "reported, because we cannot know what SQL an unfamiliar field emits. "
            "Only reported when the project is evidenced to reach both SQLite and "
            "Postgres: NetBox declares 23 of these fields and is not reported, because "
            "NetBox runs Postgres and only Postgres, and 23 findings about a "
            "dependency it took deliberately is how a family earns an ignore rule.",
        ),
    )

    def diverging(self, ctx: ProjectContext, found: Divergence) -> Iterator[Finding]:
        for model in ctx.model_graph.models.values():
            if model.is_proxy:
                continue
            for field in model.all_fields.values():
                if not field.dotted.startswith(POSTGRES_PACKAGE):
                    continue
                yield self.report(ctx, model, field)

    def report(self, ctx: ProjectContext, model: ModelNode, field: FieldNode) -> Finding:
        effect = POSTGRES_ONLY_EFFECT.get(
            field.kind,
            "SQLite has no equivalent type and the column cannot hold what this field is for",
        )
        return self.finding(
            location=Location(
                file=ctx.rel(model.path),
                line=field.lineno,
                end_line=field.end_lineno,
                snippet=ctx.snippet(model.path, field.lineno, field.end_lineno),
            ),
            message=(
                f"`{model.name}.{field.name}` uses {field.kind}, which only Postgres "
                f"has -- {effect}"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    content=ctx.snippet(model.path, field.lineno, field.end_lineno),
                    source=ctx.rel(model.path),
                ),
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=f"{field.dotted} resolved through this module's imports",
                    source="model graph",
                ),
            ),
            properties={"field": f"{model.name}.{field.name}", "kind": field.kind},
        )


POSTGRES_CONSTRAINTS = "django.contrib.postgres.constraints."
"""Constraints that exist only on Postgres, by the same package test as DJX-005."""


@register
class UnenforcedConstraint(DivergenceRule):
    """A constraint SQLite silently declines to create, or cannot parse at all.

    The most dangerous rule in the family, because it is the only one whose
    consequence is *data*. The others make a query answer differently or a
    migration fail; this one lets a developer's database hold rows that
    production would have refused, and nothing about the local database looks
    wrong until that data is loaded somewhere real.
    """

    meta = RuleMeta(
        id="DJX-006",
        title="a constraint is declared that SQLite does not enforce",
        family=Family.DJX,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "Measured against a real SQLite: a `UniqueConstraint` carrying any "
            "`deferrable=` is dropped from the emitted `CREATE TABLE` entirely -- not "
            "the deferral, the constraint. The same model without the argument emits "
            "`CONSTRAINT ... UNIQUE (...)`, and with it emits nothing, so two rows "
            "sharing the supposedly unique value are accepted on SQLite and rejected "
            "on Postgres. `DEFERRED` and `IMMEDIATE` behave identically there; both "
            "lose the constraint. An `ExclusionConstraint` fails harder and earlier: "
            "SQLite cannot parse `EXCLUDE` and `migrate` stops with a syntax error. "
            "The difference matters because the first failure mode is silent and "
            "produces data, and the second is loud and produces nothing."
        ),
        remediation=(
            "For a deferrable unique constraint, decide whether the deferral is "
            "actually needed -- it usually exists to allow a swap inside one "
            "transaction -- and if it is not, drop the argument and get the "
            "constraint enforced on both backends. If it is needed, the constraint is "
            "a Postgres dependency and development should run Postgres too, because "
            "the alternative is a local database with no uniqueness at all. An "
            "`ExclusionConstraint` has no SQLite equivalent and the same choice "
            "applies, without the silence."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/constraints/#deferrable",
            "https://docs.djangoproject.com/en/stable/ref/contrib/postgres/constraints/",
        ),
        limitations=(
            "Django's own system checks emit `models.W038` for the deferrable case "
            "when SQLite is the configured backend, so this is not always the first "
            "warning a developer could see. It is reported anyway, and for two "
            "reasons: `W038` is a warning that does not fail `manage.py check`, and "
            "it only appears when the command happens to be pointed at SQLite -- a "
            "developer or a CI job running the same check against Postgres sees "
            "nothing at all. Nothing warns about `ExclusionConstraint`; measured, "
            "`check` returns clean and `migrate` then fails. Only constraints written "
            "literally in `Meta.constraints` are read, so a list built by a helper "
            "function is not seen.",
        ),
    )

    def diverging(self, ctx: ProjectContext, found: Divergence) -> Iterator[Finding]:
        for model in ctx.model_graph.models.values():
            if model.is_proxy:
                continue
            for constraint in model.constraints:
                effect = self.effect(constraint)
                if effect is not None:
                    yield self.report(ctx, model, constraint, *effect)

    def effect(self, constraint: ConstraintNode) -> tuple[str, str] | None:
        """What SQLite does with this constraint, and the citation for saying so.

        The two halves fail differently enough that they cannot share one
        citation. The deferrable case is a declared Django feature flag; the
        exclusion case has no flag at all, only a measured `migrate` failure.
        """
        if constraint.dotted.startswith(POSTGRES_CONSTRAINTS):
            return (
                "SQLite cannot parse `EXCLUDE` and `migrate` stops there with a syntax "
                "error, so the whole schema is unreachable on that engine",
                'sqlite3.OperationalError: near "EXCLUDE": syntax error -- raised by '
                "`migrate`; `manage.py check` reports no issues beforehand",
            )
        if constraint.deferrable:
            return (
                "SQLite drops the constraint from `CREATE TABLE` rather than declining "
                "the deferral, so the uniqueness is not enforced there at all and two "
                "rows sharing the value are accepted in development and rejected in "
                "production",
                "django.db.backends.sqlite3: supports_deferrable_unique_constraints = "
                "False (postgresql: True)",
            )
        return None

    def report(
        self,
        ctx: ProjectContext,
        model: ModelNode,
        constraint: ConstraintNode,
        effect: str,
        flag: str,
    ) -> Finding:
        named = f"`{constraint.name}`" if constraint.name else f"a {constraint.kind}"
        return self.finding(
            location=Location(
                file=ctx.rel(model.path),
                line=constraint.lineno,
                end_line=constraint.end_lineno,
                snippet=ctx.snippet(model.path, constraint.lineno, constraint.end_lineno),
            ),
            message=(
                f"{named} on {model.name} is a {constraint.kind} that SQLite does not "
                f"enforce -- {effect}"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    content=ctx.snippet(model.path, constraint.lineno, constraint.end_lineno),
                    source=ctx.rel(model.path),
                ),
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=flag,
                    source="measured against django 6.0 and sqlite 3",
                ),
            ),
            properties={
                "constraint": constraint.name or constraint.kind,
                "kind": constraint.kind,
            },
        )
