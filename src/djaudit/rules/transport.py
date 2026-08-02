"""DJS -- transport and cookie security.

These are the settings that decide whether a session survives contact with a
plain HTTP request. They share a shape -- a flag that must be on and ships off
-- but not a confidence: whether ``SECURE_SSL_REDIRECT`` matters depends on a
proxy we cannot see, while a cookie without ``Secure`` is sent over HTTP by the
browser no matter what sits in front of Django. The ``ceiling`` on each rule is
where that difference is recorded.
"""

from __future__ import annotations

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Family, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import (
    FlagRule,
    InsecureDefaultRule,
    SettingGroup,
    could_be_off,
    could_be_under,
)
from djaudit.settings import ResolvedSetting
from djaudit.values import Value

_HTTPS_CHECKLIST = "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/#https"


@register
class SslRedirectDisabled(FlagRule):
    """``SECURE_SSL_REDIRECT`` is off, so Django serves plain HTTP."""

    setting = "SECURE_SSL_REDIRECT"
    ceiling = Confidence.FIRM
    """A redirect at nginx or the load balancer does the same job, and we cannot
    see one from here. The flag really is off; whether that matters is the part
    we are inferring, so we do not claim certainty about it."""

    consequence = (
        "a request that arrives over plain HTTP is answered over plain HTTP rather "
        "than redirected, unless something in front of Django is doing it instead"
    )

    meta = RuleMeta(
        id="DJS-006",
        title="SECURE_SSL_REDIRECT is not enabled",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "The first request a browser makes to a bare hostname is plain HTTP, and "
            "if Django answers it rather than redirecting, everything in that exchange "
            "-- session cookie, credentials, response body -- crosses the network in "
            "clear text and can be modified in transit. This is commonly handled by a "
            "reverse proxy instead, which is a perfectly good answer; the risk is "
            "believing that and being wrong, because nothing fails visibly when the "
            "redirect is missing."
        ),
        remediation=(
            "Set SECURE_SSL_REDIRECT = True, or confirm the proxy in front of Django "
            "redirects HTTP to HTTPS and record that decision somewhere the next "
            "person will find it. Enabling it in Django as well costs one redirect on "
            "requests that should not be arriving anyway, and it keeps working if the "
            "proxy is ever reconfigured."
        ),
        references=(
            _HTTPS_CHECKLIST,
            "https://docs.djangoproject.com/en/stable/ref/settings/#secure-ssl-redirect",
        ),
    )


@register
class SessionCookieNotSecure(FlagRule):
    """``SESSION_COOKIE_SECURE`` is off, so the session cookie travels over HTTP."""

    setting = "SESSION_COOKIE_SECURE"
    ceiling = Confidence.CERTAIN
    """Nothing in front of Django changes this. The flag sets an attribute on the
    cookie, and the browser is what acts on it."""

    consequence = (
        "the browser will send the session cookie over plain HTTP, where anyone on "
        "the network path can read it and use it"
    )

    meta = RuleMeta(
        id="DJS-009",
        title="session cookie is not marked Secure",
        family=Family.DJS,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "Without this flag the browser attaches the session cookie to plain HTTP "
            "requests, so a single such request -- a typed hostname, an old bookmark, "
            "an http:// image in an email -- puts the cookie on the wire in clear "
            "text. Whoever reads it is logged in as that user until the session "
            "expires, and there is nothing in the logs to distinguish them from the "
            "real one. A reverse proxy does not help: this is an attribute of the "
            "cookie, and the browser is what enforces it."
        ),
        remediation=(
            "Set SESSION_COOKIE_SECURE = True in every settings module that can reach "
            "production. Existing sessions keep their old attributes until they are "
            "reissued, so consider cycling them if you believe one has been exposed."
        ),
        references=(
            _HTTPS_CHECKLIST,
            "https://docs.djangoproject.com/en/stable/ref/settings/#session-cookie-secure",
            "https://cwe.mitre.org/data/definitions/614.html",
        ),
    )


