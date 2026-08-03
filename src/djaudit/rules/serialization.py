"""Rules about what a serializer publishes, and what it will accept.

Step 2.3 asked who may reach an endpoint. These ask what comes back when they
do, and what they may send. The questions are independent: a correctly
permissioned endpoint still leaks if its serializer ships a password hash, and
a serializer with a careful field list still grants privileges if one of those
fields is the owner.

Every rule here judges a serializer alone, not a request path. That is
deliberate. A serializer is reused across views far more often than it is
written, so a field list that is wrong is wrong everywhere, and tying the
finding to one endpoint would report the same defect once per route while
implying the other routes were fine. The cost is that we cannot say whether a
given serializer is reachable, which each rule states plainly rather than
dressing up as certainty.

The recurring trap is that DRF's defaults are not neutral. `fields` is not
merely a list: `'__all__'` and `exclude` both hand the decision to the model,
so a column added by a migration months later is published by a file nobody
edited.
"""

from __future__ import annotations

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
from djaudit.registry import Rule, RuleMeta, register

if TYPE_CHECKING:
    from djaudit.api.serializers import SerializerNode


class SerializerRule(Rule):
    """Base for rules that judge one serializer at a time.

    Carries the loop and the two exclusions every rule here shares, so a
    subclass is only its own judgement.
    """

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for node in ctx.api_surface.serializers.values():
            if not node.is_model_serializer:
                # A plain Serializer publishes exactly what it declares, so
                # there is no model behind it to over-share.
                continue
            if node.meta_inherited:
                # No `Meta` of its own: an abstract mixin whose field list is
                # decided by whichever concrete subclass uses it. Judging it
                # here would report the mixin and miss the class that ships.
                continue
            yield from self.inspect(ctx, node)

    def inspect(self, ctx: ProjectContext, node: SerializerNode) -> Iterator[Finding]:
        raise NotImplementedError

    def at(self, ctx: ProjectContext, node: SerializerNode, line: int | None = None) -> Location:
        where = line or node.lineno
        return Location(
            file=ctx.rel(node.path),
            line=where,
            snippet=ctx.snippet(node.path, where),
        )

    def model_evidence(self, ctx: ProjectContext, node: SerializerNode) -> Evidence | None:
        """The columns the model has today, since that is the real field list.

        A reader looking at ``fields = '__all__'`` sees one line. The useful
        reply is what that line expands to, because the argument for changing
        it is usually a column in the list they had forgotten was there.
        """
        if node.model is None:
            return None
        model = ctx.model_graph.get(node.model)
        if model is None:
            return None
        names = sorted(model.fields)
        if not names:
            return None
        return Evidence(
            kind=EvidenceKind.AST,
            content=f"{model.label} currently has: {', '.join(names)}",
            source="djaudit model graph",
        )


@register
class OpenFieldList(SerializerRule):
    meta = RuleMeta(
        id="DJA-008",
        title="Serializer publishes every model field",
        family=Family.DJA,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        rationale=(
            "fields = '__all__' does not name a field list, it delegates the decision "
            "to the model. Whatever columns the model has at import time are what the "
            "API returns and accepts, so a later migration publishes a new column "
            "without anyone editing this file or reviewing this serializer. The field "
            "that leaks this way is rarely one anybody would have listed on purpose."
        ),
        remediation=(
            "Replace '__all__' with an explicit list of the fields this endpoint is "
            "meant to expose. It is longer to write once and then fails closed "
            "forever: a new column stays invisible until somebody adds it here "
            "deliberately, which is the review the migration did not get."
        ),
        references=(
            "https://www.django-rest-framework.org/api-guide/serializers/#specifying-which-fields-to-include",
            "https://cwe.mitre.org/data/definitions/213.html",
        ),
        limitations=(
            "Whether any routed view uses this serializer is not checked, so one "
            "reached only by a management command reads the same as a public one.",
            "Severity does not rise when the model carries an obviously sensitive "
            "column, because that judgement is DJA-010's and two findings on one "
            "line with one fix between them is one finding too many.",
        ),
    )

    def inspect(self, ctx: ProjectContext, node: SerializerNode) -> Iterator[Finding]:
        if node.mode != "all":
            return
        model = node.model or node.model_ref or "its model"
        evidence = [
            Evidence(
                kind=EvidenceKind.AST,
                content=f"{node.name}.Meta.fields = '__all__'",
                source=ctx.rel(node.path),
            )
        ]
        columns = self.model_evidence(ctx, node)
        if columns is not None:
            evidence.append(columns)
        yield self.finding(
            location=self.at(ctx, node),
            message=(
                f"{node.name} sets fields = '__all__', so every column on "
                f"{model} is published and every column added later will be too."
            ),
            evidence=tuple(evidence),
        )
