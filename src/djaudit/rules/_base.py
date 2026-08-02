"""Shared machinery for rules that inspect resolved settings.

Steps 1.4 to 1.8 add twenty-six settings rules. Left to themselves they would
each re-derive the same four things -- which modules can reach production, how
to resolve a setting there, how to grade the result, and what evidence to
attach -- and they would drift apart while doing it. Confidence in particular
has to mean the same thing everywhere or the CI gate becomes arbitrary.

So the loop and the grading live here, and a rule supplies only the judgement
that is actually its own.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

from djaudit.context import ProjectContext, SettingsModule
from djaudit.models import Confidence, Evidence, EvidenceKind, Finding, Location, Severity
from djaudit.registry import Rule
from djaudit.settings import Assessment, ResolvedSetting, SettingsView, assess, resolve_all

if TYPE_CHECKING:
    from djaudit.values import Value


class SettingsRule(Rule):
    """A rule that inspects resolved settings in production-reachable modules."""

    ceiling: Confidence = Confidence.CERTAIN
    """The best confidence this rule's own inference can justify.

    Resolution quality degrades it from here. A rule that observes a value
    directly can start at CERTAIN; one that infers intent should not.
    """

    views: dict[str, SettingsView]
    """Every production-reachable and development module, resolved once per run.

    Exposed because some rules need to look across modules -- whether a base
    module's DEBUG is switched off by everything that imports it, for instance
    -- and re-resolving the project per rule would be wasteful and could drift.
    """

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        self.views = views = resolve_all(ctx)
        for module in ctx.settings_modules:
            # Development and test settings are allowed to be insecure. That is
            # what they are for, and reporting them is how a tool teaches people
            # to ignore it.
            if not module.role.reaches_production:
                continue
            view = views.get(module.dotted)
            if view is not None:
                yield from self.inspect(ctx, module, view)

    @abstractmethod
    def inspect(
        self, ctx: ProjectContext, module: SettingsModule, view: SettingsView
    ) -> Iterator[Finding]:
        """Yield findings for one production-reachable settings module."""
        raise NotImplementedError

    def report(
        self,
        ctx: ProjectContext,
        module: SettingsModule,
        resolved: ResolvedSetting,
        *,
        message: str,
        severity: Severity | None = None,
        ceiling: Confidence | None = None,
        extra_caveats: tuple[str, ...] = (),
        evidence: tuple[Evidence, ...] = (),
        remediation: str | None = None,
    ) -> Finding:
        """Build a finding about ``resolved``, graded by the shared policy."""
        graded = assess(resolved, ceiling=ceiling or self.ceiling)
        if extra_caveats:
            graded = Assessment(graded.confidence, (*graded.caveats, *extra_caveats))

        definition = resolved.definition
        path = definition.module if definition else module.path
        node = definition.node if definition else None

        provenance = Evidence(
            kind=EvidenceKind.CONFIG,
            content=describe_resolution(module, resolved),
            source="djaudit settings resolver",
        )
        source = (
            (
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    content=ctx.snippet(path, node.lineno, node.end_lineno),
                    source=ctx.rel(path),
                ),
            )
            if node is not None
            else ()
        )

        return self.finding(
            location=ctx.location(path, node) if node is not None else module_location(ctx, module),
            message=f"{message.rstrip('.')}{graded.note()}.",
            evidence=(*source, *evidence, provenance),
            severity=severity,
            confidence=graded.confidence,
            remediation=remediation,
            properties={
                "settings_role": module.role.value,
                "settings_module": module.dotted,
                "setting": resolved.name,
            },
        )


def describe_resolution(module: SettingsModule, resolved: ResolvedSetting) -> str:
    """The resolver's working, so a reader can check it rather than trust it."""
    header = (
        f"module={module.dotted}  role={module.role.value}  "
        f"entrypoint={module.is_entrypoint}\n"
        f"{resolved.name} = {resolved.value.describe()}  ({resolved.origin.value})"
    )
    chain = "\n".join(f"  {definition.describe()}" for definition in resolved.definitions)
    return f"{header}\n{chain}" if chain else header


def module_location(ctx: ProjectContext, module: SettingsModule) -> Location:
    """Point at the settings module itself when there is no assignment to blame.

    A setting that is absent has no line, but the module that should have had
    it does, and that is where the fix goes.
    """
    return Location(file=ctx.rel(module.path), line=1)


def literal_text(value: Value) -> str | None:
    """The value as a plain string, or ``None`` if it is not one."""
    return value.literal if value.is_literal and isinstance(value.literal, str) else None