@register
class CsrfCookieNotSecure(FlagRule):
    """``CSRF_COOKIE_SECURE`` is off, so the CSRF token travels over HTTP."""

    setting = "CSRF_COOKIE_SECURE"
    ceiling = Confidence.CERTAIN

    consequence = (
        "the browser will send the CSRF token over plain HTTP, where it can be read "
        "and then used to forge a request the user never made"
    )

    meta = RuleMeta(
        id="DJS-010",
        title="CSRF cookie is not marked Secure",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "The CSRF token is only useful to an attacker who can also make the "
            "victim's browser issue a request, which is why this ranks below the "
            "session cookie rather than beside it. But the two normally travel "
            "together, so a cookie exposed on plain HTTP usually means both were, and "
            "the token is what stands between a stolen session and a state-changing "
            "request being accepted."
        ),
        remediation=(
            "Set CSRF_COOKIE_SECURE = True alongside SESSION_COOKIE_SECURE. They "
            "protect the two halves of the same exchange and there is no sensible "
            "configuration that wants one without the other."
        ),
        references=(
            _HTTPS_CHECKLIST,
            "https://docs.djangoproject.com/en/stable/ref/settings/#csrf-cookie-secure",
        ),
    )


@register
class SessionCookieNotHttpOnly(FlagRule):
    """``SESSION_COOKIE_HTTPONLY`` is off, so scripts can read the session cookie."""

    setting = "SESSION_COOKIE_HTTPONLY"
    ceiling = Confidence.CERTAIN

    consequence = (
        "JavaScript running on the page can read the session cookie, which turns any "
        "cross-site scripting flaw into a stolen session"
    )

    meta = RuleMeta(
        id="DJS-011",
        title="session cookie is readable by JavaScript",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "HttpOnly is what decides whether an XSS flaw costs you one user's page or "
            "that user's whole session. Django sets it on by default, so a project "
            "reaching this rule turned it off on purpose -- usually so a frontend "
            "could read the cookie, which means the exposure is deliberate but the "
            "consequence is often not."
        ),
        remediation=(
            "Set SESSION_COOKIE_HTTPONLY = True, which is Django's default. If a "
            "script genuinely needs to know whether the user is signed in, send that "
            "in the page or from an endpoint rather than by making the session cookie "
            "readable."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/settings/#session-cookie-httponly",
            "https://owasp.org/www-community/HttpOnly",
        ),
    )


ONE_YEAR = 31_536_000
"""The floor the HSTS preload list requires, and the usual target once a site
has finished ramping up. Chosen because it is a published requirement rather
than a number we picked."""

_HSTS_DOCS = (
    "https://docs.djangoproject.com/en/stable/ref/middleware/#http-strict-transport-security"
)


def _duration(seconds: int) -> str:
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds / size
            rendered = f"{count:.0f}" if count == int(count) else f"{count:.1f}"
            return f"{seconds:,} seconds (about {rendered} {unit}{'' if rendered == '1' else 's'})"
    return f"{seconds:,} seconds"


