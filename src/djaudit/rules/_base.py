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
from collections.abc import Callable, Iterator
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
    the base do the part that is easy to get wrong. A rule about a family of
    settings leaves this empty and overrides :meth:`selects` instead.
    """

    aliases: tuple[str, ...] = ()
    """Older names for :attr:`setting`, in falling order of precedence.

    Third-party apps rename their settings and keep reading the old name, which
    they nearly always do as ``getattr(settings, NEW, getattr(settings, OLD,
    default))`` -- so the new name wins by being *assigned at all*, even when it
    is assigned the harmless value. A rule that looked only at the new name
    would miss every project that has not migrated, and one that looked at both
    independently would report a legacy assignment the new name has already
    overruled. :meth:`resolve` follows the same chain the app does.
    """

    def resolve(self, view: SettingsView) -> ResolvedSetting:
        """The setting as the framework reads it, following :attr:`aliases`."""
        primary = view.get(self.setting)
        if primary.is_assigned:
            return primary
        for alias in self.aliases:
            fallback = view.get(alias)
            if fallback.is_assigned:
                return fallback
        return primary

    def selects(self, name: str) -> bool:
        """Whether this rule is about the setting called ``name``."""
        return name == self.setting

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
        collected: dict[tuple[str, str, int], list[tuple[SettingsModule, ResolvedSetting]]] = {}
        for module in ctx.settings_modules:
            # Development and test settings are allowed to be insecure. That is
            # what they are for, and reporting them is how a tool teaches people
            # to ignore it.
            if not module.role.reaches_production:
                continue
            view = self.views.get(module.dotted)
            if view is None:
                continue
            # A rule naming one setting wants view.get, which supplies Django's
            # default when the project never assigns it -- an absent setting is
            # often the finding. A rule matching a family cannot: there is no
            # default for a setting nobody has heard of, so it sees only what
            # the project actually assigns.
            found = (
                [self.resolve(view)]
                if self.setting
                else [rs for name, rs in view.settings.items() if self.selects(name)]
            )
            for resolved in found:
                definition = resolved.definition
                # A setting nobody assigns has no site to key on, and keying
                # on each module instead would report one missing flag once per
                # environment. It is one defect with one fix, so it gets one
                # bucket, graded against the module with most to lose.
                key = (
                    (resolved.name, str(definition.module), definition.node.lineno)
                    if definition
                    else (resolved.name, "", 0)
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

    def overridden(self, module: SettingsModule, safe: Callable[[ResolvedSetting], bool]) -> bool:
        """Whether every module inheriting ``module`` fixes the setting itself.

        A base module is not deployed on its own, so an insecure value there is
        a latent hazard rather than a live one when every environment that
        imports it corrects the value. Asking the resolver replaces scanning
        the project for a safe assignment anywhere, which said nothing about
        whether the two were connected.

        Development heirs are excluded: a development module leaving a flag off
        is what a development module is for, and it says nothing about
        production.
        """
        if module.role is not SettingsRole.BASE:
            return False
        heirs = [
            view
            for dotted, view in self.views.items()
            if dotted != module.dotted
            and module.dotted in view.chain
            and view.module.role.reaches_production
        ]
        return bool(heirs) and all(safe(self.resolve(heir)) for heir in heirs)

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


def entries_of(value: Value) -> tuple[list[object] | None, ...]:
    """Every list this value could be, or ``(None,)`` if we cannot tell.

    A conditional is not one list but several, and a rule that only looked at
    the first would miss the branch that matters.
    """
    if value.is_conditional:
        return tuple(item for branch in value.branches for item in entries_of(branch))
    if value.is_literal and isinstance(value.literal, (list, tuple)):
        return (list(value.literal),)
    return (None,)


def any_entry(value: Value, predicate: Callable[[object], bool]) -> bool:
    """Whether some resolvable branch holds an entry matching ``predicate``."""
    return any(
        any(predicate(entry) for entry in entries)
        for entries in entries_of(value)
        if entries is not None
    )


def definitely_empty(value: Value) -> bool:
    """Whether every branch we could read is an empty list."""
    branches = entries_of(value)
    return all(entries == [] for entries in branches)


def lists_entry(view: SettingsView, setting: str, entry: str) -> bool | None:
    """Whether ``setting`` holds ``entry``, or ``None`` if we could not tell.

    The three-valued answer is the point. A rule whose precondition is "this
    app is installed" must not treat "could not read the list" as "not
    installed", because plenty of projects build ``INSTALLED_APPS`` and
    ``MIDDLEWARE`` conditionally and every one of those would become a missed
    finding. NetBox is the case that pins the shape: its ``MIDDLEWARE``
    resolves to three branches of which one is unreadable, so a partial read
    answers ``None`` unless the entry turned up in a branch we could see.
    """
    branches = entries_of(view.get(setting).value)
    if any(entries is not None and entry in entries for entries in branches):
        return True
    return False if all(entries is not None for entries in branches) else None


def assignment_value(setting: ResolvedSetting) -> ast.expr | None:
    """The right-hand side of the assignment that decided a setting."""
    definition = setting.definition
    if definition is None or not isinstance(definition.node, ast.Assign | ast.AnnAssign):
        return None
    return definition.node.value


def could_be_true(value: Value) -> bool:
    """Whether ``value`` is the boolean ``True`` on at least one path.

    Deliberately identity-based. ``DEBUG = 1`` is truthy and Django would treat
    it as enabled, but it is also what a project writes when DEBUG is driven by
    something we have not modelled, and reporting it costs more in noise than
    it returns.
    """
    if value.is_conditional:
        return any(could_be_true(branch) for branch in value.branches)
    return value.is_literal and value.literal is True


def could_be_off(value: Value) -> bool:
    """Whether a security flag is anything other than ``True`` on some path.

    The mirror of ``could_be_true``, and deliberately not identity-based in the
    same way: ``True`` is the only value that turns one of these on, so
    ``SESSION_COOKIE_SECURE = 1`` is reported. That asymmetry is on purpose --
    for DEBUG a stray truthy value means "something we have not modelled drives
    this", and guessing costs noise; for a flag that must be on, anything we
    cannot see as ``True`` is something the reader wants to look at.
    """
    if value.is_conditional:
        return any(could_be_off(branch) for branch in value.branches)
    return not (value.is_literal and value.literal is True)


def could_be_under(value: Value, threshold: int) -> bool:
    """Whether a numeric setting is below ``threshold`` on some path.

    Anything that is not an integer at or above the threshold counts, which
    folds in the cases that are not numbers at all: ``None``, a string, a
    stray ``True``. None of those is a valid duration, so none of them is
    evidence that the duration is long enough.
    """
    if value.is_conditional:
        return any(could_be_under(branch, threshold) for branch in value.branches)
    if not value.is_literal:
        return True
    literal = value.literal
    return not (isinstance(literal, int) and not isinstance(literal, bool) and literal >= threshold)


class InsecureDefaultRule(SettingsRule):
    """A setting Django ships at an insecure value, which a project must raise.

    The family shares everything except the question "is this value bad" --
    the reporting, the wording, and above all the split-settings handling,
    which is the part that is easy to get subtly wrong and expensive to debug
    twice. Subclasses supply ``insecure()`` and a consequence.
    """

    consequence: str = ""
    """What goes wrong while the setting is unraised, mid-sentence."""

    corrected_as = "sets it"
    """How to describe an heir that fixes the value, mid-sentence."""

    caveats: tuple[str, ...] = ()
    """Doubt this rule always carries, attached to every finding it makes.

    Distinct from :attr:`ceiling`, and the distinction matters. A ceiling is
    for doubt about the *value*, and it is compounded by how well the value
    resolved. This is for doubt about the *consequence* -- something outside
    the settings that could make the finding moot -- which resolution quality
    says nothing about, so charging it as a grade would both double-count and
    hide the finding rather than qualify it."""

    @abstractmethod
    def insecure(self, value: Value) -> bool:
        """Whether ``value`` is the unsafe one, on any branch."""
        raise NotImplementedError

    def describe_state(self, resolved: ResolvedSetting) -> str:
        """The clause naming the current value, mid-sentence."""
        # The shared policy already appends "the setting is never assigned, so
        # Django's default applies", so saying it here too says it twice.
        return "is never set" if resolved.is_default else f"is {resolved.value.describe()}"

    def consequence_for(self, resolved: ResolvedSetting) -> str:
        return self.consequence

    def severity_for(self, resolved: ResolvedSetting) -> Severity | None:
        """Override the rule's default severity for this particular value.

        Some of these settings are insecure in more than one way, and the ways
        do not deserve the same weight -- a wildcard host is an attack, an
        empty one is a deployment that answers nothing.
        """
        return None

    def ceiling_for(self, resolved: ResolvedSetting) -> Confidence | None:
        """Override how confidently this particular value can be complained about.

        Severity and confidence move independently here. One value can be a
        plain fact about a string we read, and another value of the same
        setting can hinge on infrastructure we cannot see -- so a rule that
        grades the two failures apart usually has to grade the certainty apart
        as well.
        """
        return None

    def applies(self, ctx: ProjectContext, group: SettingGroup) -> bool:
        """Whether the setting is worth having an opinion about here at all.

        Some of these only matter once something else is switched on, and a
        rule that fires regardless is telling people to fix a value that has no
        effect. Overridden by the rules that have such a precondition.
        """
        return True

    def insecure_here(self, ctx: ProjectContext, group: SettingGroup) -> bool:
        """Whether this is the failure, judged against everything in scope.

        ``insecure()`` judges a value; this judges a situation. They differ for
        the settings whose protection can be missing rather than wrong -- a
        header that is never emitted because its middleware is not installed is
        the same defect as one emitted with a value browsers ignore, and both
        have the same fix, so they are one rule and one finding.
        """
        return self.insecure(group.setting.value)

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        if not self.insecure_here(ctx, group):
            return
        if not self.applies(ctx, group):
            return
        if resolved.origin is Origin.UNRESOLVED:
            # Nothing was learned, so there is nothing to say. Reporting every
            # unresolvable setting would bury the ones we actually read.
            return

        severity: Severity | None = self.severity_for(resolved)
        ceiling: Confidence | None = self.ceiling_for(resolved)
        caveats: tuple[str, ...] = self.caveats
        corrected = self.overridden(group.module, lambda rs: not self.insecure(rs.value))
        if resolved.is_default and self.overridden(
            group.module, lambda rs: rs.is_assigned or not self.insecure(rs.value)
        ):
            # The base never mentions the setting and every environment decides
            # it. There is no wrong value here to fix -- this is simply where
            # the setting does not live -- which is different from a base that
            # writes an insecure value down and gets overridden anyway.
            #
            # Note this covers heirs that decide it *badly* as well as heirs
            # that decide it well. Each heir is judged on the value it actually
            # assigns, so also blaming the base for staying silent would report
            # one missing setting twice.
            return

        if corrected:
            # A base left insecure while every environment corrects it is
            # ordinary split-settings practice, not a defect. Downgraded rather
            # than dropped, because the base can still be pointed at directly.
            severity, ceiling = Severity.LOW, Confidence.TENTATIVE
            caveats = (
                *caveats,
                f"every settings module that imports this one "
                f"{self.corrected_as}, which should override it",
            )

        where = group.module.dotted or ctx.rel(group.module.path)
        yield self.report(
            ctx,
            group,
            message=(
                f"{resolved.name} in {where}{group.describe_reach()} "
                f"{self.describe_state(resolved)}, so {self.consequence_for(resolved)}"
            ),
            severity=severity,
            ceiling=ceiling,
            extra_caveats=caveats,
        )


class FlagRule(InsecureDefaultRule):
    """A setting that must be ``True`` and defaults to ``False``.

    Django ships several of these -- ``SECURE_SSL_REDIRECT``,
    ``SESSION_COOKIE_SECURE``, ``CSRF_COOKIE_SECURE`` -- and they differ only
    in what they protect and how confidently we can complain, so those are the
    only two things a subclass supplies.
    """

    corrected_as = "sets it to True"

    def insecure(self, value: Value) -> bool:
        return could_be_off(value)
