"""Rules about the shape of the data rather than the code that reads it.

Every rule here is fixed by a migration. That makes them the most expensive
findings in the tool to act on and the cheapest to act on *early*, which is the
whole argument for reporting them: a nullable `CharField` costs nothing on the
day it is written and cannot be undone cheaply once four years of rows have
both spellings of empty in them.

None of these are vulnerabilities. They are ways for a correct-looking query to
return a wrong answer, which is a category the other families do not cover.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import pairwise

from djaudit.astutils import dotted_name
from djaudit.context import ProjectContext
from djaudit.graph.nodes import FieldNode, ModelNode
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

_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+")


def camel_words(name: str) -> list[str]:
    """The words in a class name, lowercased, in order.

    Matching on substrings instead would report `Recorder` for `order` and
    `Historic` for `history`, which is how a name-based rule earns its
    reputation. A model is a noun phrase and splitting it back into nouns is
    both cheap and exact.
    """
    return [part.lower() for part in _CAMEL.findall(name.replace("_", " "))]


def name_tokens(name: str) -> frozenset[str]:
    """Every word in a class name, plus each pair of adjacent words joined.

    `ChangeLog` is two words and `changelog` is one, and which of those a
    codebase writes is a coin flip. Emitting the bigrams too means the word
    list can name the compound without having to guess how it was spelled --
    `ObjectChange`, `LogEntry` and `AuditLogEntry` all reduce to a term the
    list can hold, while `Recorder` still produces no pair at all.
    """
    words = camel_words(name)
    pairs = [a + b for a, b in pairwise(words)]
    return frozenset(words) | frozenset(pairs)


RETAINED_WORDS = frozenset(
    {
        "order",
        "invoice",
        "payment",
        "transaction",
        "charge",
        "refund",
        "receipt",
        "ledger",
        "billing",
        "payout",
        "settlement",
        "purchase",
        "audit",
        "auditlog",
        "logentry",
        "changelog",
        "objectchange",
        "journal",
        "revision",
    }
)
"""Words naming a record somebody is expected to be able to produce later.

Deliberately excludes `subscription`, which NetBox uses for a
change-notification subscription that is exactly right to cascade, and every
bare `log`, which names a debugging artefact at least as often as an audit
trail. The omissions cost recall on a billing subscription and buy the rule
the right to be believed when it does fire.
"""

CASCADE = "CASCADE"

RELATION_KINDS = frozenset({"ForeignKey", "OneToOneField"})


def on_delete_of(field: FieldNode | None) -> str | None:
    """The `on_delete` policy as a bare name, or `None` if it was not readable."""
    if field is None or "on_delete" not in field.kwargs:
        return None
    resolved = dotted_name(field.kwargs["on_delete"])
    return resolved.rsplit(".", 1)[-1] if resolved else None


class ModelRule(Rule):
    """Base for rules that judge one model at a time.

    Iterates abstract bases as well as concrete models, because a field
    declared on `class Owned(models.Model): class Meta: abstract = True` is
    written once and inherited everywhere, which makes it both the most
    consequential place to get a field wrong and the easiest place to miss it.
    Rules that judge a table rather than a declaration should skip
    `model.is_abstract` themselves.
    """

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for model in ctx.model_graph.models.values():
            if model.is_proxy:
                continue
            yield from self.inspect(ctx, model)

    def inspect(self, ctx: ProjectContext, model: ModelNode) -> Iterator[Finding]:
        raise NotImplementedError

    def at(self, ctx: ProjectContext, model: ModelNode, line: int) -> Location:
        return Location(
            file=ctx.rel(model.path),
            line=line,
            snippet=ctx.snippet(model.path, line),
        )


@register
class CascadingRetainedRecord(ModelRule):
    """DJD-001 -- deleting a user deletes the evidence."""

    meta = RuleMeta(
        id="DJD-001",
        title="Retained record cascades from the user",
        family=Family.DJD,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "`on_delete=CASCADE` on a foreign key to the user model means deleting a "
            "user deletes every row that pointed at them. For preferences and "
            "bookmarks that is exactly right. For an invoice, a payment or an audit "
            "entry it destroys the record of something that happened, and the delete "
            "arrives from wherever a user is removed -- an admin action, a GDPR "
            "erasure request, a `User.objects.filter(...).delete()` in a management "
            "command -- with no indication that financial history went with it. The "
            "rows are gone before anybody asks whether they should have been."
        ),
        remediation=(
            "Use `on_delete=models.PROTECT` so the delete fails loudly and the caller "
            "has to decide, or `models.SET_NULL` with `null=True` to keep the record "
            "and drop the link, which is what an anonymisation flow wants. If the "
            "user really must be erasable, anonymise the row rather than deleting it, "
            "and make that an explicit step rather than a side effect of a cascade."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/fields/#django.db.models.ForeignKey.on_delete",
            "https://docs.djangoproject.com/en/stable/ref/models/fields/#django.db.models.PROTECT",
        ),
        limitations=(
            "Whether a record must be retained is a legal and business question that "
            "no static reading can answer, so this rule judges by the words in the "
            "model name and will not see a retained record named for its product.",
            "A project that erases users by anonymising them rather than by calling "
            "delete never reaches this cascade, and is reported here anyway because "
            "the deletion path is a fact about the schema rather than about the code.",
        ),
    )

    def inspect(self, ctx: ProjectContext, model: ModelNode) -> Iterator[Finding]:
        words = name_tokens(model.name) & RETAINED_WORDS
        if not words:
            return
        for edge in model.relations:
            if not edge.points_at_user or edge.kind not in RELATION_KINDS:
                continue
            if edge.inherited_from is not None or edge.implicit:
                # Report the class that wrote the line, not every class that
                # inherited it, or an abstract base becomes N findings.
                continue
            field = model.fields.get(edge.field_name)
            policy = on_delete_of(field)
            if policy != CASCADE:
                # Includes the unreadable case: `on_delete` given as something
                # we could not evaluate is not evidence of a cascade.
                continue
            yield self.finding(
                message=(
                    f"{model.label}.{edge.field_name} cascades from the user, so deleting "
                    f"a user deletes their {model.name} rows."
                ),
                location=self.at(ctx, model, edge.lineno),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"{edge.field_name} = {edge.kind}("
                            f"{edge.target_ref or 'settings.AUTH_USER_MODEL'}, "
                            f"on_delete=CASCADE)"
                        ),
                        source=ctx.rel(model.path),
                    ),
                    Evidence(
                        kind=EvidenceKind.AST,
                        content=(
                            f"{model.name} reads as a retained record on {', '.join(sorted(words))}"
                        ),
                        source="djaudit model graph",
                    ),
                ),
            )


STRING_FIELDS = frozenset(
    {
        "CharField",
        "TextField",
        "SlugField",
        "EmailField",
        "URLField",
        "FilePathField",
        "FileField",
        "ImageField",
    }
)
"""Field kinds whose Python value is a string, and whose empty value is `""`.

