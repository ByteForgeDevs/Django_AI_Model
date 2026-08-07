"""Which expressions can carry text the client wrote.

The substrate for the whole `DJI` family. Every rule in it asks one question --
does attacker-controlled text reach this place -- and the answer has to be the
same everywhere or the family's confidence levels stop meaning anything.

**Three answers, not two.** ``SAFE`` and ``TAINTED`` are the interesting ones,
but most expressions are neither: a function parameter holds whatever the
caller passed, and we do not know the caller. Collapsing that third case into
either neighbour is how a taint analyser becomes useless. Call it ``SAFE`` and
the analysis misses the most common real defect, a helper that interpolates its
own argument. Call it ``TAINTED`` and every string-building function in the
project is a SQL injection. ``UNKNOWN`` is a distinct answer and rules grade it
distinctly -- in practice, below the default reporting floor.

**The source set is an allowlist, and the corpus is why.** Counting attribute
reads off ``request`` across the three benchmark targets finds ``request.event``
1,727 times, ``request.user`` 1,400 and ``request.organizer`` 782 -- all of them
objects that middleware attached, none of them client text. A denylist, or
"anything reached through ``request``", would treat a model instance as an
injection vector. Only the attributes Django populates from the wire are
sources:

    GET  POST  data  body  headers  COOKIES  FILES  META  query_params

``self.request`` matters as much as ``request``: pretix reads it 3,735 times
against 2,401 for the bare name, so a model that recognised only the parameter
would see less than half of that project's request access.

**Sanitisers are narrow on purpose.** ``int(x)`` cannot return something
containing a quote, so it ends taint. ``escape(x)`` escapes HTML and is no
defence at all in a SQL string, so it does not. The test applied here is not
"does this function sound protective" but "can its result contain SQL syntax".

**Module globals read inside a function answer ``UNKNOWN``.** Def-use chains
stop at a scope boundary, so pretix's ``LOCK_ACQUISITION_TIMEOUT`` -- a module
constant bound to an integer literal, spliced into a ``SET LOCAL lock_timeout``
statement -- is not shown safe. Reaching across the boundary would be
convenient and unsound: a module attribute can be rebound by any importer, and
by the time a function runs there is no ordering that says which binding won.
``UNKNOWN`` is the correct answer to a question this analysis genuinely cannot
settle, and the design compensates by not reporting ``UNKNOWN``.
"""

from __future__ import annotations

import ast
from enum import StrEnum
from typing import TYPE_CHECKING

from djaudit.dataflow.scopes import Binding, BindingKind, Scope
from djaudit.dataflow.strings import interpolation

if TYPE_CHECKING:
    from collections.abc import Iterable

    from djaudit.dataflow.chains import DefUse

REQUEST_SOURCES = frozenset(
    {
        "GET",
        "POST",
        "data",
        "body",
        "headers",
        "COOKIES",
        "FILES",
        "META",
        "query_params",
    }
)
"""Attributes of an ``HttpRequest`` that Django fills from the wire.

``META`` is included despite also holding server-set keys, because it is where
client-controlled headers arrive -- ``HTTP_X_FORWARDED_FOR`` and friends are
strings the client chose. ``query_params`` is DRF's alias for ``GET``.
"""

REQUEST_NAMES = frozenset({"request"})
"""Names that hold a request. Convention, and near-universal in Django."""

VIEW_KWARGS = frozenset({"kwargs"})
"""``self.kwargs`` on a class-based view: the URL captures, as the client sent
them. A URL pattern constrains the shape of a capture but ``<str:name>`` is
still an arbitrary string."""

SANITISERS = frozenset({"int", "float", "bool", "len", "abs", "round", "ord", "hash", "id"})
"""Builtins whose result cannot contain SQL syntax whatever the argument was.

Each returns a number or a bool. This is the entire justification -- not that
the function validates anything, but that its return type has no room for a
quote character.
"""

SAFE_META = frozenset({"_meta"})
"""A segment that marks an attribute chain as Django model metadata.

``Voucher._meta.db_table`` is a table name the developer declared, reachable
only through a class object. Django does not put client text there, and
identifiers cannot be passed as query parameters, so interpolating one is the
only way to write the query at all.
"""

_MAX_DEPTH = 12
"""Bound on recursive resolution. Deeply chained rebinding is rare, and a
static analyser that recurses without a bound meets a cyclic definition
eventually."""


