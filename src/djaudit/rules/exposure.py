"""DJS -- development tooling and diagnostics reaching production.

The rules here are about a different failure from the rest of the family.
Nothing is misconfigured: every one of these lines is correct, was correct when
it was written, and is only wrong because of where it ended up. That is also
why they survive -- there is no wrong value for a reviewer to notice.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from djaudit.context import ProjectContext
from djaudit.discovery import locate_module
from djaudit.manifest import deployed, discover
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
from djaudit.rules._base import (
    SettingGroup,
    SettingsRule,
    entries_of,
    lists_entry,
)
from djaudit.settings import (
    Entry,
    ResolvedSetting,
    SettingsView,
    assignment_value,
    entries,
    literal_text,
    resolve_all,
)
from djaudit.urlconf import routes, urlconfs
from djaudit.values import Value


@dataclass(frozen=True)
class DebugApp:
    """One development package and what having it installed actually costs."""

    name: str
    severity: Severity
    exposure: str
    """What it does to a production deployment, mid-sentence."""


DEBUG_APPS: dict[str, DebugApp] = {
    "silk": DebugApp(
        name="django-silk",
        severity=Severity.HIGH,
        exposure=(
            "it records every request that passes through it -- headers, body and "
            "every SQL query with its parameters -- and serves them back through a "
            "UI that ships with SILKY_AUTHENTICATION and SILKY_AUTHORISATION both "
            "set to False, so whoever finds the URL reads your production traffic"
        ),
    ),
    "debug_toolbar": DebugApp(
        name="django-debug-toolbar",
        severity=Severity.MEDIUM,
        exposure=(
            "its default callback checks settings.DEBUG before rendering, so it is "
            "usually inert in production -- but it is one SHOW_TOOLBAR_CALLBACK "
            "override away from serving a SQL panel to the internet, and it should "
            "not be installed on a machine where that override is one line"
        ),
    ),
    "django_extensions": DebugApp(
        name="django-extensions",
        severity=Severity.LOW,
        exposure=(
            "it adds shell_plus and runserver_plus to the deployment, and "
            "runserver_plus is the Werkzeug debugger, which is a remote shell to "
            "anyone who reaches it -- nothing is exposed by installing it, but it "
            "turns a foothold into a much better one"
        ),
    ),
    "django_browser_reload": DebugApp(
        name="django-browser-reload",
        severity=Severity.LOW,
        exposure=(
            "it mounts an endpoint that holds a connection open waiting for a file "
            "to change, which is a development convenience and a free socket to "
            "hold open in production"
        ),
    ),
}
"""Packages whose whole purpose is to make a running application transparent.

Graded by what each one actually does rather than by how much it feels like a
development tool. Silk is the dangerous one and is the least talked about: it
has no DEBUG gate of any kind, intercepts 100% of requests by default, and
leaves its own UI unauthenticated.
"""


def app_key(entry: object) -> str | None:
    """The distinguishing part of an app label.

    Matched on the first component so that ``debug_toolbar`` and
    ``debug_toolbar.apps.DebugToolbarConfig`` are the same package, which is
    the difference between a rule that works and one that a project's choice of
    spelling switches off.
    """
    if not isinstance(entry, str) or not entry:
        return None
    return entry.split(".", 1)[0]


def always_installed(resolved: ResolvedSetting, key: str) -> bool:
    """Whether ``key`` is present on every branch we could read.

    A project that removes the app on some path has already thought about this,
    and NetBox is why the rule is written this way round: it lists
    ``debug_toolbar`` and then calls ``INSTALLED_APPS.remove('debug_toolbar')``
    unless ``DEBUG``, which is exactly right and must not be reported.
    Unreadable branches are ignored rather than treated as absences, because
    the shapes that produce them extend the list rather than trim it.
    """
    readable = [branch for branch in entries_of(resolved.value) if branch is not None]
    if not readable:
        return False
    return all(any(app_key(entry) == key for entry in branch) for branch in readable)


def entry_node(resolved: ResolvedSetting, key: str) -> ast.expr | None:
    """The string literal naming the app, anywhere in the setting's history."""
    for definition in resolved.definitions:
        found = _named_in(definition.node, key)
        if found is not None:
            return found
    return None


