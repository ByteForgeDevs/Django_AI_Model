"""How a string was built, for the rules that care what went into it.

Every `DJI` rule about SQL asks the same first question: was this string
*composed*, and if so, out of what? Python offers four ways to splice a value
into a string and Django code uses all of them, so a rule that recognises
f-strings alone will miss three quarters of the shapes it exists to find.
Measured across the three benchmark corpora, the five interpolated
``.execute()`` calls split three f-string, one ``%``, and one concatenation --
a sample too small to rank the shapes, but large enough to show that picking
one would have been wrong.

The distinction this module draws is between the *template* and the *parts*.
``f"SELECT * FROM {table} WHERE id = {pk}"`` is one string with two parts, and
a rule needs the parts, not the string: the literal text between them is the
author's own SQL and is never the defect. Returning the whole node would force
every caller to re-derive that split.

``str.join`` is included, and not for symmetry. It is how placeholder lists are
built -- ``", ".join(["%s"] * len(rows))`` appears verbatim in pretix -- and a
module that did not understand it would report the safest SQL idiom there is.

What this module does **not** do is judge. ``parts`` says what was spliced in;
:mod:`djaudit.dataflow.taint` says whether that was a bad idea. Keeping the two
apart means the shape analysis can be tested against strings that have nothing
to do with SQL.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum


class Composition(StrEnum):
    """The syntax that built a string out of other values."""

    FSTRING = "f-string"
    PERCENT = "%-formatting"
    CONCAT = "concatenation"
    FORMAT = ".format()"
    JOIN = "str.join"


#: How each composition reads in a sentence about the code.
PHRASING: dict[Composition, str] = {
    Composition.FSTRING: "an f-string",
    Composition.PERCENT: "%-formatting",
    Composition.CONCAT: "string concatenation",
    Composition.FORMAT: "str.format()",
    Composition.JOIN: "str.join()",
}


@dataclass(frozen=True, slots=True)
class Interpolation:
    """A string built by splicing values into literal text."""

    kind: Composition
    node: ast.expr
    """The composing expression itself."""

    parts: tuple[ast.expr, ...]
    """The spliced expressions, left to right. Literal text is excluded."""

    @property
    def phrasing(self) -> str:
        return PHRASING[self.kind]


def _percent_parts(right: ast.expr) -> tuple[ast.expr, ...]:
    """The operands of ``template % right``.

    A tuple on the right is the operand list; anything else is a single
    operand, including a dict for ``%(name)s`` formatting, whose values we
    cannot address individually and so take whole.
    """
    if isinstance(right, ast.Tuple):
        return tuple(right.elts)
    return (right,)


def _concat_parts(node: ast.BinOp) -> tuple[ast.expr, ...]:
    """The non-literal operands of a ``+`` chain, flattened.

    ``"a" + b + "c" + d`` parses as nested ``BinOp``s, so recursing is what
    turns four nodes into the two parts a caller wants. Constants are dropped
    because they are the author's own text.
    """
    found: list[ast.expr] = []
    for side in (node.left, node.right):
        if isinstance(side, ast.BinOp) and isinstance(side.op, ast.Add):
            found.extend(_concat_parts(side))
        elif not isinstance(side, ast.Constant):
            found.append(side)
    return tuple(found)


def _joined_parts(node: ast.Call) -> tuple[ast.expr, ...]:
    """What ``sep.join(x)`` will concatenate.

    A list or tuple displayed inline yields its elements, so
    ``", ".join(["%s"] * n)`` is seen for what it is -- placeholders -- rather
    than as an opaque call. A comprehension yields the element expression,
    which is the thing repeated. Anything else yields the argument whole.
    """
    if len(node.args) != 1:
        return ()
    arg = node.args[0]
    if isinstance(arg, ast.List | ast.Tuple | ast.Set):
        return tuple(arg.elts)
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mult):
        # ["%s"] * n -- the repeated element decides, the count cannot appear
        # in the output at all.
        for side in (arg.left, arg.right):
            if isinstance(side, ast.List | ast.Tuple):
                return tuple(side.elts)
        return (arg,)
    if isinstance(arg, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        return (arg.elt,)
    return (arg,)


def interpolation(node: ast.expr) -> Interpolation | None:
    """How ``node`` was composed, or ``None`` if it was not.

    An f-string with no replacement field is not an interpolation -- Python
    parses ``f"SELECT 1"`` as a ``JoinedStr``, and reporting it would flag a
    constant for the way its author chose to quote it.
    """
    match node:
        case ast.JoinedStr(values=values):
            spliced = tuple(v.value for v in values if isinstance(v, ast.FormattedValue))
            if not spliced:
                return None
            return Interpolation(Composition.FSTRING, node, spliced)
        case ast.BinOp(op=ast.Mod(), right=right):
            return Interpolation(Composition.PERCENT, node, _percent_parts(right))
        case ast.BinOp(op=ast.Add()):
            parts = _concat_parts(node)
            if not parts:
                return None
            return Interpolation(Composition.CONCAT, node, parts)
        case ast.Call(func=ast.Attribute(attr="format"), args=args, keywords=keywords):
            spliced = (*args, *(k.value for k in keywords))
            if not spliced:
                return None
            return Interpolation(Composition.FORMAT, node, spliced)
        case ast.Call(func=ast.Attribute(attr="join")):
            parts = _joined_parts(node)
            if not parts:
                return None
            return Interpolation(Composition.JOIN, node, parts)
    return None
