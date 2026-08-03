"""Rules about how much work one request can ask a server to do.

The other `DJA` families ask who may call an endpoint and what comes back.
These ask what it costs. A list endpoint that returns every row is correct by
every test a project is likely to have — it returns the right rows, in the
right shape, to the right people — and it fails in production the first time
the table is large, at which point it takes the database with it.

None of this is a vulnerability in the usual sense and all of it is a way for
one caller to make a service stop answering, which is the same outcome by a
cheaper route. The rules here are deliberately quiet: two of the three report
once against the settings module that decided the behaviour project-wide,
because one line is what has to change and repeating it per endpoint would
turn a single fix into a hundred findings.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from djaudit.astutils import literal
from djaudit.context import ProjectContext
from djaudit.graph.meta import meta_class
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
from djaudit.rules._api import ApiRule, Endpoint
from djaudit.rules.serialization import SECRET_FIELDS

if TYPE_CHECKING:
    from djaudit.api.permissions import Defaults
    from djaudit.api.views import ViewNode
    from djaudit.graph.inheritance import ClassIndex
    from djaudit.graph.nodes import ModelNode

SIZE_ATTRIBUTES = frozenset({"page_size", "default_limit", "max_limit", "page_size_query_param"})
"""Attributes a pagination class sets to supply its own bound.

DRF's ``PageNumberPagination.page_size`` and ``LimitOffsetPagination.default_limit``
both read ``api_settings.PAGE_SIZE`` at class-definition time, so a subclass
that assigns either of them stops depending on the setting entirely.
"""

SIZE_METHODS = frozenset({"get_page_size", "get_limit", "paginate_queryset"})
"""Methods that can decide the bound without any attribute being assigned.

