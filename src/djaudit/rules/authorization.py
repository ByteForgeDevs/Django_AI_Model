"""DJA -- who is allowed to reach an endpoint, and which rows they get.

The family exists because DRF's defaults are permissive and its configuration
is spread across four places. `DEFAULT_PERMISSION_CLASSES` ships as
`AllowAny`; a view that declares nothing inherits whatever the project set, or
that default if it set nothing; a base class may declare on the view's behalf;
and a router decides which methods any of it applies to. No single file in a
project contains the answer, which is why these rules read a resolved surface
rather than a pattern.

Each rule here reports a *precondition* and says so. "Anonymous callers can
reach this" is a fact; "this is a vulnerability" depends on whether the
endpoint was meant to be public, and no static read can settle that. So the
messages name what was read and where, and the confidence ceilings say what
the reading cannot cover.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from djaudit.api.permissions import (
    DEFAULT_PERMISSION_SETTING,
    DRF_SETTING,
    Source,
)
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
from djaudit.registry import RuleMeta, register
from djaudit.rules._api import ApiRule, Endpoint, production_settings
from djaudit.settings import assignment_value, entries

if TYPE_CHECKING:
    from djaudit.api.querysets import QuerysetNode
    from djaudit.context import ProjectContext
    from djaudit.graph.nodes import ModelNode

DRF_DOCS = "https://www.django-rest-framework.org/api-guide/permissions/"
OWASP_ACCESS = "https://owasp.org/Top10/A01_2021-Broken_Access_Control/"

OWNERSHIP_FIELDS = frozenset({"user", "owner", "author", "created_by", "account", "customer"})
"""Field names that make a model a per-user record.