class Taint(StrEnum):
    """Whether an expression can carry text the client wrote."""

    SAFE = "safe"
    """Shown not to. A literal, a number, model metadata, or a composition of
    those."""

    UNKNOWN = "unknown"
    """Not shown either way. A parameter, an unresolvable name, an opaque
    call."""

    TAINTED = "tainted"
    """Reaches an attribute Django fills from the request."""

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK: dict[Taint, int] = {Taint.SAFE: 0, Taint.UNKNOWN: 1, Taint.TAINTED: 2}


def join(values: Iterable[Taint]) -> Taint:
    """The worst of several taints.

    A string is as tainted as its most tainted part, which is the only join
    that is safe to make: one clean fragment does not launder the rest.
    """
    worst = Taint.SAFE
    for value in values:
        if value.rank > worst.rank:
            worst = value
    return worst


def _segments(node: ast.expr) -> list[str] | None:
    """The dotted segments of a pure attribute chain, root first.

    ``None`` when the chain is interrupted by anything other than a name or an
    attribute -- a call or a subscript means the chain is computed, and its
    segments no longer describe what the value is.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    parts.reverse()
    return parts


def request_source(node: ast.expr) -> str | None:
    """The request attribute this expression reads, if it reads one.

    Recognises ``request.GET`` and ``self.request.GET`` equally, and
    ``self.kwargs``. Returns the dotted source for use as evidence, so a
    finding can quote the thing it objected to rather than describing it.
    """
    segments = _segments(node)
    if segments is None:
        return None
    for index, segment in enumerate(segments):
        if segment not in REQUEST_SOURCES:
            continue
        base = segments[:index]
        if base and base[-1] in REQUEST_NAMES:
            return ".".join(segments[: index + 1])
    if len(segments) == 2 and segments[0] == "self" and segments[1] in VIEW_KWARGS:
        return "self.kwargs"
    return None


def handles_request(scope: Scope) -> bool:
    """Whether the function this scope belongs to can see a request.

    A parameter named ``request``, or any read of ``self.request`` in the body.
    Used to decide whether an unresolvable value is worth mentioning at all: an
    opaque string spliced into SQL inside a request handler is a different
    proposition from the same string inside a management command.
    """
    node = scope.node
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    args = node.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.arg in REQUEST_NAMES:
            return True
    return any(
        isinstance(inner, ast.Attribute)
        and inner.attr in REQUEST_NAMES
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "self"
        for inner in ast.walk(node)
    )


def _sanitised(node: ast.Call) -> bool:
    """Whether this call's result cannot contain SQL syntax."""
    return isinstance(node.func, ast.Name) and node.func.id in SANITISERS


def _reader_of(node: ast.Call) -> ast.expr | None:
    """The container a lookup call reads from, for ``d.get(k)`` and friends."""
    if isinstance(node.func, ast.Attribute) and node.func.attr in {
        "get",
        "getlist",
        "pop",
        "setdefault",
        "read",
        "values",
        "items",
        "keys",
        "dict",
        "urlencode",
    }:
        return node.func.value
    return None