NetBox's ``NetBoxPagination`` overrides ``paginate_queryset`` and sets
``self.default_limit`` from its own runtime configuration in ``__init__``, so
reading only class-level assignments would report a project that paginates
every list it serves.
"""


SETTINGS_HOLDERS = frozenset({"api_settings", "settings", "drf_settings"})


def is_a_real_size(value: ast.expr) -> bool:
    """Whether an assignment to a page-size attribute supplies an actual size.

    DRF's own classes read theirs straight off the settings object --
    ``page_size = api_settings.PAGE_SIZE`` -- so treating any assignment as an
    answer would let DRF's base classes, and every subclass that inherits the
    line, vouch for a project that never set PAGE_SIZE.
    """
    if isinstance(value, ast.Constant):
        return value.value is not None and value.value is not False
    if isinstance(value, ast.Attribute):
        holder = value.value
        if isinstance(holder, ast.Name) and holder.id in SETTINGS_HOLDERS:
            return False
    return True


def supplies_its_own_size(ref: str, index: ClassIndex | None) -> bool:
    """Whether a pagination class decides its page size without ``PAGE_SIZE``.

    Needed because the interesting finding here is a project that *names* a
    pagination class and never sets a page size, which does nothing at all --
    and a project that subclasses one to supply its own size is the shape that
    looks identical from the settings file and is completely fine.
    """
    if index is None:
        return False
    record = index.lookup(ref)
    if record is None:
        # A dotted string naming something outside the project: either DRF's
        # own class, which does depend on PAGE_SIZE, or a package we cannot
        # read. Both are handled by the caller falling back to the setting.
        return False
    for link in [*index.ancestry(record), record]:
        for stmt in ast.walk(link.node):
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                if stmt.name in SIZE_METHODS:
                    return True
            elif isinstance(stmt, ast.Assign):
                if not is_a_real_size(stmt.value):
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id in SIZE_ATTRIBUTES:
                        return True
                    if isinstance(target, ast.Attribute) and target.attr in SIZE_ATTRIBUTES:
                        return True
            elif isinstance(stmt, ast.AnnAssign):
                named = stmt.target
                if stmt.value is None or not is_a_real_size(stmt.value):
                    continue
                if isinstance(named, ast.Name) and named.id in SIZE_ATTRIBUTES:
                    return True
    return False


@register
class UnboundedListEndpoint(ApiRule):
    meta = RuleMeta(
        id="DJA-013",
        title="List endpoints return every row",
        family=Family.DJA,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "DRF ships DEFAULT_PAGINATION_CLASS as None, so a project that does not "
            "set it returns every row of every list endpoint in one response. "
            "Naming a class is not enough on its own: PageNumberPagination.page_size "
            "and LimitOffsetPagination.default_limit are both api_settings.PAGE_SIZE, "
            "which also ships as None, and paginate_queryset returns None when it is "
            "falsy. A project that configures pagination and no page size has "
            "pagination that does nothing, and it reads as configured in review."
        ),
        remediation=(
            "Set both entries together: REST_FRAMEWORK['DEFAULT_PAGINATION_CLASS'] "
            "and REST_FRAMEWORK['PAGE_SIZE']. If a pagination class supplies its own "
            "bound -- a subclass assigning page_size or default_limit, or reading a "
            "runtime setting the way NetBox does -- the PAGE_SIZE entry is not needed "
            "and this rule stays silent."
        ),
        references=(
            "https://www.django-rest-framework.org/api-guide/pagination/#modifying-the-pagination-style",
            "https://cwe.mitre.org/data/definitions/770.html",
        ),
        limitations=(
            "A pagination class from an installed package cannot be read, so one "
            "that supplies its own page size is treated as depending on the "
            "PAGE_SIZE setting and may be reported when it does not need it.",
            "Whether a table is large enough for an unbounded list to matter is not "
            "knowable from source, so a project whose every model holds a handful of "
            "rows is reported the same as one serving millions.",
            "A view that bounds its own results by slicing inside get_queryset is "
            "not recognised as bounded, because the slice is applied after this "
            "rule's reading of the queryset and may depend on the request.",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        routed = [e for e in self.endpoints(ctx) if e.lists]
        # A view that switched pagination off is not relying on the project
        # default, so counting it against the settings finding would blame the
        # settings file for a decision made in the view.
        yield from self.project_default(
            ctx, [e for e in routed if not e.view.pagination_ref and not e.view.pagination_disabled]
        )
        for endpoint in routed:
            yield from self.inspect(ctx, endpoint)

    def project_default(self, ctx: ProjectContext, listing: list[Endpoint]) -> Iterator[Finding]:
        """The settings answer, reported once.

        Follows DJA-001: one line decides this for every list endpoint in the
        project, so one line is what a finding should name. Reporting it per
        endpoint would turn a two-line fix into a finding per route and bury
        the endpoints that really did opt out individually.
        """
        if not self.surface.views:
            # No DRF views anywhere: the project does not use DRF, and telling
            # it that DRF's pagination default is None is a fact about a
            # library it has not installed.
            return
        defaults = self.defaults
        index = self.surface.index
        if defaults.pagination and (
            defaults.page_size or supplies_its_own_size(defaults.pagination, index)
        ):
            return
        module = self.settings_view.module.path if self.settings_view is not None else None
        location = (
            Location(file=ctx.rel(module), line=1, snippet=ctx.snippet(module, 1))
            if module is not None
            else Location(file="(settings not found)", line=1, snippet="")
        )
        if defaults.pagination:
            reason = (
                f"REST_FRAMEWORK names {defaults.pagination} but sets no PAGE_SIZE, "
                f"and DRF's pagination classes read their page size from it, so "
                f"paginate_queryset returns None and nothing is paginated"
            )
        else:
            reason = (
                "REST_FRAMEWORK sets no DEFAULT_PAGINATION_CLASS, and DRF's default "
                "is None, so every list endpoint returns its whole queryset"
            )
        yield self.finding(
            location=location,
            severity=Severity.MEDIUM if listing else Severity.LOW,
            message=(
                f"{reason}. {len(listing)} list endpoint(s) rely on this default."
                if listing
                else f"{reason}. No routed list endpoint relies on this today."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=(
                        f"DEFAULT_PAGINATION_CLASS = {defaults.pagination!r}, "
                        f"PAGE_SIZE = {defaults.page_size!r}"
                    ),
                    source=defaults.declared_by or "DRF defaults",
                ),
                *(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"{endpoint.view.label} lists "
                            f"{endpoint.view.queryset_model_ref or 'its queryset'}"
                        ),
                        source=ctx.rel(endpoint.view.path),
                    )
                    for endpoint in listing[:5]
                ),
            ),
        )

    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        if not endpoint.lists or not endpoint.view.pagination_disabled:
            return
        queryset = endpoint.queryset
        if queryset is not None and queryset.scoped:
            # Every path through this queryset consults the request, so the
            # response is at least bounded to one caller's rows. Still
            # unbounded in principle, but not the finding this rule is for.
            return
        yield self.finding(
            location=self.at(ctx, endpoint.view),
            message=(
                f"{endpoint.view.name} sets pagination_class = None, so its list "
                f"response carries every "
                f"{endpoint.view.queryset_model_ref or 'row'} the queryset matches."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=f"{endpoint.view.label}.pagination_class = None",
                    source=ctx.rel(endpoint.view.path),
                ),
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"queryset = {endpoint.view.queryset_ref}"
                        if endpoint.view.queryset_ref
                        else "queryset comes from get_queryset"
                    ),
                    source=ctx.rel(endpoint.view.path),
                ),
            ),
        )


ALL_FIELDS = "__all__"

FILTER_BACKEND_MARKERS = ("DjangoFilterBackend", "FilterBackend", "filters.")
"""Substrings that identify a backend which reads ``filterset_fields``.

