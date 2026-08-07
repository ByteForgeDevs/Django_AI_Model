"""`DJI-004` -- client text spliced into a ``RawSQL`` or ``Func`` expression.

These are the two raw-SQL doors that are *expressions* rather than queryset
methods, so they can be handed to ``annotate()``, ``filter()`` or ``order_by()``
and travel a long way from where they were built.

``RawSQL(sql, params)`` is the blunter of the two and carries its own fix in
its second argument. ``Func`` is subtler: reading ``Func.as_sql`` in Django
6.0.7 rather than guessing, it ends in ``template % data``, where ``data``
holds ``function``, ``arg_joiner`` and the compiled expressions. So three of
its keywords are SQL text -- ``template``, ``function`` and ``arg_joiner`` --
and all three are interpolated before the driver ever sees a parameter.

Two measurements shaped this rule.

**Key on the callable, never on the keyword.** ``template=`` appears 44 times
across the three benchmark corpora and only twice is it a ``Func``: the other
42 are ``create(template=...)``, ``send_mail(template=...)`` and friends --
Django's email machinery, where the word means an HTML file. A rule triggered
by the keyword would have spent its life reading mail templates. Keyed on the
callable it sees exactly 20 calls: 12 ``Func`` and 8 ``RawSQL``.

**A same-file import is enough to know which ``Func`` this is.** All 19
resolvable calls in the corpus name a Django import in their own file
(``django.db.models.Func`` 11, ``django.db.models.expressions.RawSQL`` 8), and
no file in 3,091 defines a class of either name. So this rule reads the file's
own imports instead of building a project-wide class index, which is both
cheaper and, on this evidence, no less precise.

``Func``'s SQL keywords are keyword-only -- its signature is
``Func(*expressions, output_field=None, **extra)`` -- so a positional argument
is always an expression and never a template. That asymmetry with ``RawSQL``,
whose SQL *is* its first positional argument, is why the two are read
differently below.
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

RAW_SQL = "RawSQL"
FUNC = "Func"

SQL_ARGUMENT = "sql"
"""``RawSQL(sql, params)`` -- the statement is the first parameter."""

FUNC_SLOTS = ("template", "function", "arg_joiner")
"""``Func`` keywords that reach ``template % data`` as SQL text.

Read from ``Func.as_sql`` rather than remembered. ``output_field`` is absent
because it names a field class, and the ``*expressions`` are compiled and
parameterised by the query compiler, which is the whole point of using them.
"""

ORIGINS = frozenset(
    {
        "django.db.models",
        "django.db.models.expressions",
    }
)
"""Modules these names may legitimately come from."""

PHRASES = {
    (RAW_SQL, SQL_ARGUMENT): "the SQL passed to RawSQL()",
    (FUNC, "template"): "the template of Func()",
    (FUNC, "function"): "the function name in Func()",
    (FUNC, "arg_joiner"): "the argument joiner in Func()",
}
"""How each slot is named in the message, because ``the arg_joiner of`` is not English."""


def imported_from_django(tree: ast.AST) -> frozenset[str]:
    """The names in this module bound to a Django expression class.

    An ``import`` is a statement, so a plain ``ast.walk`` finds the ones
    written inside a function or a ``try`` as well as the ones at the top.

    An ``as`` alias is deliberately not honoured. :func:`expression_calls`
    matches the two names exactly, so an aliased import could never become a
    candidate for this to confirm, and handling it here would be a branch no
    input can reach -- a mutation probe found it dead before this comment
    replaced it. No file in the 3,091 of the benchmark corpora aliases either
    name, so the reachable behaviour and the measured behaviour agree.
    """
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in ORIGINS:
            continue
        for alias in node.names:
            if alias.name in (RAW_SQL, FUNC) and alias.asname is None:
                found.add(alias.name)
    return frozenset(found)


def sql_argument(call: ast.Call) -> ast.expr | None:
    """``RawSQL``'s statement, written positionally or by name."""
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == SQL_ARGUMENT:
            return keyword.value
    return None


