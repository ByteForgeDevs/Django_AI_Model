"""DJS -- settings and deployment hardening."""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.astutils import Assignment, literal, module_assignments
from djaudit.context import ProjectContext, SettingsModule, SettingsRole
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import Rule, RuleMeta, register

_GRADING: dict[SettingsRole, tuple[Severity, Confidence]] = {
    SettingsRole.PRODUCTION: (Severity.CRITICAL, Confidence.CERTAIN),
    SettingsRole.PRIMARY: (Severity.CRITICAL, Confidence.FIRM),
    SettingsRole.UNKNOWN: (Severity.HIGH, Confidence.FIRM),
    SettingsRole.BASE: (Severity.HIGH, Confidence.FIRM),
}

_DOWNGRADE: dict[Confidence, Confidence] = {
    Confidence.CERTAIN: Confidence.FIRM,
    Confidence.FIRM: Confidence.TENTATIVE,
    Confidence.TENTATIVE: Confidence.TENTATIVE,
}


@register
class DebugEnabled(Rule):
    """``DEBUG = True`` in a settings module that can reach production."""

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

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        overridden = self._debug_disabled_downstream(ctx)

        for module in ctx.settings_modules:
            # DEBUG=True is correct in development and test settings. Reporting it
            # there is the fastest way to train people to ignore the tool.
            if not module.role.reaches_production:
                continue

            for assignment in self._debug_true_assignments(ctx, module):
                yield self._build(ctx, module, assignment, overridden)

    @staticmethod
    def _debug_true_assignments(
        ctx: ProjectContext, module: SettingsModule
    ) -> Iterator[Assignment]:
        tree = ctx.parse(module.path)
        if tree is None:
            return
        for assignment in module_assignments(tree):
            if assignment.name == "DEBUG" and literal(assignment.value) is True:
                yield assignment

    @staticmethod
    def _debug_disabled_downstream(ctx: ProjectContext) -> bool:
        """Whether some production-role module unconditionally sets ``DEBUG = False``.

        In a split-settings layout it is normal for a base module to enable DEBUG
        and for production to switch it off. When we can see that override we keep
        the base finding but drop it to informational, rather than dropping it
        entirely -- the override might itself be removed later.
        """
        for module in ctx.settings_modules:
            if module.role not in (SettingsRole.PRODUCTION, SettingsRole.PRIMARY):
                continue
            tree = ctx.parse(module.path)
            if tree is None:
                continue
            for assignment in module_assignments(tree):
                if (
                    assignment.name == "DEBUG"
                    and not assignment.conditional
                    and literal(assignment.value) is False
                ):
                    return True
        return False

    def _build(
        self,
        ctx: ProjectContext,
        module: SettingsModule,
        assignment: Assignment,
        overridden: bool,
    ) -> Finding:
        severity, confidence = _GRADING.get(module.role, (Severity.HIGH, Confidence.FIRM))
        notes: list[str] = []

        if assignment.conditional:
            confidence = _DOWNGRADE[confidence]
            notes.append("assignment is inside a conditional block, so it may not execute")

        if overridden and module.role == SettingsRole.BASE:
            severity, confidence = Severity.LOW, Confidence.TENTATIVE
            notes.append(
                "a production settings module unconditionally sets DEBUG = False, "
                "which should override this"
            )

        detail = f" ({'; '.join(notes)})" if notes else ""
        message = (
            f"DEBUG is set to True in {module.dotted or ctx.rel(module.path)}, "
            f"classified as a {module.role.value} settings module{detail}."
        )

        evidence = (
            Evidence(
                kind=EvidenceKind.SOURCE,
                content=ctx.snippet(
                    module.path, assignment.node.lineno, assignment.node.end_lineno
                ),
                source=ctx.rel(module.path),
            ),
            Evidence(
                kind=EvidenceKind.CONFIG,
                content=(
                    f"module={module.dotted}  role={module.role.value}  "
                    f"entrypoint={module.is_entrypoint}  conditional={assignment.conditional}"
                ),
                source="djaudit settings discovery",
            ),
        )

        return self.finding(
            location=ctx.location(module.path, assignment.node),
            message=message,
            evidence=evidence,
            severity=severity,
            confidence=confidence,
            properties={"settings_role": module.role.value, "settings_module": module.dotted},
        )
