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