def own_entry(resolved: ResolvedSetting, key: str) -> ast.expr | None:
    """The string literal naming the app in the assignment that decides it."""
    definition = resolved.definition
    return None if definition is None else _named_in(definition.node, key)


def _named_in(node: ast.stmt, key: str) -> ast.expr | None:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and app_key(child.value) == key:
            return child
    return None


@register
class DebugToolingInstalled(SettingsRule):
    """A development package is in the production application registry."""

    setting = "INSTALLED_APPS"
    ceiling = Confidence.FIRM
    """Whether the package is installed is a fact; what it exposes depends on
    the urlconf, which this phase does not read."""

    caveats = (
        "what a package like this exposes also depends on whether its URLs are "
        "mounted, which is in the urlconf rather than the settings",
    )

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        resolved = group.setting
        if not resolved.is_assigned:
            return
        view = self.views[group.module.dotted]
        where = group.module.dotted or ctx.rel(group.module.path)

        for key, app in DEBUG_APPS.items():
            if not always_installed(resolved, key):
                continue
            node = own_entry(resolved, key)
            # A production module that rebuilds the list -- INSTALLED_APPS =
            # [*INSTALLED_APPS, "debug_toolbar"] -- decides the setting again
            # for every app the base module named, and reporting per decision
            # site would report those apps a second time at whatever confidence
            # the rebuild happened to carry. The reader has one line to delete,
            # so the site that spells the app out reports it and the sites that
            # inherit it stay quiet. An app named in no assignment at all --
            # built up in a list the setting merely refers to -- has no such
            # site, so it is reported here, against the setting as a whole.
            if node is None and entry_node(resolved, key) is not None:
                continue
            severity, note = self.grade(view, key, app)
            yield self.report(
                ctx,
                group.narrow(f'INSTALLED_APPS["{key}"]', resolved.value),
                at=node,
                severity=severity,
                message=(
                    f"{app.name} is installed in {where}{group.describe_reach()}"
                    f"{note}, and {app.exposure}"
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.CONFIG,
                        content=f"INSTALLED_APPS contains {key!r} on every branch we could read",
                        source="djaudit settings resolver",
                    ),
                ),
            )

    def grade(self, view: SettingsView, key: str, app: DebugApp) -> tuple[Severity, str]:
        """Adjust for the settings that decide whether the package is live.

        Both adjustments come from reading the packages rather than guessing.
        A project that overrides SHOW_TOOLBAR_CALLBACK has removed the only
        thing keeping the toolbar off in production, and one that turns both of
        silk's access controls on has answered the objection.
        """
        if key == "debug_toolbar":
            config = view.get("DEBUG_TOOLBAR_CONFIG")
            overrides = entries(view, assignment_value(config), config.value)
            if "SHOW_TOOLBAR_CALLBACK" in overrides:
                return Severity.HIGH, " with SHOW_TOOLBAR_CALLBACK overridden"
        if key == "silk" and all(
            view.get(name).is_always(True)
            for name in ("SILKY_AUTHENTICATION", "SILKY_AUTHORISATION")
        ):
            return Severity.LOW, " behind its own authentication and authorisation"
        return app.severity, ""

    meta = RuleMeta(
        id="DJS-024",
        title="development tooling is installed in a production settings module",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "These packages exist to make a running application transparent, which is "
            "exactly what a production deployment must not be. They differ enormously "
            "in how much damage that does, so they are graded separately rather than "
            "lumped together as 'debug tooling'. django-silk is the one that deserves "
            "the alarm and gets the least: it has no DEBUG gate of any kind, it "
            "intercepts every request by default including headers and bodies, it "
            "stores the SQL with its parameters, and SILKY_AUTHENTICATION and "
            "SILKY_AUTHORISATION both default to False, so its UI is open to whoever "
            "finds it. django-debug-toolbar is comparatively safe, because its default "
            "SHOW_TOOLBAR_CALLBACK returns False whenever DEBUG is off -- the risk is "
            "that projects override precisely that callback to get the toolbar working "
            "on a staging box, and staging settings have a way of becoming production "
            "settings. django-extensions exposes nothing by itself but puts "
            "runserver_plus, and therefore the Werkzeug debugger, one command away."
        ),
        remediation=(
            "Install these in the development settings module only, and keep them out "
            "of the production dependency set entirely rather than merely out of "
            "INSTALLED_APPS -- a package that is not deployed cannot be enabled by "
            "accident. If a single settings module has to serve every environment, "
            "append the app inside a conditional and remove it when DEBUG is off, "
            "which is what NetBox does. For silk specifically, if it genuinely must "
            "run in production, set SILKY_AUTHENTICATION and SILKY_AUTHORISATION to "
            "True and lower SILKY_INTERCEPT_PERCENT, because the defaults are not a "
            "starting point, they are wide open."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/settings/#installed-apps",
            "https://django-debug-toolbar.readthedocs.io/en/latest/installation.html",
            "https://github.com/jazzband/django-silk#authentication--authorisation",
        ),
        limitations=(
            "Knows the package is installed, not what it exposes. What a debug app actually "
            "reveals depends on the urlconf, which this phase does not read.",
            "Requires the app to be present on every branch it could read, so a project that "
            "assembles INSTALLED_APPS in a way we cannot follow is left alone.",
        ),
    )