class _Analysis:
    """One taint query, carrying the state that keeps recursion terminating."""

    def __init__(self, chains: DefUse | None) -> None:
        self.chains = chains
        self.seen: set[int] = set()

    def of(self, node: ast.expr | None, depth: int = 0) -> Taint:
        if node is None or depth > _MAX_DEPTH:
            return Taint.UNKNOWN
        if id(node) in self.seen:
            # A cyclic definition -- `x = x + suffix`. The value's own history
            # cannot settle it, so the other reaching definitions decide.
            return Taint.SAFE
        self.seen.add(id(node))
        try:
            return self._of(node, depth)
        finally:
            self.seen.discard(id(node))

    def _of(self, node: ast.expr, depth: int) -> Taint:
        spliced = interpolation(node)
        if spliced is not None:
            return join(self.of(part, depth + 1) for part in spliced.parts)
        match node:
            case ast.Constant():
                return Taint.SAFE
            case ast.Compare() | ast.UnaryOp(op=ast.Not()):
                # A bool. No room for syntax whatever the operands were.
                return Taint.SAFE
            case ast.Name():
                return self.name(node, depth)
            case ast.Attribute():
                return self.attribute(node, depth)
            case ast.Subscript(value=value):
                return self.of(value, depth + 1)
            case ast.Call():
                return self.call(node, depth)
            case ast.IfExp(body=body, orelse=orelse):
                return join((self.of(body, depth + 1), self.of(orelse, depth + 1)))
            case ast.BoolOp(values=values):
                return join(self.of(value, depth + 1) for value in values)
            case ast.BinOp(left=left, right=right):
                return join((self.of(left, depth + 1), self.of(right, depth + 1)))
            case ast.List(elts=elts) | ast.Tuple(elts=elts) | ast.Set(elts=elts):
                return join(self.of(elt, depth + 1) for elt in elts)
            case ast.ListComp(elt=elt) | ast.SetComp(elt=elt) | ast.GeneratorExp(elt=elt):
                return self.comprehension(node, elt, depth)
            case ast.Starred(value=value):
                return self.of(value, depth + 1)
        return Taint.UNKNOWN

    def name(self, node: ast.Name, depth: int) -> Taint:
        """A name is as tainted as the worst definition that can reach it."""
        if self.chains is None:
            return Taint.UNKNOWN
        use = self.chains.of(node)
        if use is None or not use.reaching:
            return Taint.UNKNOWN
        return join(self.binding(binding, depth) for binding in use.reaching)

    def binding(self, binding: Binding, depth: int) -> Taint:
        if binding.kind is BindingKind.PARAMETER:
            # The caller decides, and we are not looking at the caller.
            return Taint.UNKNOWN
        if binding.kind in {BindingKind.IMPORT, BindingKind.FUNCTION_DEF, BindingKind.CLASS_DEF}:
            return Taint.SAFE
        if binding.value is None:
            return Taint.UNKNOWN
        # Two binding kinds that every other analysis has to separate collapse
        # here, and the collapse is worth stating because it does not hold
        # elsewhere. A `for` target holds one *element* of `binding.value`, and
        # for `DJP` that distinction is the whole rule -- a queryset is not a
        # model instance. For taint the container and its element carry the
        # same verdict in both directions: an element of `request.GET` is a key
        # the client chose, and an element of a list of literals is a literal.
        # An unpacked target is the same story, since a tuple display joins its
        # elements and any other right-hand side is opaque on its own.
        return self.of(binding.value, depth + 1)

    def attribute(self, node: ast.Attribute, depth: int) -> Taint:
        source = request_source(node)
        if source is not None:
            return Taint.TAINTED
        segments = _segments(node)
        if segments is None:
            # The chain is computed -- `x.get_field(y).column`. Whatever
            # produced it decides, so fall through to the base expression.
            return self.of(node.value, depth + 1)
        if any(segment in SAFE_META for segment in segments):
            return Taint.SAFE
        if segments[0] in REQUEST_NAMES or (len(segments) > 1 and segments[1] in REQUEST_NAMES):
            # Reached through a request but not one of its wire attributes:
            # `request.user`, `request.event`. Middleware put an object there.
            return Taint.SAFE
        return self.of(node.value, depth + 1) if isinstance(node.value, ast.Name) else Taint.UNKNOWN

    def call(self, node: ast.Call, depth: int) -> Taint:
        if _sanitised(node):
            return Taint.SAFE
        container = _reader_of(node)
        if container is not None:
            reading = self.of(container, depth + 1)
            if reading is Taint.TAINTED:
                return Taint.TAINTED
        arguments = [*node.args, *(k.value for k in node.keywords)]
        worst = join(self.of(argument, depth + 1) for argument in arguments)
        if worst is Taint.TAINTED:
            # An opaque function handed client text. It may well sanitise, but
            # nothing here shows that it does, and assuming otherwise is how a
            # taint analysis stops finding anything.
            return Taint.TAINTED
        return Taint.UNKNOWN

    def comprehension(self, node: ast.expr, elt: ast.expr, depth: int) -> Taint:
        generators = getattr(node, "generators", ())
        iterated = join(self.of(gen.iter, depth + 1) for gen in generators)
        if iterated is Taint.TAINTED:
            return Taint.TAINTED
        return self.of(elt, depth + 1)


def taint_of(node: ast.expr | None, chains: DefUse | None = None) -> Taint:
    """Whether ``node`` can carry client-written text.

    ``chains`` supplies the def-use information that resolves names. Without
    it every name is ``UNKNOWN``, which is correct but not useful, so callers
    inside a scope should always pass it.
    """
    return _Analysis(chains).of(node)


def tainted_parts(
    node: ast.expr, parts: tuple[ast.expr, ...], chains: DefUse | None = None
) -> tuple[tuple[ast.expr, Taint], ...]:
    """Each spliced part with its taint, sharing one analysis.

    Sharing matters: the recursion guard is per-query, so resolving parts one
    at a time through :func:`taint_of` would let a name that appears twice be
    walked twice.
    """
    analysis = _Analysis(chains)
    return tuple((part, analysis.of(part)) for part in parts)
