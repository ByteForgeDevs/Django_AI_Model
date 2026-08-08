"""`DJS-028` — deployment checks Django reported that we did not.

This rule audits us. Every other rule in this tool reports a defect in the
project; this one reports a defect in our coverage, in the only place it can be
measured honestly -- against a second opinion, on the reader's own settings,
produced by the framework itself.

Two things put a check here, and from the reader's side they are one sentence:
either no rule of ours reads that setting at all, or a rule reads it and could
not settle it from the source. The second is the more interesting failure.
`DJS-006` reads `SECURE_SSL_REDIRECT` out of the settings module; when the
value is assembled from the environment it emits nothing, and Django, which
sees what the environment actually produced, says it is off. That is not a
missing rule. That is our rule missing.

Reporting it is worth more than hiding it. The finding is real -- Django is
right about the setting -- and it doubles as a standing measurement of our own
recall, taken on real projects rather than on a fixture we wrote.
"""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext
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


@register
class DeploymentCheckGap(Rule):
    meta = RuleMeta(
        id="DJS-028",
        title="Django's deployment check found a setting we did not",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.LIVE,
        rationale=(
            "`manage.py check --deploy` reads settings after every import, "
            "override and environment variable has resolved, so it can see "
            "values our static tier can only guess at. When it reports a "
            "security check that landed on no finding of ours, one of two "
            "things is true: we have no rule for that setting, or we have one "
            "and it could not settle the value from the source. Both are gaps "
            "in what this tool told you, and the setting is misconfigured "
            "either way."
        ),
        remediation=(
            "Fix the setting Django named -- its own message says how. The "
            "finding is quoted verbatim so it can be checked against a command "
            "you can run yourself: `python manage.py check --deploy`."
        ),
        fallback=(
            "Without the live tier there is no second opinion, so this rule "
            "reports nothing and the gap it measures is not smaller -- it is "
            "unmeasured. Every other `DJS` finding still stands on its own "
            "evidence, but nothing is left to say which settings Django would "
            "have flagged that we read past."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/",
            "https://docs.djangoproject.com/en/stable/ref/checks/#security",
        ),
        limitations=(
            "Only Django's `security.*` checks are considered. A model or "
            "admin check is a real problem but it is not a settings gap, and "
            "claiming it here would turn this rule into a bug tracker for "
            "Django's entire check framework.",
            "A check listed in `SILENCED_SYSTEM_CHECKS` is invisible. Django "
            "removes it from the report entirely and discloses only how many "
            "were silenced, never which, so a project can hide a gap from this "
            "rule without hiding the risk from itself.",
            "The severity is Django's level rather than a judgement of ours. A "
            "deployment warning fires on defaults as readily as on mistakes, "
            "so a project that has not configured HSTS yet reads the same as "
            "one that turned it off.",
            "This rule requires the live tier. On a static run it reports "
            "nothing, and that silence means the second opinion was never "
            "asked for rather than that our coverage was found complete.",
            "The location is the settings module Django was pointed at, not "
            "the line the setting is on. Django reports no line, and inventing "
            "one would be the fabrication this family exists to avoid.",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        if not ctx.live or not ctx.deployment_gaps:
            return

        report = ctx.deployment_report
        location = self._locate(ctx)
        for check in sorted(ctx.deployment_gaps):
            message = report.by_id(check) if report is not None else None
            if message is None:
                # The gap set is built from the report, so this cannot happen
                # unless a caller assembled a context by hand. Reporting a
                # check id with nothing to quote would be a finding with no
                # evidence, which is the one thing this tool must not emit.
                continue
            yield self.finding(
                location=location,
                severity=message.severity,
                message=(
                    f"Django's deployment check reports `{check}` and no rule "
                    f"of ours did: {message.text}"
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.COMMAND_OUTPUT,
                        content=message.describe()
                        + (f"\n\tHINT: {message.hint}" if message.hint else ""),
                        source="manage.py check --deploy",
                    ),
                ),
            )

    def _locate(self, ctx: ProjectContext) -> Location:
        """The settings module Django was told to use, or the project root.

        Django's report carries no file and no line. The entrypoint settings
        module is the nearest true thing we can point at: it is where the
        reader will make the change, even when the value arrives from
        somewhere else.
        """
        entrypoint = next((s for s in ctx.settings_modules if s.is_entrypoint), None)
        chosen = entrypoint or next(iter(ctx.settings_modules), None)
        if chosen is None:
            return Location(file="manage.py", line=1)
        return Location(file=str(chosen.path.relative_to(ctx.root)), line=1)
