"""DJP-010 -- a client may sort a growing table by a column with no index.

Static analysis cannot know how many rows a table holds, and that is the whole
difficulty with reporting an unindexed sort. Measured against the corpora, the
naive readings are not worth shipping: every `Meta.ordering` on an unindexed
column is 44 findings across netbox and pretix, and every DRF `ordering_fields`
naming an unindexed column is 21 on pretix. Most are lookup tables -- a
`Question`, an `ItemCategory`, a `DeviceRole` -- where sorting the whole table
costs nothing because the whole table is fifty rows.

So the rule asks a narrower question with a defensible static answer: does the
model *accumulate rows over time*? A column that stamps its own creation --
`auto_now_add=True`, or a date defaulted to `now` -- is what an append-only
table looks like, and it is the difference between a log and a configuration
list. Applying that filter takes the 21 down to 7, and the 7 are exactly
pretix's transactional tables: `Checkin`, `Invoice`, `CartPosition`,
`WaitingListEntry`, `Voucher`, `ReusableMedium`, `RevokedTicketSecret`. The
same filter rejects all 9 netbox candidates, which are inventory.

The mechanism is what makes it worth saying at all. `ordering_fields` is a
list of columns a *client* chooses between, so the expensive plan is one query
parameter away and no amount of care in the view prevents it. Postgres given
`ORDER BY unindexed LIMIT 50` sorts the qualifying rows in full before it can
return the first page, and it does that again for every page.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence

from djaudit.api.views import ViewNode
from djaudit.context import ProjectContext
from djaudit.graph.nodes import ModelNode
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Severity,
    Tier,
)
from djaudit.registry import RuleMeta, register
from djaudit.rules._api import ApiRule, Endpoint

ALL_FIELDS = "__all__"

ORDERING_BACKEND = "OrderingFilter"
"""Substring identifying a backend that reads ``ordering_fields``.

A substring rather than an exact name because projects subclass it: pretix
routes every list endpoint through `RichOrderingFilter` and
`TotalOrderingFilter`, neither of which is DRF's class and both of which are
it. The check exists to suppress a view whose `ordering_fields` nothing reads,
not to enumerate the backends in existence.
"""

TIME_FIELDS = frozenset({"DateTimeField", "DateField"})

MULTI_VALUED = frozenset({"ManyToManyField", "GenericRelation"})
"""Relations that are not a column on this table.

