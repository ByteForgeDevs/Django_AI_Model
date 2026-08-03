"""DJS -- settings and deployment hardening."""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext, SettingsRole
from djaudit.models import Confidence, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import SettingGroup, SettingsRule, could_be_true
from djaudit.settings import Definition, ResolvedSetting

_GRADING: dict[SettingsRole, tuple[Severity, Confidence]] = {
    SettingsRole.PRODUCTION: (Severity.CRITICAL, Confidence.CERTAIN),
    SettingsRole.PRIMARY: (Severity.CRITICAL, Confidence.FIRM),
    SettingsRole.UNKNOWN: (Severity.HIGH, Confidence.FIRM),
    SettingsRole.BASE: (Severity.HIGH, Confidence.FIRM),
}


@register
class DebugEnabled(SettingsRule):
    """``DEBUG`` can be true in a settings module that can reach production."""

    setting = "DEBUG"

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
        limitations=(
            "Reads the module, not the process. A deployment can hand Django a different "
            "settings module than the one this looks like, or override DEBUG from an "
            "environment variable we resolved to a default; that is why an env-dependent value "
            "is reported at lower confidence rather than at certainty.",
            "Classifying a module as production-reachable is a judgement about names and "
            "imports. A module with an unusual name that only ever runs locally can be misread "
            "as one that ships.",
        ),
    )

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        # An unset DEBUG is already False, and an unresolvable one is not
        # evidence of anything. Only an assignment we could read counts.
        if not resolved.is_explicit or not could_be_true(resolved.value):
            return
        culprit = _culprit(resolved)
        if culprit is None:
            return
        overridden = self.overridden(group.module, lambda rs: rs.is_always(False))
        yield self._build(ctx, group, culprit, overridden)

    def _build(
        self,
        ctx: ProjectContext,
        group: SettingGroup,
        culprit: Definition,
        overridden: bool,
    ) -> Finding:
        module = group.module
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
            group,
            message=(
                f"DEBUG can be True in {where}, classified as a "
                f"{module.role.value} settings module{inherited}{group.describe_reach()}"
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
