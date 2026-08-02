"""DJS -- secret management.

A secret that resolves to a value we can read is a secret anyone with
repository access can read. These rules deliberately never quote the value they
found: a finding travels into JSON artifacts, SARIF uploads and CI logs, all of
which are shared more widely than the source it came from.
"""

from __future__ import annotations

from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import SettingGroup, SettingsRule, literal_text


@register
class HardcodedSecretKey(SettingsRule):
    """``SECRET_KEY`` resolves to a value that is readable in the source."""

    setting = "SECRET_KEY"
    ceiling = Confidence.CERTAIN

    meta = RuleMeta(
        id="DJS-002",
        title="SECRET_KEY is readable in the source",
        family=Family.DJS,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "SECRET_KEY signs session cookies, password reset tokens, and the CSRF "
            "token. Anyone who knows it can forge a session for any user, including "
            "staff, and mint password reset links -- no password required and nothing "
            "unusual in the logs. A key committed to the repository is known to every "
            "person who has ever had read access, every fork, and every CI provider "
            "that cached the checkout, and it stays known after they leave. Rotating "
            "it is the only remedy, and rotation logs everyone out, so the cost only "
            "grows."
        ),
        remediation=(
            "Generate a fresh key and load it from the environment:\n"
            '    python -c "from django.core.management.utils import '
            'get_random_secret_key; print(get_random_secret_key())"\n'
            "    SECRET_KEY = os.environ['DJANGO_SECRET_KEY']\n"
            "Subscripting rather than .get() makes a missing key fail at startup "
            "instead of silently falling back to the committed one. Treat the old "
            "key as compromised and rotate it, remembering that this logs every user "
            "out."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/settings/#secret-key",
            "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/#secret-key",
            "https://cwe.mitre.org/data/definitions/798.html",
        ),
    )

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        if not resolved.is_explicit:
            return

        secret = literal_text(resolved.value)
        # An empty key is a different defect: Django refuses to start, so it is
        # a broken deployment rather than a forgeable one.
        if not secret:
            return

        where = group.module.dotted or ctx.rel(group.module.path)
        how = (
            "as the fallback when the environment does not supply one"
            if resolved.value.env_dependent
            else "directly in the source"
        )

        yield self.report(
            ctx,
            group,
            message=(
                f"SECRET_KEY is set {how} in {where}{group.describe_reach()}, so anyone "
                f"who can read the repository can forge sessions and password reset tokens"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=describe_secret(secret),
                    source="djaudit secret analysis",
                ),
            ),
            redact=secret,
        )


def describe_secret(secret: str) -> str:
    """Characterise a secret without disclosing it."""
    classes = {
        "lowercase": any(c.islower() for c in secret),
        "uppercase": any(c.isupper() for c in secret),
        "digits": any(c.isdigit() for c in secret),
        "symbols": any(not c.isalnum() for c in secret),
    }
    present = ", ".join(name for name, found in classes.items() if found) or "none"
    return (
        f"length={len(secret)}  distinct characters={len(set(secret))}  "
        f"character classes={present}\n"
        "value withheld: it is a live credential and a finding travels further "
        "than the source does"
    )