Deliberately loose. django-filter is normally imported as
``django_filters.rest_framework.DjangoFilterBackend``, projects subclass it
constantly, and a view naming ``filterset_fields`` with no backend anywhere is
inert -- so the check exists to suppress that case, not to enumerate every
backend in existence.
"""

ORACLE_LOOKUPS = frozenset(
    {
        "startswith",
        "istartswith",
        "endswith",
        "iendswith",
        "contains",
        "icontains",
        "regex",
        "iregex",
        "gt",
        "gte",
        "lt",
        "lte",
        "range",
    }
)
"""Lookups that answer *part* of a value rather than confirming a whole one.

``?password__startswith=a`` is a character-by-character read of a column the
API never renders: one bit of the secret per request, no authentication beyond
whatever the list endpoint already granted. ``exact`` on the same column only
confirms a value the caller already had.
"""


def filter_backend_is_installed(endpoint: Endpoint, defaults: Defaults) -> bool:
    """Whether anything is actually going to read the view's filter attributes.

    ``filterset_fields`` is inert without a backend: DRF calls
    ``filter_queryset`` over ``filter_backends``, and django-filter's
    ``DjangoFilterBackend`` is the only thing that looks at the attribute.
    """
    refs = [*endpoint.view.filter_backend_refs, *defaults.filters]
    return any(marker in ref for ref in refs for marker in FILTER_BACKEND_MARKERS)


@dataclass
class FilterSurface:
    """What a view's filter configuration expands to, and where it was written."""

    everything: bool = False
    """Every model field is filterable: ``'__all__'``, or a filterset that
    named an ``exclude`` and no ``fields``, which django-filter resolves to the
    same thing."""

    fields: tuple[str, ...] = ()
    """Explicitly named filterable fields."""

    excluded: tuple[str, ...] = ()
    """Names an ``exclude`` list removed from the everything case, so a message
    does not accuse a project of exposing the one column it remembered."""

    lookups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Per-field lookups, when the configuration was a dict."""

    origin: str = ""
    """How it was declared, for the message."""

    line: int | None = None
    path: Path | None = None


def _field_names(node: ast.expr | None) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    value = literal(node)
    if isinstance(value, dict):
        lookups = {
            str(k): tuple(str(x) for x in v)
            for k, v in value.items()
            if isinstance(v, list | tuple)
        }
        return tuple(lookups), lookups
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value), {}
    return (), {}


def read_filter_surface(endpoint: Endpoint, index: ClassIndex | None) -> FilterSurface | None:
    """Resolve what the view actually made filterable.

    ``filterset_class`` wins outright: ``DjangoFilterBackend.get_filterset_class``
    returns it before it ever looks at ``filterset_fields``, so a view that sets
    both has a dead attribute and reporting the dead one names a line that does
    nothing.
    """
    view = endpoint.view
    if view.filterset_ref is not None:
        if index is None:
            return None
        # `filterset_class = filtersets.DeviceFilterSet` is written against the
        # view module's imports, not as a dotted path, so looking it up
        # verbatim finds nothing -- which is indistinguishable from a project
        # that has no over-broad filtersets. NetBox declares 129 of these.
        record = index.lookup(index.resolve_name(view.module, view.filterset_ref))
        declaration = (
            record.node if record is not None else nested_class(view, view.filterset_ref, index)
        )
        if declaration is None:
            return None
        meta = meta_class(declaration)
        if meta is None:
            return None
        fields_node = _assigned_in(meta, "fields")
        exclude_node = _assigned_in(meta, "exclude")
        surface = FilterSurface(
            origin=f"{view.filterset_ref}.Meta",
            line=meta.lineno,
            path=record.path if record is not None else view.path,
        )
        if fields_node is not None and literal(fields_node) == ALL_FIELDS:
            surface.everything = True
        elif fields_node is not None:
            surface.fields, surface.lookups = _field_names(fields_node)
        elif exclude_node is not None:
            # django-filter: "Setting exclude with no fields implies all other
            # fields." An exclude list is `__all__` with a hole in it, and the
            # hole is the fields somebody remembered.
            surface.everything = True
            surface.origin = f"{view.filterset_ref}.Meta.exclude"
            surface.excluded, _ = _field_names(exclude_node)
        else:
            return None
        return surface

    node = view.filterset_fields_node
    if node is None:
        return None
    surface = FilterSurface(origin="filterset_fields", line=node.lineno, path=view.path)
    if literal(node) == ALL_FIELDS:
        surface.everything = True
        return surface
    surface.fields, surface.lookups = _field_names(node)
    return surface if surface.fields else None


def nested_class(view: ViewNode, name: str, index: ClassIndex) -> ast.ClassDef | None:
    """A FilterSet written inside the viewset that uses it.

    Not an edge case: pretix declares 23 of its 31 filtersets this way, and a
    nested class is invisible to a module-level index, so without this the rule
    would be blind to most of one benchmark and read as having found nothing.
    """
    if "." in name:
        return None
    owner = index.lookup(view.label)
    if owner is None:
        return None
    for stmt in owner.node.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == name:
            return stmt
    return None


def _assigned_in(meta: ast.ClassDef, name: str) -> ast.expr | None:
    for stmt in meta.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in stmt.targets
        ):
            return stmt.value
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == name
        ):
            return stmt.value
    return None


@register
class ArbitraryFilterLookups(ApiRule):
    """DJA-014 -- the list endpoint that will answer questions about a column
    it never renders."""

    meta = RuleMeta(
        id="DJA-014",
        title="Filter backend exposes every model field",
        family=Family.DJA,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "django-filter resolves `'__all__'` through `get_all_model_fields`, which "
            "returns every concrete field and every many-to-many on the model, minus "
            "auto primary keys and parent links. Nothing about that list is reviewed: "
            "adding a column to the model adds a query parameter to the public API. "
            "When one of those columns holds credential material the endpoint becomes "
            "an oracle -- the caller cannot read the value but can ask whether it "
            "starts with a given character, one request at a time, and a filter that "
            "answers is a filter that leaks. The same applies to `Meta.exclude` on a "
            "FilterSet, which django-filter documents as meaning all other fields."
        ),
        remediation=(
            "Name the fields the API is meant to filter on. `filterset_fields = "
            "['status', 'created']` is the whole fix, and a dict form "
            "`{'created': ['gte', 'lte']}` keeps the ranges worth having. Never list "
            "a credential column, and prefer `exact` over substring and comparison "
            "lookups on anything a caller should not be able to read."
        ),
        references=(
            "https://django-filter.readthedocs.io/en/stable/guide/rest_framework.html",
            "https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html",
        ),
        limitations=(
            "A filterset built at runtime by `filterset_factory` or returned from an "
            "overridden `get_filterset_class` is not read, so a project that generates "
            "its filtersets dynamically will not be reported here.",
            "The lookups a project considers dangerous depend on the column, and this "
            "rule judges by field name alone, so a non-credential column holding "
            "sensitive data under an ordinary name is not covered.",
        ),
    )

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        seen: set[str] = set()
        for endpoint in self.endpoints(ctx):
            # Filtering is a class attribute, so a viewset routed to six
            # methods has one filter surface and deserves one finding.
            if endpoint.view.label in seen:
                continue
            seen.add(endpoint.view.label)
            yield from self.inspect(ctx, endpoint)

    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        if not filter_backend_is_installed(endpoint, self.defaults):
            # No backend reads these attributes, so whatever they say, nothing
            # happens. Reporting it would be reporting a comment.
            return
        surface = read_filter_surface(endpoint, self.surface.index)
        if surface is None:
            return
        model = ctx.model_graph.get(endpoint.view.queryset_model_ref or "")
        if surface.everything:
            yield self.report_everything(ctx, endpoint, surface, model)
            return
        yield from self.report_named(ctx, endpoint, surface)

    def where(self, ctx: ProjectContext, endpoint: Endpoint, surface: FilterSurface) -> Location:
        path = surface.path or endpoint.view.path
        line = surface.line or endpoint.view.lineno
        return Location(file=ctx.rel(path), line=line, snippet=ctx.snippet(path, line))

    def report_everything(
        self,
        ctx: ProjectContext,
        endpoint: Endpoint,
        surface: FilterSurface,
        model: ModelNode | None,
    ) -> Finding:
        held_back = set(surface.excluded)
        columns = sorted(
            f for f in (model.fields if model is not None else {}) if f not in held_back
        )
        leaked = [f for f in columns if f in SECRET_FIELDS]
        subject = model.label if model is not None else "its queryset model"
        evidence = [
            Evidence(
                kind=EvidenceKind.AST,
                content=f"{endpoint.view.label}: {surface.origin} = '__all__'",
                source=ctx.rel(surface.path or endpoint.view.path),
            )
        ]
        if model is not None and columns:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=f"{model.label} is filterable on: {', '.join(columns)}",
                    source="djaudit model graph",
                )
            )
        if leaked:
            message = (
                f"{endpoint.view.name} makes every field of {subject} filterable, "
                f"including {', '.join(leaked)}, so a caller can ask the API "
                f"questions about a column it never returns."
            )
        else:
            trailer = (
                f" {surface.origin.rsplit('.', 1)[-1]} holds back "
                f"{', '.join(sorted(held_back))}, but not anything added after it."
                if held_back
                else ""
            )
            message = (
                f"{endpoint.view.name} makes every field of {subject} filterable, so any "
                f"column added to the model later becomes a public query parameter.{trailer}"
            )
        return self.finding(
            message=message,
            location=self.where(ctx, endpoint, surface),
            evidence=tuple(evidence),
            severity=Severity.HIGH if leaked else Severity.MEDIUM,
            confidence=Confidence.FIRM if leaked else Confidence.TENTATIVE,
        )

    def report_named(
        self, ctx: ProjectContext, endpoint: Endpoint, surface: FilterSurface
    ) -> Iterator[Finding]:
        for name in surface.fields:
            if name.split("__", 1)[0] not in SECRET_FIELDS:
                continue
            lookups = surface.lookups.get(name, ())
            oracles = sorted(set(lookups) & ORACLE_LOOKUPS)
            # `exact` on a credential column is how a redemption endpoint is
            # built -- find the ticket by its barcode, the gift card by its
            # code -- and the caller has to hold the value already. A
            # substring or range lookup needs no such thing: it reads the
            # column back one answer at a time. Same field, different bug.
            detail = (
                f" with {', '.join(oracles)}, which reads it back a character at a time"
                if oracles
                else ", and any caller holding a value can confirm it"
            )
            yield self.finding(
                message=(
                    f"{endpoint.view.name} lets callers filter on {name}{detail}, so the "
                    f"endpoint answers questions about credential material."
                ),
                location=self.where(ctx, endpoint, surface),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=f"{endpoint.view.label}: {surface.origin} includes {name}",
                        source=ctx.rel(surface.path or endpoint.view.path),
                    ),
                ),
                severity=Severity.HIGH if oracles else Severity.MEDIUM,
                confidence=Confidence.FIRM if oracles else Confidence.TENTATIVE,
            )


CREDENTIAL_WORDS = (
    "login",
    "signin",
    "sign_in",
    "logon",
    "authenticate",
    "authtoken",
    "obtain_token",
    "obtainauthtoken",
    "get_token",
    "token_obtain",
    "password",
    "passwd",
    "forgot",
    "reset",
    "recover",
    "register",
    "signup",
    "sign_up",
    "otp",
    "mfa",
    "2fa",
    "twofactor",
    "two_factor",
    "verify",
    "confirm",
    "token",
    "provision",
    "credential",
    "session",
    "auth",
)
"""Words that name credential handling rather than describe it.