Deliberately a small list of unambiguous names. A model with an ``owner``
foreign key to the user model holds rows belonging to particular people, and an
endpoint returning all of them is returning other people's. A model without one
may still be sensitive, but nothing in its definition says so, and guessing is
how this rule would become noise.
"""


@register
class PermissiveDefaultPermissions(ApiRule):
    """The project-wide permission default admits anonymous callers."""

    ceiling = Confidence.CERTAIN
    """The setting is read directly. What it *means* depends on whether views
    override it, which the message says rather than the grade."""

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        """Reported against the settings module, not per endpoint.

        One line decides this for the whole project, so the reader has one
        place to change and should get one finding -- reporting it per view
        would produce 151 copies of the same sentence on NetBox.
        """
        surface = ctx.api_surface
        if not surface.views:
            return
        view = production_settings(ctx)
        if view is None:
            return
        setting = view.get(DRF_SETTING)
        block = entries(view, assignment_value(setting), setting.value)
        entry = block.get(DEFAULT_PERMISSION_SETTING)
        node = entry.node if entry is not None else None

        classes = tuple(entry.value.literal) if entry is not None else ()
        permissive = entry is None or not classes or _all_open(classes)
        if not permissive:
            return

        # The setting may be assigned in a base module the entrypoint imports,
        # so the location follows the definition rather than the module we
        # resolved it through -- otherwise the fix points at the wrong file.
        definition = setting.definition
        path = definition.module if definition is not None else view.module.path
        line = node.lineno if node is not None else (definition.line if definition else 1)
        where = (definition.dotted if definition else view.module.dotted) or ctx.rel(path)
        detail = (
            f"{DEFAULT_PERMISSION_SETTING} is not set in {where}, so DRF's own default applies"
            if entry is None
            else f"{DEFAULT_PERMISSION_SETTING} is {list(classes)!r} in {where}"
        )
        relying = sorted(
            v.label
            for v in surface.views.values()
            if v.permissions_unset and v.label in surface.routes.routed()
        )
        yield self.finding(
            location=Location(file=ctx.rel(path), line=line, snippet=ctx.snippet(path, line)),
            message=(
                f"{detail}, so every endpoint that declares no permission_classes "
                f"is reachable by anonymous callers ({len(relying)} of "
                f"{len(surface.routes.routed())} routed views)"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=(
                        "rest_framework/settings.py ships "
                        "DEFAULT_PERMISSION_CLASSES = ['rest_framework.permissions.AllowAny']"
                    ),
                    source="djangorestframework",
                ),
                Evidence(
                    kind=EvidenceKind.AST,
                    content="views relying on the default: " + (", ".join(relying[:10]) or "none"),
                    source="djaudit API surface",
                ),
            ),
            severity=Severity.HIGH if relying else Severity.LOW,
        )

    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        return iter(())

    meta = RuleMeta(
        id="DJA-001",
        title="DRF default permission classes allow any caller",
        family=Family.DJA,
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "DRF ships DEFAULT_PERMISSION_CLASSES set to AllowAny, so a project that "
            "does not set it has no authorization on any view that does not set its "
            "own. The failure is silent: nothing errors, and every endpoint answers."
        ),
        remediation=(
            "Set REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES'] to "
            "['rest_framework.permissions.IsAuthenticated'] and grant access explicitly "
            "on the views that should be public."
        ),
        references=(DRF_DOCS, OWASP_ACCESS),
        limitations=(
            "A view that declares its own permission_classes is unaffected by this "
            "setting, and the count in the message says how many rely on it.",
        ),
    )


@register
class EndpointWithoutPermissions(ApiRule):
    """A routed endpoint left to a permissive project default."""

    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        guard = endpoint.guard
        if guard.source not in (Source.SETTING, Source.DRF_DEFAULT):
            return
        if not guard.is_open:
            return
        view = endpoint.view
        origin = (
            f"the project default in {self.settings_module}"
            if guard.source is Source.SETTING
            else "DRF's own default, since the project sets none"
        )
        yield self.finding(
            location=self.at(ctx, view),
            message=(
                f"{view.name} declares no permission_classes anywhere in its ancestry "
                f"and falls back to {origin}, which admits anonymous callers on "
                f"{_methods(endpoint)}"
            ),
            evidence=(self.guard_evidence(guard),),
            severity=Severity.HIGH if endpoint.writes else Severity.MEDIUM,
        )

    meta = RuleMeta(
        id="DJA-002",
        title="Routed view relies on a permissive permission default",
        family=Family.DJA,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "The view never says who may call it, and the default it inherits says "
            "anyone. This is the common shape of an accidentally public endpoint: "
            "nobody wrote a permissive rule, they wrote nothing."
        ),
        remediation=(
            "Declare permission_classes on the view, or raise the project default and "
            "mark deliberately public endpoints with AllowAny so the intent is written down."
        ),
        references=(DRF_DOCS, OWASP_ACCESS),
        limitations=(
            "An endpoint may be public by design; this rule reports that nothing in "
            "the application restricts it, not that something should.",
            "Authentication enforced by middleware or by a gateway in front of the "
            "application is not visible to a static read of the view.",
        ),
    )


@register
class OpenWritableEndpoint(ApiRule):
    """A view that permits anonymous writes."""

    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        guard = endpoint.guard
        if not endpoint.writes or not guard.is_open:
            return
        # DJA-002 already reports the endpoints that inherited their opening.
        # Reporting them here too would put the same view on two lines of the
        # same report with the same fix.
        if guard.source in (Source.SETTING, Source.DRF_DEFAULT):
            return
        writes = sorted(endpoint.methods & {"POST", "PUT", "PATCH", "DELETE"})
        declared = "an empty permission_classes" if not guard.classes else "AllowAny"
        yield self.finding(
            location=self.at(ctx, endpoint.view),
            message=(
                f"{endpoint.view.name} declares {declared} and is routed for "
                f"{', '.join(writes)}, so anonymous callers can change data"
            ),
            evidence=(self.guard_evidence(guard),),
            severity=Severity.CRITICAL,
        )

    meta = RuleMeta(
        id="DJA-003",
        title="Write methods reachable without authentication",
        family=Family.DJA,
        severity=Severity.CRITICAL,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "An unauthenticated read is an exposure; an unauthenticated write is an "
            "invitation. The view names a permissive permission explicitly, so this "
            "is a decision someone made rather than a default nobody changed."
        ),
        remediation=(
            "Restrict the write methods -- IsAuthenticated at minimum, or "
            "IsAuthenticatedOrReadOnly if the reads are genuinely public."
        ),
        references=(DRF_DOCS, OWASP_ACCESS),
        limitations=(
            "A credential check performed inside a view method rather than by a "
            "permission class is not visible to this rule and will look like nothing.",
        ),
    )


@register
class UnscopedUserOwnedQueryset(ApiRule):
    """A list endpoint returning every row of a per-user model."""

    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        queryset = endpoint.queryset
        if queryset is None or not queryset.unfiltered or not endpoint.lists:
            return
        if queryset.model is None:
            return
        model = ctx.model_graph.get(queryset.model)
        if model is None:
            return
        owners = _ownership_fields(model)
        if not owners:
            return
        # An authenticated-only endpoint still shows every user their
        # neighbours' rows, so authentication is not the answer here -- but a
        # guard we could not read might be, and claiming otherwise would be a
        # guess presented as a finding.
        if not endpoint.guard.certain:
            return
        yield self.finding(
            location=self.at(ctx, endpoint.view, queryset.lineno),
            message=(
                f"{endpoint.view.name} lists every {model.name} row without consulting "
                f"the request, and {model.name} is per-user through "
                f"{', '.join(sorted(owners))} -- so every caller sees everyone's records"
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"{queryset.declared_by or endpoint.label}: "
                        f"{queryset.expression or _returns_text(queryset)}"
                    ),
                    source="djaudit API surface",
                ),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"{model.label} relates to the user model through "
                        f"{', '.join(sorted(owners))}"
                    ),
                    source="djaudit model graph",
                ),
                self.guard_evidence(endpoint.guard),
            ),
            severity=Severity.CRITICAL if endpoint.guard.is_open else Severity.HIGH,
        )

    meta = RuleMeta(
        id="DJA-004",
        title="List endpoint not scoped to the requesting user",
        family=Family.DJA,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "The model holds rows belonging to particular users and the endpoint "
            "returns all of them. Authentication does not help: every logged-in "
            "caller sees every other caller's records. This is broken object-level "
            "authorization at the collection rather than the object, which is worse "
            "than an IDOR because no id has to be guessed."
        ),
        remediation=(
            "Override get_queryset to filter by the requesting user -- "
            "return Model.objects.filter(owner=self.request.user) -- rather than "
            "relying on a permission class, which cannot narrow rows."
        ),
        references=(
            OWASP_ACCESS,
            "https://www.django-rest-framework.org/api-guide/filtering/",
        ),
        limitations=(
            "A filter backend listed in filter_backends is not counted as scoping, "
            "because a backend filtering on a query parameter narrows only what the "
            "caller chose to narrow.",
            "Row-level security enforced by the database is invisible to a static read "
            "of the application source, and would make this finding a false positive.",
            "A generic ListAPIView routed by a plain path() entry is not reported, "
            "because only a router states outright that a URL returns a collection.",
        ),
    )


def _all_open(classes: object) -> bool:
    if not isinstance(classes, list | tuple):
        return False
    return all(
        isinstance(item, str) and item.endswith(("permissions.AllowAny", ".AllowAny"))
        for item in classes
    )


def _methods(endpoint: Endpoint) -> str:
    return ", ".join(sorted(endpoint.methods)) or "every routed method"


def _returns_text(queryset: QuerysetNode) -> str:
    return "; ".join(r.expression for r in queryset.returns) or "queryset"


def _ownership_fields(model: ModelNode) -> set[str]:
    """Fields tying a model's rows to individual users.

    Both halves have to hold: the relation has to reach the user model *and*
    be named like ownership. A ``ForeignKey`` to the user model called
    ``approved_by`` records who signed something off; it does not make the row
    theirs, and filtering by it would be wrong as well as noisy.
    """
    return {
        edge.field_name
        for edge in model.relations
        if edge.points_at_user and edge.field_name in OWNERSHIP_FIELDS
    }
