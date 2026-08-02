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

import ast
from abc import abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, replace

from djaudit.context import ProjectContext, SettingsModule, SettingsRole
from djaudit.evaluator import Evaluator
from djaudit.models import Confidence, Evidence, EvidenceKind, Finding, Location, Severity
from djaudit.registry import Rule
from djaudit.settings import (
    Assessment,
    Origin,
    ResolvedSetting,
    SettingsView,
    assess,
    resolve_all,
)
from djaudit.values import Value

MIN_MASKABLE = 4
"""Below this a value is too short to redact usefully and too short to be a
secret worth protecting -- and masking it would hide the fact that it is tiny,
which is the very thing the reader needs to see."""


_ROLE_WEIGHT: dict[SettingsRole, int] = {
    SettingsRole.PRODUCTION: 4,
    SettingsRole.PRIMARY: 3,
    SettingsRole.UNKNOWN: 2,
    SettingsRole.BASE: 1,
    SettingsRole.DEVELOPMENT: 0,
    SettingsRole.TEST: 0,
}


@dataclass(frozen=True, slots=True)
class SettingGroup:
    """One setting, decided in one place, as seen by every module it reaches."""

    setting: ResolvedSetting
    module: SettingsModule
    """The worst-affected module, which is what the finding is graded against."""

    modules: tuple[SettingsModule, ...]
    """Every production-reachable module that resolves to this same decision."""

    @property
    def shared(self) -> bool:
        return len(self.modules) > 1

    def narrow(self, name: str, value: Value) -> SettingGroup:
        """The same group judged on one part of the setting rather than all of it.

        ``DATABASES`` as a whole is usually unresolvable, and grading a finding
        about one password against that would bury it at tentative. The part
        keeps the whole's provenance -- whether the assignment was conditional,
        where it lives, which modules it reaches -- and contributes only its own
        value, so the shared policy grades it without needing a second path
        through it.
        """
        # The whole setting is usually UNRESOLVED precisely because this part
        # was not the problem, and assess() floors anything unresolved at
        # tentative. The part is graded on whether the part resolved.
        origin = Origin.EXPLICIT if not value.is_unknown else Origin.UNRESOLVED
        return replace(self, setting=replace(self.setting, name=name, value=value, origin=origin))

    def describe_reach(self) -> str:
        """A clause naming the other modules affected, or nothing."""
        others = [m.dotted for m in self.modules if m.dotted != self.module.dotted]
        return f" (also reaching {', '.join(others)})" if others else ""


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

    setting: str = ""
    """The setting this rule is about, resolved and grouped for it.

    Almost every settings rule is about exactly one setting, and saying so lets
    the base do the part that is easy to get wrong.
    """

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        self.views = resolve_all(ctx)
        for group in self.groups(ctx):
            yield from self.inspect(ctx, group)

    def groups(self, ctx: ProjectContext) -> list[SettingGroup]:
        """One group per distinct place ``setting`` is decided.

        A base module's assignment is inherited by everything that imports it,
        so resolving per module and reporting per module reports one line as
        many findings as there are environments. Grouping by the assignment
        that actually decides the value reports the defect once and names every
        module it reaches, which is also what makes the severity right: the
        group is graded by the worst-affected module, so a key sitting in
        base.py is judged by the fact that production uses it.
        """
        collected: dict[tuple[str, int], list[tuple[SettingsModule, ResolvedSetting]]] = {}
        for module in ctx.settings_modules:
            # Development and test settings are allowed to be insecure. That is
            # what they are for, and reporting them is how a tool teaches people
            # to ignore it.
            if not module.role.reaches_production:
                continue
            view = self.views.get(module.dotted)
            if view is None:
                continue
            resolved = view.get(self.setting) if self.setting else None
            if resolved is None:
                continue
            definition = resolved.definition
            key = (
                (str(definition.module), definition.node.lineno)
                if definition
                else (str(module.path), 0)
            )
            collected.setdefault(key, []).append((module, resolved))

        groups = []
        for members in collected.values():
            # Grade by the module with most to lose, so an inherited value is
            # judged by the environment that actually deploys it.
            module, resolved = max(members, key=lambda pair: _ROLE_WEIGHT[pair[0].role])
            groups.append(
                SettingGroup(
                    setting=resolved,
                    module=module,
                    modules=tuple(sorted((m for m, _ in members), key=lambda m: m.dotted)),
                )
            )
        return groups

    @abstractmethod
    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        """Yield findings for one place where the setting is decided."""
        raise NotImplementedError

    def report(
        self,
        ctx: ProjectContext,
        group: SettingGroup,
        *,
        message: str,
        at: ast.expr | None = None,
        severity: Severity | None = None,
        ceiling: Confidence | None = None,
        extra_caveats: tuple[str, ...] = (),
        evidence: tuple[Evidence, ...] = (),
        remediation: str | None = None,
        redact: str | None = None,
    ) -> Finding:
        """Build a finding about ``group``, graded by the shared policy."""
        module, resolved = group.module, group.setting
        graded = assess(resolved, ceiling=ceiling or self.ceiling)
        if extra_caveats:
            graded = Assessment(graded.confidence, (*graded.caveats, *extra_caveats))

        definition = resolved.definition
        path = definition.module if definition else module.path
        # ``at`` points inside the assignment -- one entry of a dict, say --
        # where the fix actually goes.
        node: ast.stmt | ast.expr | None = at or (definition.node if definition else None)

        provenance = Evidence(
            kind=EvidenceKind.CONFIG,
            content=mask(describe_resolution(group), redact),
            source="djaudit settings resolver",
        )
        source = (
            (
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    content=mask(ctx.snippet(path, node.lineno, node.end_lineno), redact),
                    source=ctx.rel(path),
                ),
            )
            if node is not None
            else ()
        )

        # Location carries its own copy of the source line, so redacting only
        # the evidence would still ship the secret in every report.
        location = ctx.location(path, node) if node is not None else module_location(ctx, module)
        if redact:
            location = replace(location, snippet=mask(location.snippet, redact))

        return self.finding(
            location=location,
            message=f"{message.rstrip('.')}{graded.note()}.",
            evidence=(*source, *evidence, provenance),
            severity=severity,
            confidence=graded.confidence,
            remediation=remediation,
            properties={
                "settings_role": module.role.value,
                "settings_module": module.dotted,
                "settings_modules": " ".join(m.dotted for m in group.modules),
                "setting": resolved.name,
            },
        )