class Presence(StrEnum):
    """How firmly a settings module installs an app."""

    ALWAYS = "always"
    SOMETIMES = "sometimes"
    NEVER = "never"
    UNKNOWN = "unknown"


def presence(ctx: ProjectContext, views: dict[str, SettingsView], key: str) -> Presence:
    """Whether production settings install ``key``, on some paths, or at all.

    The three answers need three different sentences, and the middle one is the
    interesting one: an app added and then removed depending on a flag is not
    running today, but nothing about the *deployment* stops it -- only a value
    does.
    """
    verdicts: set[Presence] = set()
    for module in ctx.settings_modules:
        if not module.role.reaches_production:
            continue
        view = views.get(module.dotted)
        if view is None:
            continue
        resolved = view.get("INSTALLED_APPS")
        if not resolved.is_assigned or not any(
            branch is not None for branch in entries_of(resolved.value)
        ):
            verdicts.add(Presence.UNKNOWN)
        elif always_installed(resolved, key):
            verdicts.add(Presence.ALWAYS)
        elif entry_node(resolved, key) is not None:
            verdicts.add(Presence.SOMETIMES)
        else:
            verdicts.add(Presence.NEVER)
    for verdict in (Presence.ALWAYS, Presence.SOMETIMES, Presence.UNKNOWN):
        if verdict in verdicts:
            return verdict
    return Presence.NEVER if verdicts else Presence.UNKNOWN