`ordering_fields` naming one makes DRF sort through a join, which is a
different cost from a table scan and not one this rule measures. A plain
foreign key is the opposite case: `order_by('owner')` sorts on `owner_id`,
which is a column here and can perfectly well lack an index.
"""


def accumulates(model: ModelNode) -> str | None:
    """The column showing this table gains rows over time, if one does.

    A self-stamping timestamp is the cheapest honest evidence that rows arrive
    and are kept. `auto_now_add` is unambiguous. A date with a default is the
    same statement written by hand, and is how several of pretix's tables spell
    it. `auto_now` is deliberately *not* accepted: it stamps modification, so
    it says a row changes, not that another one was added.
    """
    for field in model.all_fields.values():
        if field.auto_now_add:
            return field.name
    for field in model.all_fields.values():
        if field.kind in TIME_FIELDS and field.has_default and not field.auto_now:
            return field.name
    return None


def sortable(node: ast.expr) -> tuple[tuple[str, ...], bool]:
    """The columns a client may sort by, and whether that is every column.

    Returns the names it could read; a name it could not is simply absent,
    which costs recall and never precision.
    """
    if isinstance(node, ast.Constant):
        if node.value == ALL_FIELDS:
            return (), True
        return ((node.value,) if isinstance(node.value, str) else ()), False
    # A *list* containing "__all__" is not the wildcard: DRF compares
    # `ordering_fields == "__all__"` against the attribute itself, so inside a
    # list it is an ordinary name, and an ordinary name that is not a column.
    if isinstance(node, ast.List | ast.Tuple):
        named = tuple(
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
        return named, False
    return (), False


def unindexed(model: ModelNode, names: Sequence[str]) -> list[str]:
    """Which of those columns carry no index, in the order they were written.

    A name that is not a concrete field of this model is skipped rather than
    reported: `ordering_fields` accepts relation paths and annotation aliases,
    and neither is a column whose index this rule can speak about.
    """
    indexed = model.indexed_fields
    found: list[str] = []
    for name in names:
        # A relation path and `pk` are both absent from `all_fields`, so the
        # lookup below declines them without needing to name them here.
        column = name.lstrip("-")
        field = model.all_fields.get(column)
        if field is None or field.kind in MULTI_VALUED:
            continue
        if "db_index" in field.unreadable or "unique" in field.unreadable:
            continue
        if column not in indexed and column not in found:
            found.append(column)
    return found


@register
class UnindexedClientOrdering(ApiRule):
    meta = RuleMeta(
        id="DJP-010",
        title="Client can sort a growing table by an unindexed column",
        family=Family.DJP,
        tier=Tier.STATIC,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        rationale=(
            "`ordering_fields` lets the caller choose the sort column, so the "
            "plan is decided by a query parameter rather than by the view. "
            "Postgres asked to `ORDER BY` a column with no index sorts every "
            "qualifying row before it can return the first page of results, "
            "and repeats that work for each page. On a table that accumulates "
            "rows the cost grows with the age of the deployment, which is why "
            "these endpoints are fast in staging and slow in year three."
        ),
        remediation=(
            "Add `db_index=True` to the column, or a `Meta.indexes` entry "
            "leading with it where the sort is usually combined with a filter. "
            "If the column is not meant to be sorted on, drop it from "
            "`ordering_fields` -- the list is an allowlist, so removing a name "
            "removes the plan it permits."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/options/#django.db.models.Options.indexes",
            "https://www.django-rest-framework.org/api-guide/filtering/#orderingfilter",
        ),
        limitations=(
            "Only reports models carrying a self-stamping timestamp, which is "
            "the static evidence available that a table accumulates rows. A "
            "growing table without one is missed.",
            "Reads `ordering_fields` only. A default `ordering` on the view or "
            "the model sorts every list request on the same column, but is not "
            "chosen by the caller and is not reported here.",
            "Cannot see a functional or partial index created in a migration by "
            "hand rather than declared on the model, so an index added through "
            "`RunSQL` will look absent.",
        ),
    )

    def inspect(self, ctx: ProjectContext, endpoint: Endpoint) -> Iterator[Finding]:
        view = endpoint.view
        node = view.ordering_fields_node
        if node is None or not self.ordering_backend_installed(view):
            return
        model = self.model_for(ctx, view.queryset_model_ref)
        if model is None:
            return
        stamp = accumulates(model)
        if stamp is None:
            return

        names, everything = sortable(node)
        if everything:
            columns = [
                name
                for name, field in model.all_fields.items()
                if field.kind not in MULTI_VALUED and name not in model.indexed_fields
            ]
            if not columns:
                return
            detail = (
                f"`ordering_fields = '{ALL_FIELDS}'` exposes every column, of which "
                f"{len(columns)} carry no index: {', '.join(sorted(columns)[:6])}"
            )
        else:
            columns = unindexed(model, names)
            if not columns:
                return
            detail = (
                f"`ordering_fields` lets a client sort by "
                f"{', '.join(f'`{c}`' for c in columns)}, which "
                f"{'carries' if len(columns) == 1 else 'carry'} no index"
            )

        yield self.finding(
            location=ctx.location(view.path, node),
            message=(
                f"{view.name} sorts {model.label} on a client-chosen column with no "
                f"index. {detail}."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=(
                        f"{model.label}.{stamp} stamps its own creation, so the table "
                        f"accumulates rows; indexed columns are "
                        f"{', '.join(sorted(model.indexed_fields)) or 'none'}"
                    ),
                    source=f"{view.path}:{node.lineno}",
                ),
            ),
        )

    def ordering_backend_installed(self, view: ViewNode) -> bool:
        """Whether anything will read `ordering_fields`.

        DRF calls `filter_queryset` over `filter_backends`, and only an
        ordering backend looks at the attribute. A view that names it with no
        such backend, and no project-wide default, has written a comment.
        """
        refs = [*view.filter_backend_refs, *self.defaults.filters]
        return any(ORDERING_BACKEND in ref for ref in refs)

    def model_for(self, ctx: ProjectContext, ref: str | None) -> ModelNode | None:
        return ctx.model_graph.get(ref) if ref else None
