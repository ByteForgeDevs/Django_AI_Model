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

from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.engines import Divergence, EngineChoice, module_name, portability
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import Rule, RuleMeta, register
from djaudit.settings import resolve_all

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


def _joined(default: EngineChoice, choices: tuple[EngineChoice, ...]) -> str:
    """`a`, `b` or `c` -- the alternatives, as prose."""
    described = [_describe(default, c) for c in choices]
    if len(described) == 1:
        return described[0]
    return f"{', '.join(described[:-1])} or {described[-1]}"


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
        found = portability(ctx, resolve_all(ctx))
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