@register
class DebugToolingDeployed(Rule):
    """A development package is in the production dependency set.

    The companion to DJS-024 and deliberately its complement: this reports only
    what that rule does not, so a package that is both deployed and installed
    is one finding rather than two.
    """

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        manifests = discover(ctx.root)
        if not manifests:
            return
        views = resolve_all(ctx)

        for key, app in DEBUG_APPS.items():
            found = deployed(manifests, app.name)
            if found is None:
                continue
            manifest, requirement = found
            state = presence(ctx, views, key)
            if state is Presence.ALWAYS:
                # DJS-024 has already reported this, with the severity the
                # running app deserves. Saying it again in a quieter voice
                # would only teach the reader to skim.
                continue
            severity, confidence, note = _GRADES[state]
            yield self.finding(
                location=Location(
                    file=ctx.rel(manifest.path),
                    line=requirement.line,
                    snippet=requirement.raw,
                ),
                severity=severity,
                confidence=confidence,
                message=(
                    f"{app.name} is in the production dependency set ({manifest.label}), {note}"
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.CONFIG,
                        content=f"{ctx.rel(manifest.path)}:{requirement.line}: {requirement.raw}",
                        source="djaudit manifest reader",
                    ),
                ),
                properties={"distribution": app.name, "installed": state.value},
            )

    meta = RuleMeta(
        id="DJS-025",
        title="development tooling is in the production dependency set",
        family=Family.DJS,
        severity=Severity.LOW,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Keeping a debug package out of INSTALLED_APPS is the right fix and it is "
            "not the whole fix, because what remains is a deployment where the only "
            "thing standing between a profiler and production traffic is a value. The "
            "shape this is really about is the conditional install: an app appended "
            "under 'if DEBUG' and removed otherwise is correct today and one "
            "environment variable away from being incorrect, and the flip is usually "
            "done deliberately, by an operator debugging an incident, at the worst "
            "possible moment. A package that is merely present is a smaller matter -- "
            "image weight, and a better foothold for anyone who gets one -- which is "
            "why it is reported at info rather than low. Both are worth knowing and "
            "neither is worth an alarm, so this rule is quiet by design."
        ),
        remediation=(
            "Move the package to the development requirements file or dependency "
            "group, and install production from a manifest that does not include it. "
            "A package that is not on the machine cannot be switched on by a settings "
            "change, which is the difference between a guard and a gap. Where a "
            "single image genuinely has to serve both -- a self-hosted product whose "
            "operators need to enable debugging -- keep the conditional install and "
            "make sure the flag controlling it cannot be set from the environment "
            "alone."
        ),
        references=(
            "https://packaging.python.org/en/latest/discussions/install-requires-vs-requirements/",
            "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/",
        ),
        limitations=(
            "Reads dependency manifests, so a package installed by a Dockerfile, a system "
            "package or an editable checkout is invisible.",
            "Classifying a manifest as production or development is a guess from its filename "
            "and its declared groups, and a project with an unusual layout can be read either "
            "way.",
        ),
    )


_GRADES: dict[Presence, tuple[Severity, Confidence, str]] = {
    Presence.SOMETIMES: (
        Severity.LOW,
        Confidence.FIRM,
        "and INSTALLED_APPS adds it on some paths and not others -- so it is not "
        "running today, but what stops it is a settings value rather than the "
        "absence of the package, and that value can change without a deploy",
    ),
    Presence.NEVER: (
        Severity.INFO,
        Confidence.FIRM,
        "and nothing in the settings installs it, so it is inert -- it is weight in "
        "the image and a better toolkit for anyone who gets a foothold",
    ),
    Presence.UNKNOWN: (
        Severity.INFO,
        Confidence.TENTATIVE,
        "and INSTALLED_APPS could not be read, so whether anything installs it is "
        "unknown from the source alone",
    ),
}


ADMIN_GUARDS = ("django_otp", "two_factor", "allauth_2fa", "axes", "defender", "admin_honeypot")
"""Apps whose presence means the project has already answered this rule.

Not one of them moves the admin, and that is the point: each of them addresses
what the default path actually costs -- unlimited automated login attempts
against a URL every scanner knows -- which is a better answer than moving it.
"""


def mounts_admin(view: str) -> bool:
    """Whether a route's view argument is a Django admin site.

    Deliberately broader than ``admin.site.urls``. Projects import the site
    directly, subclass ``AdminSite`` for a second one, and name the result
    whatever they like, so what is matched is the shape rather than one
    spelling.
    """
    text = view.replace(" ", "")
    if text.endswith(("site.urls", "site.get_urls()")):
        return True
    return "admin" in text.lower() and text.endswith(".urls")