`FileField` and `ImageField` are in the list because the column holds a path
and the empty path is `""`, which is the same collision on a different name.
"""


def covered_by_uniqueness(model: ModelNode, name: str) -> bool:
    """Whether a uniqueness rule spans this column.

    Django documents `null=True` as the way to have more than one row with no
    value, and that argument does not weaken when the uniqueness is composite:
    pretix's `Customer` is unique on `(organizer, email)` and has to be able to
    hold many customers of one organizer with no email at all. Reading only
    field-level `unique=True` reported exactly that field, which is how this
    check came to exist.
    """
    if any(name in group for group in model.unique_together):
        return True
    return any(c.is_unique and name in c.fields for c in model.constraints)


@register
class NullableStringField(ModelRule):
    meta = RuleMeta(
        id="DJD-002",
        title="Nullable string column has two empty values",
        family=Family.DJD,
        severity=Severity.LOW,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "A nullable string column can be empty in two ways, `NULL` and `''`, and "
            "Django's own documentation asks projects not to build one. The cost is "
            "not paid at the declaration, it is paid at every read: `filter(x='')` "
            "does not match the `NULL` rows and `filter(x=None)` does not match the "
            "empty ones, so a query that looks exhaustive silently returns a subset. "
            "Because `blank=True` is absent here the project's own validation layer "
            "would reject the empty value, which means any `NULL` in the column "
            "arrived from a migration backfill or a direct write rather than from "
            "anything that states which spelling means empty."
        ),
        remediation=(
            "Drop `null=True` and give the column `default=''`, then write a data "
            "migration that rewrites the existing `NULL`s to `''` so the two "
            "populations converge. If the column genuinely needs to distinguish "
            "'not supplied' from 'supplied as empty', keep `null=True`, add "
            "`blank=True` to say so, and make the distinction explicit at every "
            "read rather than leaving it to whichever spelling a row happens to hold."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/fields/#null",
            "https://docs.djangoproject.com/en/stable/ref/models/fields/#blank",
        ),
        limitations=(
            "Fields carrying `blank=True` are not reported even though they hold the "
            "same two values, because the project has then stated that empty is a "
            "permitted input and the convention is at least written down somewhere.",
            "A string column spanned by any uniqueness rule is exempt, whether that "
            "is `unique=True` or a composite, since Django documents `null` "
            "as the way to allow more than one row with no value and the alternative "
            "would be a unique constraint that rejects the second empty string.",
        ),
    )

    def inspect(self, ctx: ProjectContext, model: ModelNode) -> Iterator[Finding]:
        bad = [
            f
            for f in model.fields.values()
            if f.kind in STRING_FIELDS
            and f.null
            and not f.blank
            and not f.unique
            and not covered_by_uniqueness(model, f.name)
        ]
        if not bad:
            return
        # One finding per model, not per column: pretix's Invoice declares
        # fifteen of these and they are one migration to fix, not fifteen.
        names = ", ".join(f.name for f in bad)
        yield self.finding(
            message=(
                f"{model.label} declares {len(bad)} nullable string "
                f"{'column' if len(bad) == 1 else 'columns'} with no `blank=True`: {names}."
            ),
            location=self.at(ctx, model, bad[0].lineno),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content="\n".join(
                        f"{f.name} = models.{f.kind}(..., null=True)  # line {f.lineno}"
                        for f in bad
                    ),
                    source=ctx.rel(model.path),
                ),
            ),
        )
