"""`DJI-002` -- client text spliced into the SQL given to ``.raw()``.

``Model.objects.raw()`` is the ORM's own door out of the ORM. It takes a
statement, runs it, and maps the rows onto model instances -- and that last step
is the whole problem, because it makes the call *look* like ORM usage. A
``.filter()`` beside it is parameterised by construction and cannot be injected;
``.raw()`` next to it is a string, and the model instances that come back say
nothing about how the string was built.

``.raw()`` takes ``params`` for exactly the same reason ``cursor.execute`` does,
and passes them the same way. So the trigger is the same as :mod:`DJI-001
<djaudit.rules.raw_sql>`: composed **and** reaching the request. Composition
alone is not a defect, and the corpus is again the evidence -- the one composed
``.raw()`` in 3,091 files wraps ``queryset.query.sql_with_params()``, which is
SQL the ORM generated, with its values already travelling separately in
``params``. Flagging that would be wrong, and it is the idiom, not an outlier.

**The receiver is identified by the model graph, not by its name.** ``.raw`` is
a method on other things -- ``requests``' ``Response.raw`` is the one a name
blocklist would have been written for -- so the receiver has to be an
expression :mod:`the queryset tracker <djaudit.dataflow.querysets>` already
believes is a queryset. That test accepts ``Book.objects.raw(...)``, a queryset
held in a local, and a chain like ``Book.objects.filter(...).raw(...)``, and
declines ``cursor.raw``, ``response.raw`` and a bare parameter, structurally.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

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
from djaudit.rules._injection import Candidate, Composed, Frame, Site, SqlSurface

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext

RAW = "raw"
"""The queryset method that takes a statement instead of building one."""

RAW_ARGUMENT = "raw_query"
"""Django's own name for the first parameter, usable as a keyword."""

RECEIVER_LIMIT = 40
"""Where to cut the receiver quoted in the message.

The receiver is a chain, and a long one -- ``CachedValue.objects.
prefetch_related(*prefetch)`` is real -- would push the part of the message
that matters off the line.
"""


def receiver(statement: Composed) -> str:
    """The receiver as source, shortened, for naming the call in the message.

    Quoting what was written beats naming the model: it is what the reader will
    search the file for, and it needs no second pass over the tracker.
    """
    if statement.receiver is None:
        return "objects"
    text = ast.unparse(statement.receiver)
    if len(text) <= RECEIVER_LIMIT:
        return text
    return text[: RECEIVER_LIMIT - 3] + "..."


def sql_argument(call: ast.Call) -> ast.expr | None:
    """The statement handed to ``.raw()``, positionally or by keyword.

    ``params`` is deliberately not read: a value there is the *fix*, and a rule
    that looked at it would report the correct code as well as the wrong.
    """
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == RAW_ARGUMENT:
            return keyword.value
    return None


def statement_call(node: ast.AST) -> Candidate | None:
    """A ``.raw()`` call with something in the statement position.

    Shared by the prefilter and the rule body so the two cannot drift; see
    :mod:`djaudit.rules._injection` for why that matters.
    """
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != RAW:
        return None
    statement = sql_argument(node)
    if statement is None:
        return None
    return Candidate(call=node, receiver=node.func.value, surface=RAW, argument=statement)


@register
class RawQuerysetInterpolation(SqlSurface):
    """`DJI-002` -- request data interpolated into a ``.raw()`` query."""

    WORDS = (".raw(",)
    """The literal call, which is far narrower than the word alone.

    ``.raw(`` appears in 1 file of the 3,091 in the three benchmark corpora;
    ``raw`` appears in 215, largely as ``raw_id_fields`` and in prose. No file
    in any corpus writes the call with a space before the parenthesis.
    """

    meta = RuleMeta(
        id="DJI-002",
        title="Request data interpolated into a .raw() query",
        family=Family.DJI,
        severity=Severity.CRITICAL,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Model.objects.raw() sends its first argument to the database as SQL. "
            "Returning model instances does not make the statement safe, and the ORM "
            "does no escaping on the way in: a quote in a value built into the string "
            "ends the literal the author wrote and starts an expression the client "
            "chose. The params argument exists to carry values, and is sent to the "
            "driver separately, where nothing in it can be read as syntax."
        ),
        remediation=(
            "Move the value into params and leave a placeholder in the statement: "
            "Model.objects.raw('SELECT ... WHERE x = %s', [value]). If the query can "
            "be expressed with the ORM, prefer that -- filter() and annotate() are "
            "parameterised by construction. Where an identifier must be interpolated, "
            "take it from model metadata or check it against a fixed allowlist first."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/topics/db/sql/#passing-parameters-into-raw",
            "https://owasp.org/www-community/attacks/SQL_Injection",
            "https://cwe.mitre.org/data/definitions/89.html",
        ),
        limitations=(
            "Taint is tracked within one function. A value that reaches the statement "
            "through a helper's parameter is reported as unknown rather than tainted, "
            "so an injection assembled across two functions is not seen.",
            "The receiver must be an expression the model graph recognises as a "
            "queryset, so a .raw() call on a manager reached through an unresolved "
            "import, or on a queryset returned by an unrecognised helper, is skipped.",
            "Composition alone is never reported, so an interpolated statement whose "
            "value arrives from a source this analysis does not model, such as a "
            "database row written by an earlier request, is not flagged.",
        ),
    )

    def candidate(self, node: ast.AST) -> Candidate | None:
        return statement_call(node)

    def accepts(self, found: Composed, frame: Frame) -> bool:
        """The receiver must be something the tracker calls a queryset.

        The tracker keys a whole chain at its outermost call, so the node to
        ask about is the ``.raw()`` call itself rather than its receiver.
        """
        return id(found.call) in frame.querysets

    def report(self, ctx: ProjectContext, path: Path, site: Site) -> Finding:
        statement = site.composed
        named, confidence = self.spoken(site)
        return self.finding(
            location=ctx.location(path, statement.call),
            confidence=confidence,
            message=(
                f"{self.phrasing(statement)} the SQL passed to {receiver(statement)}"
                f".raw(), splicing in request data: {named}. Pass it in params instead."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=ast.unparse(statement.spliced.node),
                    source=f"{ctx.rel(path)}:{statement.call.lineno}",
                ),
            ),
            properties={
                "composition": statement.spliced.kind.value,
                "tainted": named,
            },
        )
