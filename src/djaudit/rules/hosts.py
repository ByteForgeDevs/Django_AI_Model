"""DJS -- host, origin, and framing.

These settings decide which requests Django is willing to answer and who is
allowed to embed or call the result. They share a shape that the transport
family does not: the value is a *list*, so the question is not "is this on" but
"what is in it", and one bad entry is enough.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Family, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import (
    InsecureDefaultRule,
    SettingGroup,
    could_be_true,
)
from djaudit.settings import ResolvedSetting, SettingsView
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


_CSRF_DOCS = "https://docs.djangoproject.com/en/stable/ref/settings/#csrf-trusted-origins"


def csrf_pattern(entry: str) -> str:
    """The host pattern Django actually derives from one trusted origin.

    ``CsrfViewMiddleware`` does not compare the configured string to anything.
    It takes ``urlsplit(origin).netloc`` and strips leading asterisks, and that
    result is what is matched -- so this function, not the entry, is what the
    setting means.
    """
    return urlsplit(entry).netloc.lstrip("*")


def csrf_reduction(entry: str) -> str:
    """What ``entry`` ends up being compared against, for a message."""
    pattern = csrf_pattern(entry)
    return repr(pattern) if pattern else "nothing"


def csrf_verdict(entry: object) -> str:
    """Classify one entry as ``"fine"``, ``"broad"``, ``"tld"`` or ``"inert"``.

    ``is_same_domain`` treats a pattern as a subdomain wildcard only when it
    begins with a dot, and as an exact hostname otherwise. Everything follows
    from that one line of Django.
    """
    if not isinstance(entry, str):
        return "fine"
    pattern = csrf_pattern(entry)
    if "://" not in entry:
        # Origin and Referer headers both arrive as scheme://host, and both of
        # Django's comparisons go through netloc, which an entry with no scheme
        # does not have. It matches nothing at either end.
        return "inert"
    if "*" not in entry:
        return "fine"
    if not pattern.startswith("."):
        # The asterisk only widens anything when a dot follows it. Anywhere
        # else it is left in the hostname, or stripped to nothing, and either
        # way the entry stops meaning what it was written to mean.
        return "inert"
    labels = [label for label in pattern.strip(".").split(".") if label]
    return "broad" if len(labels) > 1 else "tld"


def csrf_entries(value: Value, verdict: str) -> tuple[str, ...]:
    """Every entry of ``value``, on any branch, carrying ``verdict``."""
    seen: dict[str, None] = {}
    for entries in entries_of(value):
        for entry in entries or ():
            if isinstance(entry, str) and csrf_verdict(entry) == verdict:
                seen[entry] = None
    return tuple(seen)


@register
class CsrfTrustedOriginsTooBroad(InsecureDefaultRule):
    """``CSRF_TRUSTED_ORIGINS`` trusts too much, or silently trusts nothing."""

    setting = "CSRF_TRUSTED_ORIGINS"
    ceiling = Confidence.FIRM
    corrected_as = "narrows it"

    def insecure(self, value: Value) -> bool:
        return any_entry(value, lambda entry: csrf_verdict(entry) != "fine")

    def _worst(self, resolved: ResolvedSetting) -> str:
        for verdict in ("tld", "inert", "broad"):
            if csrf_entries(resolved.value, verdict):
                return verdict
        return "fine"

    def severity_for(self, resolved: ResolvedSetting) -> Severity | None:
        # Trusting every host under a TLD is not a judgement call about anyone's
        # infrastructure, so it outranks both of the others.
        return {
            "tld": Severity.HIGH,
            "inert": Severity.MEDIUM,
            "broad": Severity.MEDIUM,
        }.get(self._worst(resolved))

    def ceiling_for(self, resolved: ResolvedSetting) -> Confidence | None:
        # Whether an entry does anything is a fact about the string. Whether
        # trusting your own subdomains is safe depends on who can put content
        # on them, which is the one thing the source cannot tell us -- so that
        # branch stays out of a default run and waits to be asked for.
        return Confidence.TENTATIVE if self._worst(resolved) == "broad" else None

    def describe_state(self, resolved: ResolvedSetting) -> str:
        worst = self._worst(resolved)
        entries = csrf_entries(resolved.value, worst)
        quoted = ", ".join(repr(entry) for entry in entries)
        if worst == "tld":
            return f"trusts every host under a top-level domain via {quoted}"
        if worst == "broad":
            return f"trusts every subdomain via {quoted}"
        # Naming the pattern Django ends up with is the whole point of this
        # branch: the entry looks right, and the derived value is the evidence
        # that it is not.
        if len(entries) == 1:
            return f"lists {quoted}, which Django reduces to {csrf_reduction(entries[0])}"
        pairs = ", ".join(f"{entry!r} to {csrf_reduction(entry)}" for entry in entries)
        return f"lists entries Django reduces to something that matches no origin: {pairs}"

    def consequence_for(self, resolved: ResolvedSetting) -> str:
        worst = self._worst(resolved)
        if worst == "tld":
            return (
                "any site on the internet whose name ends that way can post a "
                "state-changing request to this one and have the CSRF check wave it "
                "through, which is the protection removed rather than relaxed"
            )
        if worst == "inert":
            return (
                "the cross-origin requests it was added to allow are still rejected -- "
                "the setting looks configured, the CSRF failures look unrelated to it, "
                "and the usual next step is to widen the list further"
            )
        return (
            "anything that can serve a page from any subdomain -- a tenant, a "
            "docs host, a stale CNAME someone else has claimed -- can make "
            "authenticated state-changing requests on behalf of a logged-in user"
        )

    meta = RuleMeta(
        id="DJS-014",
        title="CSRF_TRUSTED_ORIGINS is too broad or has no effect",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "CSRF_TRUSTED_ORIGINS is the list of origins allowed to make state-changing "
            "requests from somewhere other than this site, so every entry is a hostname "
            "whose compromise becomes this site's compromise. Django derives the pattern "
            "it matches by taking urlsplit(origin).netloc and stripping leading "
            "asterisks, then treating the result as a subdomain wildcard only if it "
            "begins with a dot. Two failures follow. A wildcard over a domain whose "
            "subdomains are not all yours trusts whoever holds them. And an entry with "
            "no scheme -- the spelling this setting required before Django 4.0 -- has no "
            "netloc at all, so it matches nothing, in both the Origin and the Referer "
            "check. Django reports that second one as 4_0.E001, but system checks do not "
            "run under gunicorn or uvicorn, so a deployment that never invokes manage.py "
            "will not hear about it."
        ),
        remediation=(
            "Write each origin as a full scheme://host, and prefer naming hosts to "
            "wildcarding them. A wildcard needs the dot -- 'https://*.example.com' works, "
            "'https://*' and 'https://*example.com' do not do what they look like. Keep "
            "https:// entries rather than http://: a trusted plaintext origin can be "
            "forged by anyone on the network path. If a wildcard is genuinely needed, it "
            "is only as trustworthy as your control over every name under it, so it does "
            "not belong on a domain where customers or a hosting provider can create "
            "subdomains."
        ),
        references=(
            _CSRF_DOCS,
            "https://docs.djangoproject.com/en/stable/ref/csrf/",
        ),
    )


_CORS_DOCS = "https://github.com/adamchainz/django-cors-headers#configuration"
CORS_MIDDLEWARE = "corsheaders.middleware.CorsMiddleware"


def installs_middleware(view: SettingsView, dotted: str) -> bool | None:
    """Whether ``MIDDLEWARE`` contains ``dotted``, or ``None`` if unreadable.

    The three-valued answer is the point. A setting that configures middleware
    which is not installed does nothing, so silence is right -- but only when we
    genuinely read the list and it was not there. Plenty of projects build
    ``MIDDLEWARE`` conditionally, and treating "could not read" as "not
    installed" would turn every one of those into a missed finding.
    """
    middleware = view.get("MIDDLEWARE")
    if all(entries is None for entries in entries_of(middleware.value)):
        return None
    return any_entry(middleware.value, lambda entry: entry == dotted)


@register
class CorsAllowsAllOrigins(InsecureDefaultRule):
    """``django-cors-headers`` is configured to answer every origin."""

    setting = "CORS_ALLOW_ALL_ORIGINS"
    aliases = ("CORS_ORIGIN_ALLOW_ALL",)
    ceiling = Confidence.FIRM
    corrected_as = "turns it off"

    consequence = (
        "every response the middleware touches carries "
        "Access-Control-Allow-Origin: *, which lets any page on the internet read "
        "it -- intended behaviour for an API whose data is already public, and a "
        "way out of the network for anything reachable only from inside it, "
        "because a browser on the corporate LAN is a route to an intranet service "
        "that no firewall rule covers"
    )

    def insecure(self, value: Value) -> bool:
        return could_be_true(value)

    def applies(self, ctx: ProjectContext, group: SettingGroup) -> bool:
        view = self.views.get(group.module.dotted)
        if view is None:
            return False
        # Credentials plus a wildcard is a different and far worse defect, and
        # DJS-016 reports it. Saying it twice would be one mistake, two tickets.
        if could_be_true(view.get("CORS_ALLOW_CREDENTIALS").value):
            return False
        # The setting is read by middleware. Without the middleware there is no
        # header, no matter what the setting says.
        return installs_middleware(view, CORS_MIDDLEWARE) is not False

    meta = RuleMeta(
        id="DJS-015",
        title="CORS is open to every origin",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "The same-origin policy stops one site reading another's responses, and CORS "
            "is how a site waives that. Waiving it for every origin is deliberate and "
            "correct for an API whose data is already public, so this is reported at "
            "medium rather than treated as a breach. It is worth reading twice in two "
            "cases. The first is an internal service: a wildcard means any page an "
            "employee visits can read it through their browser, which sits inside the "
            "network perimeter, and no firewall rule sees that request. The second is a "
            "site that later switches CORS_ALLOW_CREDENTIALS on -- at that point "
            "django-cors-headers stops sending '*' and starts echoing the caller's own "
            "origin back, which turns this setting into cross-origin account access. "
            "django-cors-headers still honours the pre-3.5 name CORS_ORIGIN_ALLOW_ALL, "
            "so both spellings are read the way the package reads them."
        ),
        remediation=(
            "Replace it with CORS_ALLOWED_ORIGINS listing the front-ends that call this "
            "API, each as a full scheme://host. If the set is genuinely open-ended, "
            "CORS_ALLOWED_ORIGIN_REGEXES will narrow it further than a wildcard, and "
            "CORS_URLS_REGEX will confine CORS to the API paths rather than applying it "
            "to the whole site. If the wildcard is intentional, make sure it stays paired "
            "with CORS_ALLOW_CREDENTIALS left off, because that pairing is the only thing "
            "keeping browsers from sending cookies with these requests."
        ),
        references=(
            _CORS_DOCS,
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS",
        ),
    )
