"""DJS -- transport and cookie security.

These are the settings that decide whether a session survives contact with a
plain HTTP request. They share a shape -- a flag that must be on and ships off
-- but not a confidence: whether ``SECURE_SSL_REDIRECT`` matters depends on a
proxy we cannot see, while a cookie without ``Secure`` is sent over HTTP by the
browser no matter what sits in front of Django. The ``ceiling`` on each rule is
where that difference is recorded.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import (
    FlagRule,
    SecurityMiddlewareSetting,
    SettingGroup,
    SettingsRule,
    could_be_off,
    could_be_under,
    entries_of,
)
from djaudit.settings import ResolvedSetting
from djaudit.values import Value

_HTTPS_CHECKLIST = "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/#https"


class CookieMiddlewareSetting(FlagRule):
    """A cookie flag that only means anything while Django sets the cookie.

    ``SESSION_COOKIE_SECURE`` is not read by the session framework at large; it
    is read by ``SessionMiddleware`` at the moment it writes the header. A
    project that removes that middleware and installs its own has moved the
    decision into code, and the setting stops being evidence of what the
    browser receives.

    pretix is the case that pins this. It replaces both the session and CSRF
    middleware to support per-organizer domains, and each replacement passes
    ``secure=request.is_secure()`` and upgrades the cookie to the ``__Host-``
    prefix over HTTPS -- stricter than the setting would have been, on a
    project that never assigns it. Read as a settings fact, DJS-009 and DJS-010
    called those cookies insecure and were simply wrong.

    Downgrading every such project would have been the easy answer and a bad
    one: most replacements are subclasses that call ``super()``, still read the
    setting, and deserve the finding at full strength. So the replacement is
    resolved and read instead, and there are three outcomes:

    * it sets the flag itself -- the premise is false, and nothing is reported;
    * it is project code that never mentions the flag -- the finding stands at
      full confidence, and is now better evidenced than before;
    * it cannot be found or read, being third-party -- the finding stands, with
      the uncertainty recorded and the confidence dropped to match.
    """

    django_middleware: str = ""
    """The stock middleware whose absence hands the decision to project code."""

    cookie_kwarg: str = ""
    """The ``set_cookie`` keyword a replacement would pass to set this flag."""

    def replacement(self, group: SettingGroup) -> str | None:
        """The entry standing in for :attr:`django_middleware`, if any.

        Read per branch, because a conditionally built ``MIDDLEWARE`` is not
        one list. pretix builds three and one of them cannot be resolved, so
        asking ``lists_entry`` whether the whole setting contains Django's
        middleware answers "could not tell" -- and the readable branches, which
        both drop Django's and install pretix's, get thrown away with it.

        So unreadable branches are skipped rather than allowed to veto, and the
        evidence has to be unanimous among the ones that are left: no readable
        branch may keep Django's middleware, and at least one must name a
        replacement. A list that is entirely unreadable, or never assigned,
        says nothing and leaves the rule alone.

        Matched on the trailing class name. A project that subclasses
        ``SessionMiddleware`` usually keeps the name, and one that renames it
        is indistinguishable from an unrelated middleware -- in which case this
        stays quiet and the rule keeps its confidence, which is the right way
        round to be wrong.
        """
        view = self.views.get(group.module.dotted)
        if view is None or not view.get("MIDDLEWARE").is_assigned:
            return None
        branches = [b for b in entries_of(view.get("MIDDLEWARE").value) if b is not None]
        if not branches:
            return None
        if any(self.django_middleware in branch for branch in branches):
            return None
        name = self.django_middleware.rsplit(".", 1)[-1]
        for branch in branches:
            for entry in branch:
                if isinstance(entry, str) and entry.rsplit(".", 1)[-1] == name:
                    return entry
        return None

    def sets_the_flag(self, ctx: ProjectContext, dotted: str) -> bool | None:
        """Whether ``dotted``'s class body passes the cookie keyword itself.

        ``None`` when the class cannot be found, which is the ordinary case for
        a middleware installed from a dependency: we are a static tool and do
        not read site-packages.

        Deliberately a keyword search over the whole class body rather than an
        attempt to follow ``set_cookie`` calls. What matters is whether the
        replacement has an opinion about this flag at all, and a class that
        writes ``secure=`` anywhere in it does. Following the call properly
        would mean resolving the response object through arbitrary code for no
        change in the answer.
        """
        module, _, name = dotted.rpartition(".")
        path = ctx.module_path(module)
        tree = None if path is None else ctx.parse(path)
        if tree is None:
            return None
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return any(
                    isinstance(child, ast.keyword) and child.arg == self.cookie_kwarg
                    for child in ast.walk(node)
                )
        return None

    def applies(self, ctx: ProjectContext, group: SettingGroup) -> bool:
        """False once we can see the replacement setting the flag for itself.

        This is the only branch that drops the finding, and it needs the
        strongest evidence of the three: the class was found in this project's
        own source and it passes the keyword. Anything less keeps the finding.
        """
        replacement = self.replacement(group)
        if replacement is None:
            return True
        self._unreadable = self.sets_the_flag(ctx, replacement) is None
        self._replacement = replacement
        return not self.sets_the_flag(ctx, replacement)

    _unreadable = False
    _replacement = ""

    def delegated(self, group: SettingGroup) -> str:
        """Only the unreadable case weakens the finding.

        A replacement we read and found silent about the flag leaves the rule
        at full confidence -- it is better evidenced than the ordinary case,
        not worse.
        """
        if not self._unreadable:
            return ""
        stock = self.django_middleware.rsplit(".", 1)[-1]
        return (
            f"{self._replacement} stands in for Django's {stock} and is not part of "
            f"this project, so whether it sets the cookie's {self.cookie_kwarg} flag "
            f"itself could not be read"
        )


@register
class SslRedirectDisabled(SecurityMiddlewareSetting, FlagRule):
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

    inert_consequence = (
        "no redirect happens at all -- SecurityMiddleware is the only thing in Django "
        "that reads this setting, so the line reads as though plain HTTP were being "
        "turned away while every plain HTTP request is answered normally, which is "
        "worse than leaving it off because it stops anyone looking further"
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
        limitations=(
            "A redirect at nginx, a load balancer or a CDN does the same job and is invisible "
            "from inside the repository. The flag really is off; whether that matters is the "
            "inference, which is why this never claims certainty.",
            "SECURE_REDIRECT_EXEMPT is not read, so a project that redirects everything except "
            "a health check is graded the same as one that redirects everything.",
        ),
    )


@register
class SessionCookieNotSecure(CookieMiddlewareSetting):
    """``SESSION_COOKIE_SECURE`` is off, so the session cookie travels over HTTP."""

    setting = "SESSION_COOKIE_SECURE"
    cookie_kwarg = "secure"
    django_middleware = "django.contrib.sessions.middleware.SessionMiddleware"
    ceiling = Confidence.CERTAIN
    """No proxy changes this. The flag sets an attribute on the cookie and the
    browser is what acts on it -- but the middleware that writes the cookie can,
    which is what :class:`CookieMiddlewareSetting` watches for."""

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
        limitations=(
            "A session backend that does not use a cookie at all makes the setting moot, "
            "and that is not checked.",
            "Only speaks for Django while Django sets the cookie. A project that installs "
            "its own session or CSRF middleware in place of the stock one is reported "
            "tentatively instead of firmly, with the replacement named -- whether that "
            "replacement sets the flag itself is not read, only that the decision has "
            "moved somewhere a settings rule cannot follow.",
        ),
    )


@register
class CsrfCookieNotSecure(CookieMiddlewareSetting):
    """``CSRF_COOKIE_SECURE`` is off, so the CSRF token travels over HTTP."""

    setting = "CSRF_COOKIE_SECURE"
    cookie_kwarg = "secure"
    django_middleware = "django.middleware.csrf.CsrfViewMiddleware"
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
        limitations=(
            "Reports the flag, not the deployment. A site served only over HTTPS with HSTS "
            "already in force is much less exposed than the finding's severity suggests, and "
            "neither of those is visible from the setting.",
            "Only speaks for Django while Django sets the cookie. A project that installs "
            "its own session or CSRF middleware in place of the stock one is reported "
            "tentatively instead of firmly, with the replacement named -- whether that "
            "replacement sets the flag itself is not read, only that the decision has "
            "moved somewhere a settings rule cannot follow.",
        ),
    )


@register
class SessionCookieNotHttpOnly(CookieMiddlewareSetting):
    """``SESSION_COOKIE_HTTPONLY`` is off, so scripts can read the session cookie."""

    setting = "SESSION_COOKIE_HTTPONLY"
    cookie_kwarg = "httponly"
    django_middleware = "django.contrib.sessions.middleware.SessionMiddleware"
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
        limitations=(
            "Django's default is already True, so this can only fire on an explicit assignment. "
            "A project that reads the session cookie from JavaScript through some other "
            "mechanism is not detected.",
            "Only speaks for Django while Django sets the cookie. A project that installs "
            "its own session or CSRF middleware in place of the stock one is reported "
            "tentatively instead of firmly, with the replacement named -- whether that "
            "replacement sets the flag itself is not read, only that the decision has "
            "moved somewhere a settings rule cannot follow.",
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
class HstsNotEnforced(SecurityMiddlewareSetting):
    """``SECURE_HSTS_SECONDS`` is unset, zero, or shorter than a year."""

    setting = "SECURE_HSTS_SECONDS"
    ceiling = Confidence.FIRM
    """A CDN or nginx may be sending the header instead, and that is a very
    common place to put it. We cannot see one from here, so we never claim
    certainty that the header is missing from the response."""

    corrected_as = "sets a longer max-age"

    inert_consequence = (
        "no Strict-Transport-Security header is ever sent -- SecurityMiddleware is what "
        "builds it and it is not installed -- so the browser is never told anything, "
        "while the settings file records a policy that looks finished"
    )

    def insecure(self, value: Value) -> bool:
        return could_be_under(value, ONE_YEAR)

    def describe_state(self, resolved: ResolvedSetting) -> str:
        if self._inert:
            return super().describe_state(resolved)
        literal = resolved.value.literal
        if resolved.is_default or literal == 0:
            return "is never set" if resolved.is_default else "is 0"
        if isinstance(literal, int) and not isinstance(literal, bool):
            return f"is {_duration(literal)}, short of the one year"
        return f"is {resolved.value.describe()}"

    def consequence_for(self, resolved: ResolvedSetting) -> str:
        if self._inert:
            return super().consequence_for(resolved)
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
        limitations=(
            "The Strict-Transport-Security header is very commonly sent by nginx or a CDN "
            "instead, and none of that is visible here. This says Django is not sending it, not "
            "that the response lacks it.",
            "Django only sends the header on requests it considers secure, so a project whose "
            "proxy configuration is wrong can have this setting right and still send nothing.",
        ),
    )


@register
class HstsSubdomainsExcluded(SecurityMiddlewareSetting):
    """``SECURE_HSTS_INCLUDE_SUBDOMAINS`` is off while HSTS is switched on."""

    setting = "SECURE_HSTS_INCLUDE_SUBDOMAINS"
    ceiling = Confidence.FIRM
    corrected_as = "sets it to True"

    consequence = (
        "the HTTPS-only policy stops at the bare hostname and every subdomain is still "
        "reachable over plain HTTP, which is enough for an attacker to serve a page from "
        "one and set a cookie that the parent domain will accept"
    )

    inert_consequence = (
        "includeSubDomains is never sent, because the header it rides on is never sent "
        "either -- SecurityMiddleware builds both and it is not installed"
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
        limitations=(
            "Only fires once SECURE_HSTS_SECONDS is definitely non-zero. A duration read from "
            "the environment is treated as off, so a project that enables HSTS at deploy time "
            "will not be told its subdomains are excluded.",
            "Cannot see whether every subdomain can actually serve HTTPS, which is the thing "
            "that decides whether turning this on is safe.",
        ),
    )


@register
class ProxySslHeaderTrusted(SettingsRule):
    """``SECURE_PROXY_SSL_HEADER`` makes a request header decide "is this HTTPS"."""

    setting = "SECURE_PROXY_SSL_HEADER"
    ceiling = Confidence.TENTATIVE
    """We can read the setting. We cannot see the proxy, and the proxy is the
    entire question, so this never claims more than tentative when the value
    itself is well-formed."""

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        if not resolved.is_assigned or not resolved.value.is_literal:
            # Absent is the safe default, and unresolvable means we learned
            # nothing -- healthchecks builds this from an environment variable.
            return
        literal = resolved.value.literal
        if literal is None:
            return

        header = self._header(literal)
        if header is None:
            yield self.report(
                ctx,
                group,
                message=(
                    f"SECURE_PROXY_SSL_HEADER in {group.module.dotted} is "
                    f"{resolved.value.describe()}, which is not a two-item sequence, so "
                    "Django raises ImproperlyConfigured while working out the scheme of "
                    "every single request"
                ),
                severity=Severity.MEDIUM,
                ceiling=Confidence.FIRM,
            )
            return

        if not header.startswith("HTTP_"):
            yield self.report(
                ctx,
                group,
                message=(
                    f"SECURE_PROXY_SSL_HEADER in {group.module.dotted} names {header!r}, "
                    "which is not a WSGI environment key, so Django looks it up in "
                    "request.META, never finds it, and quietly falls back to the scheme "
                    "of the connection it actually received -- the setting has no effect "
                    "at all and nothing anywhere reports a problem"
                ),
                severity=Severity.MEDIUM,
                ceiling=Confidence.FIRM,
                remediation=(
                    f"WSGI uppercases a request header, replaces its dashes with "
                    f"underscores and prefixes it with HTTP_, so write it that way: "
                    f"{self._as_wsgi(header)!r}. Then confirm the change took effect "
                    "rather than assuming it, because the broken spelling fails silently "
                    "and so does the fix."
                ),
            )
            return

        yield self.report(
            ctx,
            group,
            message=(
                f"SECURE_PROXY_SSL_HEADER in {group.module.dotted} tells Django to treat "
                f"any request carrying {self._as_header(header)} as HTTPS, so that header "
                "now decides whether cookies are marked secure and whether "
                "SECURE_SSL_REDIRECT thinks it has anything to do"
            ),
            severity=Severity.LOW,
        )

    @staticmethod
    def _header(literal: object) -> str | None:
        if isinstance(literal, (tuple, list)) and len(literal) == 2:
            name = literal[0]
            if isinstance(name, str):
                return name
        return None

    @staticmethod
    def _as_wsgi(header: str) -> str:
        return "HTTP_" + header.upper().replace("-", "_")

    @staticmethod
    def _as_header(wsgi: str) -> str:
        return wsgi.removeprefix("HTTP_").replace("_", "-").title()

    meta = RuleMeta(
        id="DJS-012",
        title="SECURE_PROXY_SSL_HEADER trusts a request header",
        family=Family.DJS,
        severity=Severity.LOW,
        confidence=Confidence.TENTATIVE,
        tier=Tier.STATIC,
        rationale=(
            "This setting hands a request header the job of deciding whether a request "
            "arrived over HTTPS. That is correct and necessary behind a TLS-terminating "
            "proxy, and it is sound only while every route to the application passes "
            "through a proxy that overwrites the header. If anything can reach Django "
            "directly -- a container port published by accident, a health-check ingress, "
            "a second load balancer added later -- a client can send the header itself and "
            "Django will believe it, which marks cookies secure over plain HTTP and makes "
            "SECURE_SSL_REDIRECT decide it has nothing to do. Reported at low and "
            "tentative because we can read the setting but cannot see the proxy, and the "
            "proxy is the entire question. It is escalated when the header is spelled in "
            "a way that can never match, which is a different problem: not a risk to "
            "weigh, but a setting that silently does nothing."
        ),
        remediation=(
            "Confirm that the proxy sets this header on every request it forwards and "
            "strips any copy the client supplied, and that Django is not reachable except "
            "through it. Once confirmed, suppress this finding with a comment naming the "
            "proxy -- the point of the rule is that the invariant gets checked once by "
            "somebody, not that the setting is wrong."
        ),
        references=(
            _HTTPS_CHECKLIST,
            "https://docs.djangoproject.com/en/stable/ref/settings/#secure-proxy-ssl-header",
        ),
        limitations=(
            "The proxy is the entire question and cannot be seen from the repository. A "
            "well-formed value is reported at low severity and tentative confidence precisely "
            "because the setting is right whenever exactly one proxy sets the header and strips "
            "it from client requests.",
            "Does not check whether the named header is one the deployment's proxy actually "
            "overwrites, because nothing in the source says which proxy that is.",
        ),
    )
