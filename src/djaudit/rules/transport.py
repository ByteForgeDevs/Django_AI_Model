"""DJS -- transport and cookie security.

These are the settings that decide whether a session survives contact with a
plain HTTP request. They share a shape -- a flag that must be on and ships off
-- but not a confidence: whether ``SECURE_SSL_REDIRECT`` matters depends on a
proxy we cannot see, while a cookie without ``Secure`` is sent over HTTP by the
browser no matter what sits in front of Django. The ``ceiling`` on each rule is
where that difference is recorded.
"""

from __future__ import annotations

from djaudit.models import Confidence, Family, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import FlagRule

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
