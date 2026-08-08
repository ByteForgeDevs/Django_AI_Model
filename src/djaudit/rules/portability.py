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
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import Rule, RuleMeta, register
from djaudit.rules._injection import Frame, own_calls

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.dataflow.scopes import Scope

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
            "SQLite and Postgres disagree about more than speed. SQLite compares text "
            "case-sensitively by default and Postgres does too but collates it "
            "differently, so ordering and `iexact` diverge; SQLite does not enforce "
            "`max_length`, so a value that truncates in one and raises in the other "
            "passes every local test; foreign keys are unenforced under SQLite unless "
            "explicitly switched on; `distinct('field')`, `ArrayField` and the JSON "
            "containment operators exist only on Postgres. None of these fail at "
            "import time and none are visible in a diff. They fail in production, "
            "against real data, on code that passed the whole test suite -- because "
            "the test suite ran against the other database. That is what makes this "
            "worth reporting even though nothing here is a vulnerability: it converts "
            "'works locally' from evidence into a coincidence."
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


def json_field(ctx: ProjectContext, model: str | None, path: str) -> str | None:
    """The lookup path `path` reduced to a JSONField on `model`, or None.

    A JSON lookup can be written against the field itself (`data__contains`) or
    against a key inside it (`data__tags__contains`), and both raise on SQLite.
    Key transforms are not fields, so the path is shortened one segment at a
    time until it lands on something the model graph knows -- and because the
    answer only counts when that something is a JSONField, a shortened path can
    never wander onto an unrelated column.
    """
    if model is None:
        return None
    fields = ctx.model_graph.reachable_fields(model)
    parts = path.split("__")
    while parts:
        found = fields.get("__".join(parts))
        if found is not None:
            return "__".join(parts) if found.kind == "JSONField" else None
        parts.pop()
    return None


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
