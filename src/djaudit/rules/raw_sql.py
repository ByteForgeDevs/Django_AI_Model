"""`DJI-001` -- client text spliced into the SQL given to a database cursor.

``cursor.execute`` takes parameters for a reason. The second argument is passed
to the driver separately from the statement, so a value in it can never be read
as syntax; a value built into the first argument has already become syntax by
the time the driver sees it. That is the entire distinction, and it is the one
this rule enforces.

**It reports reach, not shape.** An interpolated statement is not by itself a
defect, and the corpus settles the point: across healthchecks, NetBox and
pretix there are exactly five ``.execute()`` calls whose statement was
composed, and all five are correct. Three splice a local built from literals,
two splice ``_meta.db_table`` -- and a table name *cannot* be a parameter, so
interpolating it is the only way to write the query at all. A rule that flagged
composition would have scored nought for five on mature code and taught its
first reader to switch it off.

So the trigger is :mod:`djaudit.dataflow.taint` reaching the spliced value: the
statement is composed **and** one of the spliced parts reads something Django
filled from the request. Everything that is merely unresolvable stays quiet, by
a decision recorded in that module -- a helper's own parameter is the most
common real defect of this kind and also the most common safe pattern, and
nothing in one function's text tells the two apart.

**Cursors are identified, not assumed.** ``.execute`` is a method on plenty of
things that are not database cursors, so the receiver has to resolve to one:
``connection.cursor()`` written inline, or a name bound to it by assignment or
by ``with``. All five corpus sites are recognised, which is what makes their
silence meaningful -- a rule that stayed quiet because it could not find the
cursors would look identical from the outside.
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
    from djaudit.dataflow.chains import DefUse

EXECUTE = frozenset({"execute", "executemany"})
"""The DB-API methods that take a statement. ``executescript`` is SQLite-only
and takes no parameters at all, so it is a different rule's problem."""

CURSOR_FACTORY = "cursor"
"""``connection.cursor()``. The call that produces the object we care about."""

CURSOR_NAMES = frozenset({"cursor", "cur", "c"})
"""Conventional names, used only when a name resolves to nothing.

A fallback rather than the test: it exists for the cursor handed in as a
parameter, where there is no binding to resolve and the convention is the only
evidence available.
"""


def sql_argument(call: ast.Call) -> ast.expr | None:
    """The statement argument of an ``execute`` call.

    DB-API positions it first, and psycopg accepts ``query=`` by keyword.
    Returning ``None`` for a call with no arguments is not defensive tidiness:
    ``cursor.execute()`` is a ``TypeError`` at runtime but parses cleanly, and
    a rule that indexed ``args[0]`` would crash on it.
    """
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg in {"sql", "query", "operation"}:
            return keyword.value
    return None


def produces_cursor(node: ast.expr) -> bool:
    """Whether this expression is a ``.cursor()`` call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == CURSOR_FACTORY
    )


def is_cursor(receiver: ast.expr, chains: DefUse | None) -> bool:
    """Whether ``receiver`` holds a database cursor.

    Resolution first, convention second. ``with connection.cursor() as c`` and
    ``c = connection.cursor()`` both leave a binding whose value is the factory
    call, and reading that is stronger evidence than any name.

    The convention is consulted only where resolution has nothing to say. A
    binding can exist and still be silent -- a parameter carries no value, and
    neither does an ``except`` target -- and treating that silence as a denial
    is not conservatism but a different claim entirely, one that would reject
    the cursor a helper was handed. Where resolution *does* have a value and it
    is not a cursor, the name loses: a local called ``cursor`` that was
    assigned a workflow is a workflow.
    """
    if produces_cursor(receiver):
        return True
    if isinstance(receiver, ast.Attribute) and receiver.attr == CURSOR_FACTORY:
        # `self.cursor`, `self.connection.cursor` -- an attribute holding one
        # rather than a call producing one.
        return True
    if not isinstance(receiver, ast.Name):
        return False
    if chains is not None:
        use = chains.of(receiver)
        if use is not None and use.reaching:
            valued = [binding for binding in use.reaching if binding.value is not None]
            if valued:
                return any(produces_cursor(binding.value) for binding in valued)  # type: ignore[arg-type]
    return receiver.id in CURSOR_NAMES


def statement_call(node: ast.AST) -> Candidate | None:
    """An ``execute``-like call with something in the statement position.

    Used twice, and deliberately: once as a cheap whole-file prefilter that
    decides whether the scope tree is worth building at all, and once inside
    the scope walk. Sharing it means the prefilter cannot drift from the test
    it is meant to approximate -- a prefilter that admits less than the rule
    reports is a silent recall hole, and nothing downstream would show it.
    """
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in EXECUTE:
        return None
    statement = sql_argument(node)
    if statement is None:
        return None
    return Candidate(
        call=node, receiver=node.func.value, surface=node.func.attr, argument=statement
    )


@register
class RawSqlInterpolation(SqlSurface):
    """`DJI-001` -- request data interpolated into a cursor statement."""

    meta = RuleMeta(
        id="DJI-001",
        title="Request data interpolated into a SQL statement",
        family=Family.DJI,
        severity=Severity.CRITICAL,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "The statement passed to cursor.execute() is parsed as SQL. A value built "
            "into it has become syntax before the driver sees it, so a quote character "
            "in client input ends the literal the author intended and begins an "
            "expression the client chose. Parameters are never parsed: the driver sends "
            "them beside the statement, and no content can change its meaning."
        ),
        remediation=(
            "Pass the value as a query parameter: cursor.execute('... WHERE x = %s', "
            "[value]). Keep the placeholder in the statement and the value out of it. "
            "Where an identifier must be interpolated because a parameter cannot carry "
            "one, take it from model metadata or check it against a fixed allowlist "
            "before it reaches the string."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/topics/db/sql/#performing-raw-queries",
            "https://owasp.org/www-community/attacks/SQL_Injection",
            "https://cwe.mitre.org/data/definitions/89.html",
        ),
        limitations=(
            "Taint is tracked within one function. A value that reaches the statement "
            "through a helper's parameter is reported as unknown rather than tainted, "
            "so an injection assembled across two functions is not seen.",
            "A cursor must resolve to a connection.cursor() call or carry a "
            "conventional name. A cursor obtained some other way, such as from a "
            "connection pool wrapper, is not recognised as one.",
            "Composition alone is never reported, so an interpolated statement whose "
            "value arrives from a source this analysis does not model, such as a "
            "database row written by an earlier request, is not flagged.",
        ),
    )

    WORDS = ("execute",)
    """Every shape reported is a call to a method named ``execute``.

    A composed statement handed to one appears in 5 files across the three
    benchmark corpora, out of 3,091; this word is what keeps def-use chains
    from being built for the other 3,086.
    """

    def candidate(self, node: ast.AST) -> Candidate | None:
        return statement_call(node)

    def accepts(self, found: Composed, frame: Frame) -> bool:
        """``.execute`` is a method on plenty of things that are not cursors."""
        return found.receiver is not None and is_cursor(found.receiver, frame.chains)

    def report(self, ctx: ProjectContext, path: Path, site: Site) -> Finding:
        statement = site.composed
        named, confidence = self.spoken(site)
        return self.finding(
            location=ctx.location(path, statement.call),
            confidence=confidence,
            message=(
                f"{self.phrasing(statement)} the SQL passed to {statement.surface}(), "
                f"splicing in request data: {named}. Pass it as a query parameter "
                f"instead."
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