def expression_calls(node: ast.AST) -> Iterator[Candidate]:
    """Every SQL-bearing argument of a ``RawSQL`` or ``Func`` call.

    Shared by the prefilter and the rule body so the two cannot drift; see
    :mod:`djaudit.rules._injection` for why that matters.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return
    name = node.func.id
    if name == RAW_SQL:
        statement = sql_argument(node)
        if statement is not None:
            yield Candidate(
                call=node,
                receiver=None,
                surface=RAW_SQL,
                argument=statement,
                slot=SQL_ARGUMENT,
            )
    elif name == FUNC:
        for keyword in node.keywords:
            if keyword.arg in FUNC_SLOTS:
                yield Candidate(
                    call=node,
                    receiver=None,
                    surface=FUNC,
                    argument=keyword.value,
                    slot=keyword.arg,
                )


@register
class RawExpressionInterpolation(SqlSurface):
    """`DJI-004` -- request data interpolated into a ``RawSQL`` or ``Func``."""

    WORDS = ("RawSQL", "Func")
    """The callables themselves.

    Bare names rather than ``RawSQL(``, because both are commonly aliased on
    import and both are short enough that a word test costs nothing.
    """

    meta = RuleMeta(
        id="DJI-004",
        title="Request data interpolated into a RawSQL or Func expression",
        family=Family.DJI,
        severity=Severity.CRITICAL,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "RawSQL splices its first argument into the query verbatim, and Func "
            "renders itself with `template % data`, so its template, function and "
            "arg_joiner keywords are SQL text too. A value built into any of them is "
            "syntax by the time the driver sees it, and a quote in client input ends "
            "the literal the author intended. Because both are expressions rather "
            "than queryset methods they are often built in one place and used in "
            "another, which is what lets an injectable one survive review."
        ),
        remediation=(
            "For RawSQL, put a placeholder in the SQL and the value in the params "
            "argument: RawSQL('title = %s', [value]). For Func, pass the value as an "
            "expression rather than building it into the template -- Value(value) is "
            "compiled and parameterised, and appears 78 times across the benchmark "
            "corpora, so it is already the house idiom. Better still, replace the "
            "expression with a built-in one; Django ships a Func subclass for almost "
            "every database function worth calling."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/expressions/#raw-sql-expressions",
            "https://docs.djangoproject.com/en/stable/ref/models/expressions/#func-expressions",
            "https://owasp.org/www-community/attacks/SQL_Injection",
            "https://cwe.mitre.org/data/definitions/89.html",
        ),
        limitations=(
            "Taint is tracked within one function. A value that reaches the "
            "expression through a helper's parameter is reported as unknown rather "
            "than tainted, so an injection assembled across two functions is unseen.",
            "The callable must be a bare name that the same file imports from "
            "django.db.models, so a RawSQL reached through a module attribute or "
            "re-exported by a project's own helper module is not recognised here.",
            "Func subclasses that set a composed template as a class attribute are "
            "not read, because the template is then written in a class body rather "
            "than at a call, and no such class appears in the benchmark corpora.",
            "Only the three documented Func keywords are treated as SQL. A custom "
            "template referring to some other key of extra would splice that value "
            "too, which this rule does not attempt to follow.",
        ),
    )

    def __init__(self) -> None:
        self._imports: dict[Path, frozenset[str]] = {}

    def candidates(self, node: ast.AST) -> Iterator[Candidate]:
        return expression_calls(node)

    def accepts(self, found: Composed, frame: Frame) -> bool:
        """The name must be one this file imported from Django.

        Cached per file because a rule instance lives for exactly one run, and
        because `accepts` is reached only by candidates that were already
        composed -- two calls across the whole corpus, so the walk it avoids is
        small either way and the cache is for tidiness, not for speed.
        """
        if frame.path not in self._imports:
            tree = frame.ctx.parse(frame.path)
            self._imports[frame.path] = frozenset() if tree is None else imported_from_django(tree)
        called = found.call.func
        return isinstance(called, ast.Name) and called.id in self._imports[frame.path]

    def report(self, ctx: ProjectContext, path: Path, site: Site) -> Finding:
        statement = site.composed
        named, confidence = self.spoken(site)
        slot = PHRASES.get(
            (statement.surface, statement.slot or ""),
            f"{statement.surface}()",
        )
        return self.finding(
            location=ctx.location(path, statement.call),
            confidence=confidence,
            message=(
                f"{self.phrasing(statement)} {slot}, splicing in request data: "
                f"{named}. Pass it as a parameter instead."
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
                "expression": statement.surface,
                "slot": statement.slot or "",
            },
        )
