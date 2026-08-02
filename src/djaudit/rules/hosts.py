"""DJS -- host, origin, and framing.

These settings decide which requests Django is willing to answer and who is
allowed to embed or call the result. They share a shape that the transport
family does not: the value is a *list*, so the question is not "is this on" but
"what is in it", and one bad entry is enough.
"""

from __future__ import annotations

from collections.abc import Callable

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Family, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import (
    InsecureDefaultRule,
    SettingGroup,
    could_be_true,
)
from djaudit.settings import ResolvedSetting
from djaudit.values import Value

_HOSTS_DOCS = "https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts"


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


@register
class AllowedHostsUnrestricted(InsecureDefaultRule):
    """``ALLOWED_HOSTS`` accepts any Host header, or answers nothing at all."""

    setting = "ALLOWED_HOSTS"
    ceiling = Confidence.FIRM
    """A proxy can enforce the Host header before Django ever sees it, and we
    cannot see the proxy. The list really does say what it says; whether
    anything else is filtering is the part we are inferring."""

    corrected_as = "names its own hosts"

    def insecure(self, value: Value) -> bool:
        return any_entry(value, lambda entry: entry == "*") or definitely_empty(value)

    def _wildcarded(self, resolved: ResolvedSetting) -> bool:
        return any_entry(resolved.value, lambda entry: entry == "*")

    def severity_for(self, resolved: ResolvedSetting) -> Severity | None:
        # An open host list is an attack surface; an empty one is a site that
        # answers nothing. Both are worth saying and they are not the same size.
        return Severity.HIGH if self._wildcarded(resolved) else Severity.LOW

    def applies(self, ctx: ProjectContext, group: SettingGroup) -> bool:
        """An empty list is only a problem once DEBUG is off.

        With DEBUG on, Django quietly allows localhost and its variants, which
        is what makes an empty list the normal and correct state of a settings
        module you only ever run locally.
        """
        if self._wildcarded(group.setting):
            return True
        view = self.views.get(group.module.dotted)
        return view is not None and not could_be_true(view.get("DEBUG").value)

    def describe_state(self, resolved: ResolvedSetting) -> str:
        if self._wildcarded(resolved):
            return 'contains "*"'
        return "is empty" if resolved.is_assigned else "is never set, so it is empty"

    def consequence_for(self, resolved: ResolvedSetting) -> str:
        if self._wildcarded(resolved):
            return (
                "Django answers a request whatever Host header it carries, and it then "
                "builds absolute URLs from that header -- including the ones in password "
                "reset emails, which is how an attacker turns this into a link to their "
                "own server that arrives in your user's inbox from you"
            )
        return (
            "Django rejects every request with a 400 once DEBUG is off, so this is a "
            "deployment that serves nothing rather than one that is exposed -- but it "
            "fails at the first request in production and passes every test locally"
        )

    meta = RuleMeta(
        id="DJS-013",
        title="ALLOWED_HOSTS is a wildcard or empty",
        family=Family.DJS,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "ALLOWED_HOSTS is the only thing that ties a Django response to the hostname "
            'it was asked for. With "*" in the list the check is off, and the Host header '
            "-- which the client controls -- flows into every absolute URL Django builds. "
            "The consequence people underestimate is password reset poisoning: "
            "django.contrib.auth builds the reset link from the request, so an attacker "
            "who submits a reset for someone else's account with their own Host header "
            "gets a genuine reset token delivered to the victim as a link pointing at the "
            "attacker's server. Cache poisoning against a shared cache is the other. An "
            "empty list is the opposite failure and still worth reporting: it is correct "
            "while DEBUG is on and returns 400 for everything the moment it is not."
        ),
        remediation=(
            "List the hostnames the site actually answers to. A leading dot is the "
            'subdomain wildcard Django understands -- ".example.com" matches '
            'example.com and everything under it -- while "*.example.com" is not '
            "special-cased and will simply never match. If the hostname genuinely varies "
            "at deployment time, read it from the environment rather than opening the "
            "list, and remember that a proxy enforcing the Host header is a good second "
            "layer but not a substitute, because it does not stop anything that reaches "
            "Django by another route."
        ),
        references=(
            _HOSTS_DOCS,
            "https://docs.djangoproject.com/en/stable/topics/security/#host-header-validation",
        ),
    )