Never consulted on its own, and never used to decide whether to report --
only how loudly. A view reaches this list already known to accept an
anonymous POST, which is what makes ``token`` and ``session`` safe to include:
behind a login they are ordinary domain words, and in front of one they name
the thing being handed out.
"""

TOKEN_BASES = ("ObtainAuthToken", "TokenObtainPair", "TokenViewBase", "TokenRefresh")
"""Base classes that make the question moot. DRF's own ``ObtainAuthToken`` and
simplejwt's token views are login endpoints whatever a project renames them."""

SCOPED_THROTTLE = "ScopedRateThrottle"


def names_a_credential_endpoint(endpoint: Endpoint, patterns: Sequence[str]) -> str | None:
    """The word that says this endpoint handles credentials, or ``None``."""
    view = endpoint.view
    for base in view.bases:
        if any(marker in base for marker in TOKEN_BASES):
            return base
    haystacks = [view.name.lower(), view.module.lower()]
    haystacks.extend(p.lower() for p in patterns)
    for word in CREDENTIAL_WORDS:
        for text in haystacks:
            if word in text:
                return word
    return None


def unthrottled_because(endpoint: Endpoint, defaults: Defaults) -> str | None:
    """Why nothing limits how fast this endpoint can be called, or ``None``.

    Three ways to arrive at no limit, and only the first is visible in review.
    """
    view = endpoint.view
    if not view.throttles_unset and not view.throttle_refs:
        return "sets throttle_classes = [], switching off the project default"
    refs = view.throttle_refs if not view.throttles_unset else defaults.throttles
    if not refs:
        return (
            "declares no throttle classes and REST_FRAMEWORK sets no "
            "DEFAULT_THROTTLE_CLASSES, which DRF ships empty"
        )
    if all(SCOPED_THROTTLE in ref for ref in refs) and view.throttle_scope is None:
        # `ScopedRateThrottle.allow_request` reads `view.throttle_scope` and
        # returns True the moment it is missing. The settings file names a
        # throttle; the endpoint has none.
        return (
            "is throttled only by ScopedRateThrottle and sets no throttle_scope, "
            "so allow_request returns True on every call"
        )
    scopes = {"anon", "user"} | ({view.throttle_scope} if view.throttle_scope else set())
    rated = set(defaults.throttle_rates)
    if rated and not (scopes & rated):
        return (
            f"is throttled on {', '.join(sorted(scopes))} but DEFAULT_THROTTLE_RATES "
            f"sets a rate for {', '.join(sorted(rated))} only"
        )
    return None


