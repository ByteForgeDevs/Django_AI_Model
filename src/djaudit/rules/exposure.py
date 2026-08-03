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

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Evidence, EvidenceKind, Family, Finding, Severity, Tier
from djaudit.registry import RuleMeta, register
from djaudit.rules._base import (
    SettingGroup,
    SettingsRule,
    assignment_value,
    entries,
    entries_of,
)
from djaudit.settings import ResolvedSetting, SettingsView


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
    """The string literal in the source that named the app."""
    for definition in resolved.definitions:
        for child in ast.walk(definition.node):
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
            severity, note = self.grade(view, key, app)
            yield self.report(
                ctx,
                group.narrow(f'INSTALLED_APPS["{key}"]', resolved.value),
                at=entry_node(resolved, key),
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
    )