def mask(text: str, secret: str | None) -> str:
    """Blank out a secret before it travels further than the repository.

    A finding ends up in JSON artifacts, SARIF uploads and CI logs, all of
    which are shared more widely than the source is. Quoting the key we are
    complaining about would spread it, so the evidence shows the shape of the
    value and not the value.
    """
    if not secret or len(secret) < MIN_MASKABLE:
        return text
    return text.replace(secret, f"{secret[:2]}<redacted:{len(secret)} chars>")


def describe_resolution(group: SettingGroup) -> str:
    """The resolver's working, so a reader can check it rather than trust it."""
    module, resolved = group.module, group.setting
    affected = " ".join(m.dotted for m in group.modules)
    header = (
        f"module={module.dotted}  role={module.role.value}  "
        f"entrypoint={module.is_entrypoint}\n"
        f"affects={affected}\n"
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


@dataclass(frozen=True, slots=True)
class Entry:
    """One key of a dict literal, resolved without reference to its siblings."""

    key: str
    value: Value
    node: ast.expr | None = None
    """The value expression, when there was one to point at.

    ``None`` when the entry came from a resolved value rather than a literal in
    the source -- a dict built by a helper has no line of its own, so a finding
    about it falls back to the assignment.
    """


def entries(
    view: SettingsView, node: ast.expr | None, value: Value | None = None
) -> dict[str, Entry]:
    """Read a dict setting one key at a time.

    Preferring the AST over the resolved value is deliberate. A dict collapses
    to unknown the moment any single entry does, and a real ``DATABASES`` block
    always has something coming from the environment -- so a password written
    in beside it would disappear along with the rest. Resolving each entry on
    its own keeps what is knowable, and the AST keeps the line numbers, which a
    plain dict has already thrown away.

    When the assignment is not a literal at all -- ``getattr(configuration,
    'DATABASES', ...)``, or a call to the project's own builder -- there is no
    dict in the source to walk, so a resolved value is used instead. That shape
    is common enough to matter: it is how NetBox writes every one of its
    settings, and a rule that quietly skipped it would report nothing and look
    like it had checked. Such entries carry no node, so a finding about one
    points at the assignment.

    Non-string keys are skipped: every setting shaped like this is keyed by
    name, and the callers all look keys up by name.
    """
    if not isinstance(node, ast.Dict):
        if value is not None and value.is_literal and isinstance(value.literal, dict):
            return {
                key: Entry(key=key, value=Value.of(inner, env_dependent=value.env_dependent))
                for key, inner in value.literal.items()
                if isinstance(key, str)
            }
        return {}

    evaluator = Evaluator(view.scope)
    found: dict[str, Entry] = {}
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if key_node is None:
            continue
        key = evaluator.evaluate(key_node)
        name = literal_text(key)
        if name is None:
            continue
        found[name] = Entry(key=name, value=evaluator.evaluate(value_node), node=value_node)
    return found


def assignment_value(setting: ResolvedSetting) -> ast.expr | None:
    """The right-hand side of the assignment that decided a setting."""
    definition = setting.definition
    if definition is None or not isinstance(definition.node, ast.Assign | ast.AnnAssign):
        return None
    return definition.node.value