@register
class HstsNotEnforced(InsecureDefaultRule):
    """``SECURE_HSTS_SECONDS`` is unset, zero, or shorter than a year."""

    setting = "SECURE_HSTS_SECONDS"
    ceiling = Confidence.FIRM
    """A CDN or nginx may be sending the header instead, and that is a very
    common place to put it. We cannot see one from here, so we never claim
    certainty that the header is missing from the response."""

    corrected_as = "sets a longer max-age"

    def insecure(self, value: Value) -> bool:
        return could_be_under(value, ONE_YEAR)

    def describe_state(self, resolved: ResolvedSetting) -> str:
        literal = resolved.value.literal
        if resolved.is_default or literal == 0:
            return "is never set" if resolved.is_default else "is 0"
        if isinstance(literal, int) and not isinstance(literal, bool):
            return f"is {_duration(literal)}, short of the one year"
        return f"is {resolved.value.describe()}"

    def consequence_for(self, resolved: ResolvedSetting) -> str:
        literal = resolved.value.literal
        if isinstance(literal, int) and not isinstance(literal, bool) and 0 < literal < ONE_YEAR:
            return (
                "the browser stops enforcing HTTPS again that soon after its last visit, "
                "and a visitor who returns after the window has lapsed can still be "
                "stripped back to plain HTTP on their first request -- which is usually a "
                "ramp-up that was started and never finished"
            )
        return (
            "no Strict-Transport-Security header is sent and the browser is willing to try "
            "plain HTTP first, which is the request an attacker on the network path needs "
            "in order to intercept it before any redirect can happen"
        )

    meta = RuleMeta(
        id="DJS-007",
        title="HSTS not enforced for at least a year",
        family=Family.DJS,
        severity=Severity.LOW,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Redirecting HTTP to HTTPS still leaves the very first request in the clear, "
            "and that request is the one an attacker needs. HSTS closes the gap by telling "
            "the browser never to use plain HTTP for this host again. Low severity rather "
            "than high because the header is very often set at nginx, a load balancer or a "
            "CDN instead, and from inside the repository we cannot see that."
        ),
        remediation=(
            "Set SECURE_HSTS_SECONDS = 31536000 once the site is served over HTTPS "
            "everywhere, including every subdomain if SECURE_HSTS_INCLUDE_SUBDOMAINS is on. "
            "Ramp up rather than jumping straight there -- a few minutes, then a day, then "
            "a year -- because the header is sticky: a browser that has seen it refuses "
            "plain HTTP for the full max-age and there is no way to recall it early. If a "
            "proxy in front of Django already sends the header, set it there and suppress "
            "this finding instead of sending it twice."
        ),
        references=(
            _HTTPS_CHECKLIST,
            _HSTS_DOCS,
            "https://docs.djangoproject.com/en/stable/ref/settings/#secure-hsts-seconds",
        ),
    )


@register
class HstsSubdomainsExcluded(InsecureDefaultRule):
    """``SECURE_HSTS_INCLUDE_SUBDOMAINS`` is off while HSTS is switched on."""

    setting = "SECURE_HSTS_INCLUDE_SUBDOMAINS"
    ceiling = Confidence.FIRM
    corrected_as = "sets it to True"

    consequence = (
        "the HTTPS-only policy stops at the bare hostname and every subdomain is still "
        "reachable over plain HTTP, which is enough for an attacker to serve a page from "
        "one and set a cookie that the parent domain will accept"
    )

    def insecure(self, value: Value) -> bool:
        return could_be_off(value)

    def applies(self, ctx: ProjectContext, group: SettingGroup) -> bool:
        """Only once HSTS is definitely on.

        Off is the correct value while ``SECURE_HSTS_SECONDS`` is 0 -- there is
        no policy to extend -- so firing then would be telling people to change
        a setting that does nothing. An unresolvable duration is treated as off
        for the same reason: we would be guessing, and the guess costs noise on
        every project that reads the duration from its environment.
        """
        view = self.views.get(group.module.dotted)
        if view is None:
            return False
        return not could_be_under(view.get("SECURE_HSTS_SECONDS").value, 1)

    meta = RuleMeta(
        id="DJS-008",
        title="HSTS does not cover subdomains",
        family=Family.DJS,
        severity=Severity.LOW,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "HSTS without includeSubDomains protects the hostname it was served from and "
            "nothing else. An attacker who can reach any subdomain over plain HTTP -- a "
            "forgotten staging box, a wildcard DNS record, a CNAME to a service that "
            "lapsed -- can serve content there and set a cookie scoped to the parent "
            "domain, which the application will then accept. Low severity because "
            "excluding subdomains is sometimes a deliberate and correct choice, and "
            "because it only matters at all once HSTS is switched on."
        ),
        remediation=(
            "Set SECURE_HSTS_INCLUDE_SUBDOMAINS = True once every subdomain is served "
            "over HTTPS. Confirm that first: the directive is as sticky as the max-age it "
            "rides on, so a subdomain that cannot do HTTPS becomes unreachable for the "
            "full window and cannot be rescued early. If some subdomain genuinely cannot "
            "be moved, leaving this off is a defensible choice -- suppress the finding "
            "with a comment saying which one and why."
        ),
        references=(
            _HTTPS_CHECKLIST,
            _HSTS_DOCS,
            "https://docs.djangoproject.com/en/stable/ref/settings/#secure-hsts-include-subdomains",
        ),
    )
