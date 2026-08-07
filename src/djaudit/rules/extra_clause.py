"""`DJI-003` -- client text spliced into a ``.extra()`` clause.

``.extra()`` is the oldest raw-SQL door in Django and the widest. Four of its
six arguments are SQL -- ``select``, ``where``, ``tables``, ``order_by`` --
and only ``params`` and ``select_params`` are not. That split was read off
``inspect.signature(QuerySet.extra)`` on Django 6.0.7 rather than remembered;
an earlier draft of this rule also listed ``having``, which has not been a
parameter for a long time. The classic
Django SQL injection is a ``where`` list built with ``%`` a few characters from
the ``params`` argument that would have fixed it::

    Book.objects.extra(where=["title = '%s'" % request.GET["q"]])   # injectable
    Book.objects.extra(where=["title = %s"], params=[request.GET["q"]])  # not

Django's own documentation warns about this method and advises against using
it, which is the reason for the one number worth knowing here: **``.extra()``
does not appear once in the 3,091 files of the three benchmark corpora.** This
rule therefore has no corpus evidence behind it, and cannot have any. It is
specified by Django's documented signature and tested against fixtures, and it
is included because the codebases that still call ``.extra()`` are exactly the
old ones nobody has audited.

The trigger and the receiver test are :mod:`DJI-002
<djaudit.rules.raw_queryset>`'s, for the same reasons.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
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

EXTRA = "extra"

POSITIONS = ("select", "where", "params", "tables", "order_by", "select_params")
"""``.extra()``'s parameters in order, so a positional call can be read too."""

SQL_SLOTS = frozenset({"select", "where", "tables", "order_by"})
"""The arguments Django parses as SQL.

``params`` and ``select_params`` are absent deliberately: a value there is the
fix, and reading them would report the correct code beside the wrong.
"""


def clauses(call: ast.Call) -> Iterator[tuple[str, ast.expr]]:
    """Every expression ``.extra()`` will treat as SQL, with its argument name.

    Each of these arguments is a *container* of SQL rather than a statement:
    ``where`` and ``tables`` take lists, ``select`` takes a mapping of alias to
    expression. The elements are yielded, not the container, because that is
    where composition happens and where the reader needs pointing.
    """
    for index, argument in enumerate(call.args):
        if index < len(POSITIONS) and POSITIONS[index] in SQL_SLOTS:
            yield from ((POSITIONS[index], part) for part in elements(argument))
    for keyword in call.keywords:
        if keyword.arg in SQL_SLOTS:
            yield from ((keyword.arg, part) for part in elements(keyword.value))


def elements(argument: ast.expr) -> Iterator[ast.expr]:
    """The SQL inside a clause argument, one piece at a time."""
    if isinstance(argument, ast.Dict):
        # `select={'alias': 'SQL'}` -- the values are SQL, the keys are aliases.
        yield from (value for value in argument.values if value is not None)
    elif isinstance(argument, ast.List | ast.Tuple | ast.Set):
        yield from argument.elts
    else:
        # A name holding the list, or a comprehension building it. Yielding it
        # whole lets `compose` resolve a name; anything else it cannot read
        # falls out as uncomposed.
        yield argument


def statement_calls(node: ast.AST) -> Iterator[Candidate]:
    """Every SQL-bearing argument of a ``.extra()`` call.

    Shared by the prefilter and the rule body so the two cannot drift; see
    :mod:`djaudit.rules._injection` for why that matters.
    """
    if not isinstance(node, ast.Call):
        return
    if not isinstance(node.func, ast.Attribute) or node.func.attr != EXTRA:
        return
    for slot, part in clauses(node):
        yield Candidate(
            call=node, receiver=node.func.value, surface=EXTRA, argument=part, slot=slot
        )


@register
class ExtraClauseInterpolation(SqlSurface):
    """`DJI-003` -- request data interpolated into a ``.extra()`` clause."""

    WORDS = (".extra(",)
    """The literal call. No file in any benchmark corpus contains it."""

    meta = RuleMeta(
        id="DJI-003",
        title="Request data interpolated into a .extra() clause",
        family=Family.DJI,
        severity=Severity.CRITICAL,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "QuerySet.extra() splices its select, where, tables and order_by arguments "
            "into the generated SQL as written. A value built into one of "
            "them is syntax by the time the driver sees it, so a quote in client input "
            "ends the literal the author intended. The params and select_params "
            "arguments exist to carry values and are sent separately, where nothing in "
            "them can change what the statement means."
        ),
        remediation=(
            "Put a placeholder in the clause and the value in params: "
            "extra(where=['title = %s'], params=[value]). Django's documentation "
            "advises against extra() altogether -- filter(), annotate() and "
            "Func/Value expressions cover almost every use and are parameterised by "
            "construction, so replacing the call outright is usually the better fix."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#extra",
            "https://owasp.org/www-community/attacks/SQL_Injection",
            "https://cwe.mitre.org/data/definitions/89.html",
        ),
        limitations=(
            "Taint is tracked within one function. A value that reaches the clause "
            "through a helper's parameter is reported as unknown rather than tainted, "
            "so an injection assembled across two functions is not seen.",
            "The receiver must be an expression the model graph recognises as a "
            "queryset, so an extra() call on a manager reached through an unresolved "
            "import, or on a queryset returned by an unrecognised helper, is skipped.",
            "No call to extra() appears in any of the three benchmark corpora, so "
            "this rule's precision is measured against fixtures alone and not against "
            "the real-world code the other rules in this family were tuned on.",
        ),
    )

    def candidates(self, node: ast.AST) -> Iterator[Candidate]:
        return statement_calls(node)

    def accepts(self, found: Composed, frame: Frame) -> bool:
        """The receiver must be something the tracker calls a queryset."""
        return id(found.call) in frame.querysets

    def report(self, ctx: ProjectContext, path: Path, site: Site) -> Finding:
        statement = site.composed
        named, confidence = self.spoken(site)
        return self.finding(
            location=ctx.location(path, statement.call),
            confidence=confidence,
            message=(
                f"{self.phrasing(statement)} the {statement.slot} clause of .extra(), "
                f"splicing in request data: {named}. Pass it in params instead."
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
                "clause": statement.slot or "",
            },
        )
