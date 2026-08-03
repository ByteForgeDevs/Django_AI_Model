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
from collections.abc import Iterator
from typing import TYPE_CHECKING

from djaudit.context import ProjectContext
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

if TYPE_CHECKING:
    from djaudit.graph.inheritance import ClassIndex

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
