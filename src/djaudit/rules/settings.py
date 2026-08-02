"""DJS -- settings and deployment hardening."""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext, SettingsModule, SettingsRole
from djaudit.models import Confidence, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import SettingsRule
from djaudit.settings import Definition, ResolvedSetting, SettingsView
from djaudit.values import Value

_GRADING: dict[SettingsRole, tuple[Severity, Confidence]] = {
    SettingsRole.PRODUCTION: (Severity.CRITICAL, Confidence.CERTAIN),
    SettingsRole.PRIMARY: (Severity.CRITICAL, Confidence.FIRM),
    SettingsRole.UNKNOWN: (Severity.HIGH, Confidence.FIRM),
    SettingsRole.BASE: (Severity.HIGH, Confidence.FIRM),
}


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


@register
class DebugEnabled(SettingsRule):
    """``DEBUG`` can be true in a settings module that can reach production."""

    meta = RuleMeta(
        id="DJS-001",
        title="DEBUG enabled in a production-reachable settings module",
        family=Family.DJS,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "With DEBUG=True, Django returns a full traceback page to anyone who can "
            "trigger an exception. That page exposes source code, local variable values, "
            "the settings module (including secrets Django fails to mask), and recent SQL. "
            "Django also retains every executed query in memory, so a long-running "
            "production process leaks memory until it is killed. ALLOWED_HOSTS validation "
            "is skipped as well. This is the single most damaging Django misconfiguration."
        ),
        remediation=(
            "Set DEBUG = False and drive it from the environment, e.g.\n"
            "    DEBUG = os.environ.get('DJANGO_DEBUG', '') == '1'\n"
            "then confirm with `python manage.py check --deploy`."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/settings/#debug",
            "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/",
        ),
    )

    def inspect(
        self, ctx: ProjectContext, module: SettingsModule, view: SettingsView
    ) -> Iterator[Finding]:
        resolved = view.get("DEBUG")
        # An unset DEBUG is already False, and an unresolvable one is not
        # evidence of anything. Only an assignment we could read counts.
        if not resolved.is_explicit or not could_be_true(resolved.value):
            return
        culprit = _culprit(resolved)
        if culprit is None:
            return
        yield self._build(ctx, module, resolved, culprit, _overridden(self.views, module))

    def _build(
        self,
        ctx: ProjectContext,
        module: SettingsModule,
        resolved: ResolvedSetting,
        culprit: Definition,
        overridden: bool,
    ) -> Finding:
        severity, ceiling = _GRADING.get(module.role, (Severity.HIGH, Confidence.FIRM))
        caveats: tuple[str, ...] = ()

        if overridden:
            severity, ceiling = Severity.LOW, Confidence.TENTATIVE
            caveats = (
                "every settings module that imports this one sets DEBUG = False, "
                "which should override it",
            )

        where = module.dotted or ctx.rel(module.path)
        inherited = f", inherited from {culprit.dotted}" if culprit.dotted != module.dotted else ""

        return self.report(
            ctx,
            module,
            resolved,
            message=(
                f"DEBUG can be True in {where}, classified as a "
                f"{module.role.value} settings module{inherited}"
            ),
            severity=severity,
            ceiling=ceiling,
            extra_caveats=caveats,
        )


def _culprit(resolved: ResolvedSetting) -> Definition | None:
    """The last assignment that leaves DEBUG able to be true."""
    for definition in reversed(resolved.definitions):
        if could_be_true(definition.value):
            return definition
    return resolved.definition


def _overridden(views: dict[str, SettingsView], module: SettingsModule) -> bool:
    """Whether every settings module inheriting from ``module`` turns DEBUG off.

    A base module is not deployed by itself, so DEBUG=True there is a latent
    hazard rather than a live one when every module that imports it switches
    DEBUG off. Asking the resolver replaces a scan for `DEBUG = False` anywhere
    in the project, which said nothing about whether the two were connected.
    """
    if module.role is not SettingsRole.BASE:
        return False

    # A development module inheriting the base and leaving DEBUG on is the
    # point of a development module, so it says nothing about production.
    heirs = [
        view
        for dotted, view in views.items()
        if dotted != module.dotted
        and module.dotted in view.chain
        and view.module.role.reaches_production
    ]
    return bool(heirs) and all(heir.get("DEBUG").is_always(False) for heir in heirs)