def at_default_path(pattern: str) -> bool:
    """Whether a route pattern puts its view at ``/admin/``.

    Handles the regex spelling too, since ``re_path(r'^admin/')`` is the same
    URL and a rule that only understood ``path()`` would be silent on every
    project that has not migrated.
    """
    return pattern.lstrip("^/").rstrip("$") in {"admin/", "admin"}


@register
class AdminAtDefaultPath(Rule):
    """The admin is mounted where every scanner already looks."""

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        views = resolve_all(ctx)
        production = [
            views[module.dotted]
            for module in ctx.settings_modules
            if module.role.reaches_production and module.dotted in views
        ]
        if not production:
            return
        if any(
            any(lists_entry(view, "INSTALLED_APPS", guard) for guard in ADMIN_GUARDS)
            for view in production
        ):
            return

        names = (literal_text(view.get("ROOT_URLCONF").value) or "" for view in production)
        for path in urlconfs(ctx, names):
            for route in routes(ctx, path):
                if not mounts_admin(route.view) or not at_default_path(route.pattern):
                    continue
                yield self.finding(
                    location=Location(
                        file=ctx.rel(path),
                        line=route.line,
                        snippet=ctx.snippet(path, route.line),
                    ),
                    confidence=Confidence.FIRM if route.literal else Confidence.TENTATIVE,
                    message=(
                        f"{route.view} is mounted at /{route.pattern}"
                        + (
                            ", so the admin is at the path every scanner tries first"
                            if route.literal
                            else " under a prefix we could not read, so this is the default "
                            "path unless that prefix is non-empty"
                        )
                    ),
                    evidence=(
                        Evidence(
                            kind=EvidenceKind.AST,
                            content=f"{route.router}({route.pattern!r}, {route.view})",
                            source=f"{ctx.rel(path)}:{route.line}",
                        ),
                    ),
                    properties={"router": route.router, "view": route.view},
                )

    meta = RuleMeta(
        id="DJS-026",
        title="the admin is mounted at the default path",
        family=Family.DJS,
        severity=Severity.INFO,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "This is obscurity, not security, and it is reported at info because that "
            "is what it is worth -- an attacker who wants the admin will find it, and a "
            "project that moves it and changes nothing else has gained nothing. What "
            "the default path actually costs is signal. Every scanner on the internet "
            "tries /admin/ continuously, so the logs of a Django site at the default "
            "path contain a permanent background of credential stuffing, and a real "
            "attempt against a real account is indistinguishable from it. Move the "
            "path and every request that arrives is worth reading. The rule stays "
            "silent when the project has already answered the underlying problem some "
            "other way -- django-axes, django-otp, two-factor auth, a honeypot -- "
            "because each of those is a better answer than moving the URL, and telling "
            "someone who has done the harder thing to also do the easier one is how a "
            "tool gets ignored."
        ),
        remediation=(
            "Mount the admin somewhere unguessable and, more importantly, put "
            "something in front of it: rate limiting and lockout via django-axes, a "
            "second factor via django-otp, or network-level restriction to a VPN or an "
            "allowlist. If the admin is not used at all, remove django.contrib.admin "
            "from INSTALLED_APPS and drop the route, which is what NetBox does. Moving "
            "the path is worth doing for the log quality alone, but it should be the "
            "last of these changes, not the only one."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/contrib/admin/",
            "https://docs.djangoproject.com/en/stable/howto/deployment/checklist/",
        ),
        limitations=(
            "Reads the routes in the urlconf it can find and does not follow include(), so an "
            "admin mounted through an included module is not seen.",
            "A path built at runtime -- from an environment variable or a site prefix -- can "
            "only be reported tentatively, because the literal part is all we have.",
            "Silence depends on recognising a lockout or second-factor package by name, so a "
            "project that rate-limits the admin some other way is still reported.",
        ),
    )