@register
class UnthrottledCredentialEndpoint(ApiRule):
    """DJA-015 -- the login form that will answer as fast as it is asked."""

    meta = RuleMeta(
        id="DJA-015",
        title="Credential endpoint accepts unlimited attempts",
        family=Family.DJA,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "An endpoint that checks a credential and is not rate limited is a "
            "password oracle running at network speed. Credential stuffing does not "
            "need a vulnerability -- it needs a list of leaked passwords and an "
            "endpoint that will keep saying no until it says yes -- and the same "
            "applies to password-reset and one-time-code endpoints, where an "
            "unlimited guess rate turns a six-digit code into a few minutes of work. "
            "DRF ships DEFAULT_THROTTLE_CLASSES empty, so an endpoint is unthrottled "
            "unless the project said otherwise, and ScopedRateThrottle allows every "
            "request against a view that never set throttle_scope."
        ),
        remediation=(
            "Set DEFAULT_THROTTLE_CLASSES with a matching DEFAULT_THROTTLE_RATES "
            "entry, and give credential endpoints a stricter limit than the rest of "
            "the API -- `throttle_classes = [AnonRateThrottle]` with a rate measured "
            "in attempts per hour rather than per minute. Rate limiting is not "
            "account lockout; it should be keyed on the caller, not on the account "
            "being named, or it becomes a way to lock other people out."
        ),
        references=(
            "https://www.django-rest-framework.org/api-guide/throttling/",
            "https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/",
        ),
        limitations=(
            "Rate limiting applied by a reverse proxy, a WAF or middleware such as "
            "django-ratelimit is invisible to a reader of DRF's own configuration, so "
            "a project protected that way is reported here and is not wrong.",
            "Credential endpoints are recognised by the words in their name, module "
            "and URL pattern, so a login view named for its product rather than its "
            "job will not be reported by this rule at all.",
        ),
    )

    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        if not endpoint.writes:
            # Checking a credential means sending one.
            return
        if not endpoint.guard.is_open:
            # An endpoint already behind a login is not where credentials are
            # guessed, and requiring certainty keeps a dynamic `get_permissions`
            # from being read as public.
            return
        reason = unthrottled_because(endpoint, self.defaults)
        if reason is None:
            return
        patterns = [e.pattern for e in ctx.api_surface.routes.for_view(endpoint.label)]
        word = names_a_credential_endpoint(endpoint, patterns)
        verbs = ", ".join(sorted(endpoint.methods & {"POST", "PUT", "PATCH", "DELETE"}))
        routes = ", ".join(patterns) or "a urlconf entry"
        tail = (
            "so a caller can guess as fast as the server will answer"
            if word is not None
            else "so one caller decides how much work the server does"
        )
        yield self.finding(
            message=(f"{endpoint.view.name} accepts anonymous {verbs} and {reason}, {tail}."),
            severity=Severity.HIGH if word is not None else Severity.MEDIUM,
            confidence=Confidence.FIRM if word is not None else Confidence.TENTATIVE,
            location=self.at(ctx, endpoint.view),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"{endpoint.label} handles credentials, matched on {word!r}"
                        if word is not None
                        else f"{endpoint.label} is routed at {routes}"
                    ),
                    source=ctx.rel(endpoint.view.path),
                ),
                Evidence(
                    kind=EvidenceKind.CONFIG,
                    content=(
                        f"DEFAULT_THROTTLE_CLASSES = "
                        f"{list(self.defaults.throttles) or '[]'}, "
                        f"DEFAULT_THROTTLE_RATES keys = "
                        f"{list(self.defaults.throttle_rates) or '{}'}"
                    ),
                    source=self.settings_module or "rest_framework defaults",
                ),
                self.guard_evidence(endpoint.guard),
            ),
        )
