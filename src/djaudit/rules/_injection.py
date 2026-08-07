"""Shared scaffolding for the rules that watch a string become SQL.

Django offers several doors into raw SQL -- ``cursor.execute``, ``.raw()``,
``.extra()``, ``RawSQL`` -- and the question at each is identical: was the
statement *composed*, and did a composed part reach the request? Only the door
differs. This module holds the part that does not differ, so that a rule file
contains the surface it names and its wording, and nothing else.

The shape of every one of them:

1. a whole-file substring prefilter, so most files never get parsed further;
2. a whole-tree pass with the rule's own :meth:`SqlSurface.surface`, so def-use
   chains are built only for files that really contain the shape;
3. a scope-by-scope walk, judging each candidate with the taint lattice.

Steps 1 and 2 exist for cost, and they are written so a rule cannot get them
subtly wrong: the prefilter calls the same :meth:`~SqlSurface.surface` the rule
body calls, so it can never admit less than the rule would report. A prefilter
that under-admits is a silent recall hole -- nothing downstream shows it, and no
test that runs the rule on a small file would ever catch it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from djaudit.dataflow.strings import Interpolation, interpolation
from djaudit.dataflow.taint import Taint, request_source, tainted_parts
from djaudit.models import Confidence, Finding
from djaudit.registry import Rule

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.querysets import QuerysetValue
    from djaudit.dataflow.scopes import Scope


@dataclass(frozen=True, slots=True)
class Candidate:
    """A call that hands a string to SQL, before asking how it was built."""

    call: ast.Call
    receiver: ast.expr | None
    """What the method was called on, where the surface is a method call.

    ``None`` for a surface that is a plain call, such as ``RawSQL(...)``, which
    has no receiver to identify.
    """

    surface: str
    """How the rule will name the door in its message -- ``execute``, ``raw``."""

    argument: ast.expr
    """The expression in the statement position, composed or not."""


@dataclass(frozen=True, slots=True)
class Composed:
    """A composed string handed to something that will parse it as SQL."""

    call: ast.Call
    receiver: ast.expr | None
    surface: str
    spliced: Interpolation
    through: str | None = None
    """The local the statement was built into, where it was not built in place.

    ``sql = f"..."`` on one line and ``cursor.execute(sql)`` on the next is the
    same defect written over two lines, and it is the *more* common way to
    write it. Naming the local lets the message point at the line to change.
    """


@dataclass(frozen=True, slots=True)
class Site:
    """A composed statement whose spliced parts reach the request."""

    composed: Composed
    parts: tuple[ast.expr, ...]


@dataclass(frozen=True, slots=True)
class Frame:
    """One scope, and the analyses available inside it.

    Passed whole to :meth:`SqlSurface.accepts` because the receiver tests differ
    in what they need: ``execute`` resolves its receiver through def-use chains,
    while ``.raw()`` asks the queryset tracker whether the receiver is a
    queryset at all. Both are cached per run, so reading them here is free.
    """

    ctx: ProjectContext
    path: Path
    scope: Scope
    chains: DefUse | None

    @property
    def querysets(self) -> dict[int, QuerysetValue]:
        """Queryset-valued expressions in this scope, keyed by node identity."""
        return self.ctx.tracked(self.path, self.scope)


def describe(part: ast.expr) -> str:
    """The spliced expression as source, for the message and the evidence."""
    return ast.unparse(part)


def direct(part: ast.expr) -> bool:
    """Whether the request source is written into the splice itself.

    ``f"... {request.GET['q']}"`` leaves nothing to infer; taint that arrived
    through a name was inferred, however soundly, and the two deserve different
    confidence.
    """
    return any(
        isinstance(inner, ast.expr) and request_source(inner) is not None
        for inner in ast.walk(part)
    )


def own_calls(scope: Scope) -> Iterator[ast.Call]:
    """Calls belonging to this scope rather than to a nested one.

    Descending into children here would report the same call once per enclosing
    scope, and resolve its names against the wrong chains.
    """
    nested = {id(child.node) for child in scope.children}
    stack: list[ast.AST] = list(ast.iter_child_nodes(scope.node))
    while stack:
        node = stack.pop()
        if id(node) in nested:
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


class SqlSurface(Rule):
    """A rule that reports request data composed into one raw-SQL surface.

    A subclass supplies :attr:`WORDS`, :meth:`surface` and :meth:`report`, and
    may narrow the candidates further with :meth:`accepts`. The traversal, the
    prefilter and the taint judgement are inherited and identical.
    """

    WORDS: tuple[str, ...] = ()
    """Substrings, any of which must appear in a file for it to be parsed.

    Every shape a subclass reports must contain at least one of these in its
    source text, or the rule silently stops seeing it. Keep them to the literal
    method or callable names the surface is written with.
    """

    def candidate(self, node: ast.AST) -> Candidate | None:
        """The statement-passing call this node is, if it is one.

        Deliberately says nothing about how the statement was built, because it
        is called once per node of every admitted file as a prefilter, long
        before there are def-use chains to answer that with.
        """
        raise NotImplementedError

    def compose(self, found: Candidate, chains: DefUse | None) -> Composed | None:
        """How the statement was built, in place or through one local.

        The indirect case is not a refinement. Composing into a local and
        passing the local is the ordinary way this defect is written, and a
        rule that only read the argument expression would miss it while
        reporting the one-liner beside it.

        One hop, and only from a name whose reaching bindings agree. Following
        further would mean re-implementing constant propagation, and the
        composition that matters is almost always the assignment immediately
        above the call.
        """
        spliced = interpolation(found.argument)
        if spliced is not None:
            return Composed(found.call, found.receiver, found.surface, spliced)
        if not isinstance(found.argument, ast.Name) or chains is None:
            return None
        use = chains.of(found.argument)
        if use is None:
            return None
        built = [
            spliced
            for binding in use.reaching
            if binding.value is not None and (spliced := interpolation(binding.value))
        ]
        if not built:
            return None
        return Composed(
            found.call, found.receiver, found.surface, built[0], through=found.argument.id
        )

    def accepts(self, found: Composed, frame: Frame) -> bool:
        """Whether this candidate really is the surface, given the scope.

        The default admits everything, which is right for a surface named by an
        unambiguous callable. Override where the method name alone is not
        enough -- ``.execute`` and ``.raw`` are both defined on plenty of things
        that are not databases.
        """
        return True

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or not any(word in source for word in self.WORDS):
                continue
            tree = ctx.parse(path)
            if tree is None:
                continue
            if not any(self.candidate(node) for node in ast.walk(tree)):
                continue
            root = ctx.scopes(path)
            if root is None:
                continue
            yield from self.inspect(ctx, path, root)

    def inspect(self, ctx: ProjectContext, path: Path, scope: Scope) -> Iterator[Finding]:
        """Walk one scope's own statements, then its children."""
        frame = Frame(ctx=ctx, path=path, scope=scope, chains=ctx.def_use(scope))
        for site in self.sites(frame):
            yield self.report(ctx, path, site)
        for child in scope.children:
            yield from self.inspect(ctx, path, child)

    def sites(self, frame: Frame) -> Iterator[Site]:
        """Composed statements in this scope whose parts reach the request."""
        for node in own_calls(frame.scope):
            candidate = self.candidate(node)
            if candidate is None:
                continue
            found = self.compose(candidate, frame.chains)
            if found is None or not self.accepts(found, frame):
                continue
            tainted = tuple(
                part
                for part, taint in tainted_parts(
                    found.spliced.node, found.spliced.parts, frame.chains
                )
                if taint is Taint.TAINTED
            )
            if tainted:
                yield Site(found, tainted)

    def report(self, ctx: ProjectContext, path: Path, site: Site) -> Finding:
        """Turn one site into a finding, in the rule's own words."""
        raise NotImplementedError

    def phrasing(self, statement: Composed) -> str:
        """How the statement was built, as the opening of a sentence."""
        if statement.through is None:
            return f"{statement.spliced.phrasing.capitalize()} builds"
        return f"{statement.spliced.phrasing.capitalize()} assigned to {statement.through} builds"

    def spoken(self, site: Site) -> tuple[str, Confidence]:
        """The tainted parts named, and the confidence their directness earns."""
        named = ", ".join(describe(part) for part in site.parts)
        immediate = any(direct(part) for part in site.parts)
        return named, Confidence.CERTAIN if immediate else Confidence.FIRM