DJANGO_FILTER = "django.views.debug.SafeExceptionReporterFilter"
"""The filter that does all of Django's redaction of error reports.

Its ``hidden_settings`` pattern -- ``API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|
HTTP_COOKIE`` -- is what keeps the Authorization header, the session cookie and
anything password-shaped out of a traceback. Replace it and every one of those
goes back in.
"""

REPORTER_SETTINGS = frozenset({"LOGGING", "DEFAULT_EXCEPTION_REPORTER_FILTER"})


def sub_entries(view: SettingsView, entry: Entry) -> dict[str, Entry]:
    """The keys of a nested dict, resolved the same way as the outer one."""
    return entries(view, entry.node, entry.value)


def class_bases(ctx: ProjectContext, dotted: str) -> set[str] | None:
    """The base classes of a class named by dotted path, if we can find it.

    ``None`` when the class is outside the tree we parsed, which is a different
    answer from "it has no bases" and has to stay different: a project may
    perfectly well subclass Django's filter in a package we are not looking at.
    """
    module, _, name = dotted.rpartition(".")
    path = locate_module(ctx, module)
    tree = None if path is None else ctx.parse(path)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {ast.unparse(base) for base in node.bases}
    return None


def replaces_redaction(ctx: ProjectContext, dotted: str) -> bool | None:
    """Whether a filter class abandons Django's redaction rather than extending it.

    The distinction is the whole rule. Almost every project that sets this
    setting does so to redact *more*, by subclassing, and reporting them would
    punish exactly the people who thought about it hardest.
    """
    if dotted == DJANGO_FILTER:
        return False
    bases = class_bases(ctx, dotted)
    if bases is None:
        return None
    return not any(base.endswith("SafeExceptionReporterFilter") for base in bases)


@register
class ErrorReportsLeakRequests(SettingsRule):
    """The error-report path is configured to send more than it redacts."""

    ceiling = Confidence.FIRM
    """Whether the setting says this is a fact; whether an error ever occurs on
    a request carrying something sensitive is a near-certainty, not a fact."""

    def selects(self, name: str) -> bool:
        return name in REPORTER_SETTINGS

    def inspect(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        if group.setting.name == "LOGGING":
            yield from self.inspect_handlers(ctx, group)
        else:
            yield from self.inspect_filter(ctx, group)

    def inspect_handlers(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        view = self.views[group.module.dotted]
        top = entries(view, assignment_value(group.setting), group.setting.value)
        handlers = top.get("handlers")
        if handlers is None:
            return
        for name, handler in sub_entries(view, handlers).items():
            config = sub_entries(view, handler)
            klass = literal_text(config.get("class", Entry("class", Value.unknown("absent"))).value)
            if klass is None or not klass.endswith("AdminEmailHandler"):
                continue
            yield from self.inspect_handler(ctx, group, name, config)

    def inspect_handler(
        self, ctx: ProjectContext, group: SettingGroup, name: str, config: dict[str, Entry]
    ) -> Iterator[Finding]:
        html = config.get("include_html")
        if html is not None and html.value.is_always(True):
            yield self.report(
                ctx,
                group.narrow(f'LOGGING["handlers"]["{name}"]["include_html"]', html.value),
                at=html.node,
                severity=Severity.MEDIUM,
                message=(
                    f"the {name!r} handler mails admins with include_html on, so every "
                    "unhandled exception sends the full HTML debug page -- every local "
                    "variable in every frame, the request, and a slice of the settings "
                    "-- out through SMTP, which is the one page DEBUG exists to keep off "
                    "the internet"
                ),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.CONFIG,
                        content=f'LOGGING["handlers"]["{name}"]["include_html"] = True',
                        source="djaudit settings resolver",
                    ),
                ),
            )

        reporter = config.get("reporter_class")
        dotted = literal_text(reporter.value) if reporter is not None else None
        if reporter is None or dotted is None:
            return
        replaced = replaces_redaction(ctx, dotted)
        if replaced is False:
            return
        yield self.report(
            ctx,
            group.narrow(f'LOGGING["handlers"]["{name}"]["reporter_class"]', reporter.value),
            at=reporter.node,
            severity=Severity.MEDIUM,
            ceiling=Confidence.FIRM if replaced else Confidence.TENTATIVE,
            extra_caveats=()
            if replaced
            else (
                "the class is outside the tree we parsed, so whether it extends "
                "Django's reporter could not be checked",
            ),
            message=(
                f"the {name!r} handler builds its error emails with {dotted}, which does "
                "not extend Django's reporter, so the redaction that keeps the "
                "Authorization header, the session cookie and anything password-shaped "
                "out of a traceback is whatever that class reimplements"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=f'LOGGING["handlers"]["{name}"]["reporter_class"] = {dotted!r}',
                    source="djaudit settings resolver",
                ),
            ),
        )

    def inspect_filter(self, ctx: ProjectContext, group: SettingGroup) -> Iterator[Finding]:
        dotted = literal_text(group.setting.value)
        if dotted is None:
            return
        replaced = replaces_redaction(ctx, dotted)
        if replaced is False:
            return
        yield self.report(
            ctx,
            group,
            severity=Severity.MEDIUM,
            ceiling=Confidence.FIRM if replaced else Confidence.TENTATIVE,
            extra_caveats=()
            if replaced
            else (
                "the class is outside the tree we parsed, so whether it extends "
                "Django's reporter could not be checked",
            ),
            message=(
                f"DEFAULT_EXCEPTION_REPORTER_FILTER is {dotted}, which does not extend "
                "SafeExceptionReporterFilter, so Django's redaction is replaced rather "
                "than extended -- and that redaction is the only thing keeping the "
                "Authorization header, the session cookie and every password-shaped POST "
                "parameter out of error emails and 500 pages"
            ),
        )

    meta = RuleMeta(
        id="DJS-027",
        title="the error-report path sends more than it redacts",
        family=Family.DJS,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Django's error reports are already close to the line: an unhandled "
            "exception mails ADMINS a traceback containing the request, the POST "
            "parameters and every local variable, in plaintext over SMTP. What keeps "
            "that survivable is SafeExceptionReporterFilter, whose pattern -- "
            "API, AUTH, TOKEN, KEY, SECRET, PASS, SIGNATURE, HTTP_COOKIE -- redacts the "
            "Authorization header, the session cookie and anything password-shaped "
            "before the mail goes out. This rule is about the two opt-in ways a project "
            "removes that protection. include_html on an AdminEmailHandler attaches the "
            "full HTML debug page, which is the page DEBUG exists to keep off the "
            "internet, and mails it. Replacing the reporter filter with a class that "
            "does not extend Django's substitutes whatever that class happens to "
            "implement for the pattern above. Neither is a default and neither happens "
            "by accident, which is why both are reported and why the far more common "
            "arrangement -- the stock mail_admins handler, which Django itself ships in "
            "DEFAULT_LOGGING -- is not."
        ),
        remediation=(
            "Leave include_html off; the plaintext traceback already says what broke, "
            "and the HTML version adds every local variable in every frame for the "
            "benefit of an inbox. If a custom reporter filter is needed, subclass "
            "SafeExceptionReporterFilter and extend cleanse_setting or "
            "get_post_parameters rather than replacing the class, so the existing "
            "redaction keeps working. Better still, send exceptions to an error "
            "tracker over TLS instead of to a mailbox."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/logging/#django.utils.log.AdminEmailHandler",
            "https://docs.djangoproject.com/en/stable/howto/error-reporting/#filtering-error-reports",
        ),
        limitations=(
            "Says what the configuration does, not what any particular error report contained. "
            "Whether an exception ever occurs on a request carrying something sensitive is a "
            "near-certainty rather than a fact.",
            "A replacement reporter filter is resolved inside the repository. One that lives in "
            "an installed package cannot be read, so it is reported tentatively with that said "
            "in the caveat.",
        ),
    )
