"""Partial evaluation of Django settings expressions.

Settings modules are Python, so a rule that only understands ``DEBUG = True``
understands almost no real project. The forms that actually appear -- environment
lookups with defaults, helper functions, string building, conditional
assignment -- have to be resolved before any settings rule can say anything
useful. That is this module's job: walk an expression and return a
:class:`~djaudit.values.Value` describing what it can be.

It is a partial evaluator, not an interpreter. It never imports the target, never
calls its code, and answers ``UNKNOWN`` for anything it does not model. Being
wrong is far more expensive than being silent: an over-eager guess becomes a
false positive in somebody's CI, while an ``UNKNOWN`` merely costs a rule its
confidence.

Two safety properties matter as much as the results, because this runs against
arbitrary untrusted source:

* a node and depth budget, so a deeply nested or enormous expression cannot hang
  the run
* a size ceiling on constructed strings and containers, so ``"x" * 10**9`` does
  not exhaust memory before the node budget ever notices
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from djaudit.values import Value

MAX_NODES = 10_000
MAX_DEPTH = 40
MAX_SIZE = 100_000
"""Ceiling on a constructed string or container.

Real settings values are tiny. Anything larger is either generated data we have
no reason to reason about, or an attempt to make us allocate.
"""

MAX_INT_BITS = 4096
"""Ceiling on the magnitude of any integer we compute with.

Roughly 1,233 decimal digits -- orders of magnitude beyond any real setting.
Without it, `10 ** 10 ** 8` asks CPython to build a hundred-million-digit
integer, which neither the node budget nor the size ceiling can prevent: it is
a single BinOp producing a single value, and the cost is paid inside one
CPython call that never yields back to us.
"""


class _BudgetError(Exception):
    """Internal. Unwinds to the public entry point, which returns UNKNOWN."""


@dataclass(slots=True)
class Budget:
    max_nodes: int = MAX_NODES
    max_depth: int = MAX_DEPTH
    spent: int = 0

    def charge(self) -> None:
        self.spent += 1
        if self.spent > self.max_nodes:
            raise _BudgetError("node budget exhausted")


@dataclass(slots=True)
class Scope:
    """Names visible to an expression.

    ``names`` holds already-evaluated bindings. ``functions`` holds local
    definitions the evaluator may follow, which is what lets a project's own
    ``envbool``-style helper resolve.
    """

    names: dict[str, Value] = field(default_factory=dict)
    functions: dict[str, ast.FunctionDef] = field(default_factory=dict)

    def child(self, names: dict[str, Value]) -> Scope:
        return Scope(names={**self.names, **names}, functions=self.functions)


class Evaluator:
    """Resolves expressions against a :class:`Scope`."""

    def __init__(self, scope: Scope | None = None, budget: Budget | None = None) -> None:
        self.scope = scope or Scope()
        self.budget = budget or Budget()

    def evaluate(self, node: ast.expr) -> Value:
        """Public entry point. Never raises."""
        try:
            return self._eval(node, depth=0)
        except _BudgetError as exc:
            return Value.unknown(str(exc))
        except RecursionError:
            return Value.unknown("expression nested too deeply")

    def _eval(self, node: ast.expr, depth: int) -> Value:
        self.budget.charge()
        if depth > self.budget.max_depth:
            raise _BudgetError("expression nested too deeply")

        handler = getattr(self, f"_eval_{type(node).__name__}", None)
        if handler is None:
            return Value.unknown(f"unsupported expression: {type(node).__name__}")
        result: Value = handler(node, depth + 1)
        return result

    def _eval_all(self, nodes: list[ast.expr], depth: int) -> list[Value]:
        return [self._eval(node, depth) for node in nodes]

    # -- literals and names -------------------------------------------------

    def _eval_Constant(self, node: ast.Constant, depth: int) -> Value:  # noqa: N802
        return Value.of(node.value)

    def _eval_Name(self, node: ast.Name, depth: int) -> Value:  # noqa: N802
        value = self.scope.names.get(node.id)
        return value if value is not None else Value.unknown(f"undefined name: {node.id}")

    # -- containers ---------------------------------------------------------

    def _eval_List(self, node: ast.List, depth: int) -> Value:  # noqa: N802
        return self._sequence(node.elts, list, depth)

    def _eval_Tuple(self, node: ast.Tuple, depth: int) -> Value:  # noqa: N802
        return self._sequence(node.elts, tuple, depth)

    def _eval_Set(self, node: ast.Set, depth: int) -> Value:  # noqa: N802
        return self._sequence(node.elts, set, depth)

    def _sequence(self, elements: list[ast.expr], build: Any, depth: int) -> Value:
        if len(elements) > MAX_SIZE:
            return Value.unknown("container too large to evaluate")

        items: list[Any] = []
        tainted = False
        for element in elements:
            if isinstance(element, ast.Starred):
                inner = self._eval(element.value, depth)
                if not inner.is_literal or not isinstance(inner.literal, list | tuple | set):
                    return Value.unknown("unresolvable starred element")
                items.extend(inner.literal)
                tainted |= inner.env_dependent
                continue
            value = self._eval(element, depth)
            if not value.is_literal:
                # One unresolvable element makes the whole container
                # unresolvable. Rules ask questions like "is '*' in
                # ALLOWED_HOSTS", and a partial list would answer "no" when the
                # truth is "we cannot see".
                return Value.unknown(f"unresolvable element: {value.reason or value.kind}")
            items.append(value.literal)
            tainted |= value.env_dependent

        try:
            return Value.of(build(items), env_dependent=tainted)
        except TypeError:
            return Value.unknown("container holds unhashable elements")

    def _eval_Dict(self, node: ast.Dict, depth: int) -> Value:  # noqa: N802
        if len(node.keys) > MAX_SIZE:
            return Value.unknown("container too large to evaluate")

        result: dict[Any, Any] = {}
        tainted = False
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                inner = self._eval(value_node, depth)
                if not inner.is_literal or not isinstance(inner.literal, dict):
                    return Value.unknown("unresolvable dict unpacking")
                result.update(inner.literal)
                tainted |= inner.env_dependent
                continue
            key = self._eval(key_node, depth)
            value = self._eval(value_node, depth)
            if not key.is_literal or not value.is_literal:
                return Value.unknown("unresolvable dict entry")
            try:
                result[key.literal] = value.literal
            except TypeError:
                return Value.unknown("unhashable dict key")
            tainted |= key.env_dependent or value.env_dependent

        return Value.of(result, env_dependent=tainted)

    # -- strings ------------------------------------------------------------

    def _eval_JoinedStr(self, node: ast.JoinedStr, depth: int) -> Value:  # noqa: N802
        parts: list[str] = []
        tainted = False
        for piece in node.values:
            value = self._eval(piece, depth)
            if not value.is_literal:
                return Value.unknown("f-string has an unresolvable part")
            parts.append(str(value.literal))
            tainted |= value.env_dependent
        return self._sized_string("".join(parts), tainted)

    def _eval_FormattedValue(self, node: ast.FormattedValue, depth: int) -> Value:  # noqa: N802
        value = self._eval(node.value, depth)
        if not value.is_literal:
            return value
        if node.format_spec is not None:
            spec = self._eval(node.format_spec, depth)
            if not spec.is_literal:
                return Value.unknown("unresolvable format spec")
            try:
                return Value.of(
                    format(value.literal, str(spec.literal)), env_dependent=value.env_dependent
                )
            except (ValueError, TypeError):
                return Value.unknown("invalid format spec")
        conversion = _CONVERSIONS.get(node.conversion)
        if conversion is not None:
            return Value.of(conversion(value.literal), env_dependent=value.env_dependent)
        return value

    def _sized_string(self, text: str, tainted: bool) -> Value:
        if len(text) > MAX_SIZE:
            return Value.unknown("string too large to evaluate")
        return Value.of(text, env_dependent=tainted)

    # -- operators ----------------------------------------------------------

    def _eval_BinOp(self, node: ast.BinOp, depth: int) -> Value:  # noqa: N802
        left = self._eval(node.left, depth)
        right = self._eval(node.right, depth)
        if not left.is_literal or not right.is_literal:
            return Value.unknown("unresolvable operand")

        tainted = left.env_dependent or right.env_dependent

        # Checked before evaluating, never after: these operations pay their
        # whole cost inside one CPython call that never yields back to us, so
        # by the time we could inspect a result the damage is done.
        if not self._operands_are_safe(left.literal, right.literal):
            return Value.unknown("operand too large to evaluate")
        if not self._operation_is_safe(node.op, left.literal, right.literal):
            return Value.unknown("result too large to evaluate")

        operation = _BINARY_OPS.get(type(node.op))
        if operation is None:
            return Value.unknown(f"unsupported operator: {type(node.op).__name__}")

        try:
            result = operation(left.literal, right.literal)
        except Exception:  # target code: any failure means 'we cannot tell'
            return Value.unknown("operation failed")

        if isinstance(result, str | bytes | list | tuple | set | dict) and len(result) > MAX_SIZE:
            return Value.unknown("result too large to evaluate")
        if (
            isinstance(result, int)
            and not isinstance(result, bool)
            and result.bit_length() > MAX_INT_BITS
        ):
            return Value.unknown("result too large to evaluate")
        return Value.of(result, env_dependent=tainted)

    @staticmethod
    def _operands_are_safe(*operands: Any) -> bool:
        """Refuse integers already too large to compute with.

        A settings file can contain a literal with a hundred thousand digits.
        """
        return all(
            not (isinstance(operand, int) and not isinstance(operand, bool))
            or operand.bit_length() <= MAX_INT_BITS
            for operand in operands
        )

    @classmethod
    def _operation_is_safe(cls, op: ast.operator, left: Any, right: Any) -> bool:
        if isinstance(op, ast.Mult):
            return cls._multiplication_is_safe(left, right)
        if isinstance(op, ast.Pow):
            return cls._power_is_safe(left, right)
        return True

    @staticmethod
    def _power_is_safe(base: Any, exponent: Any) -> bool:
        if not isinstance(base, int) or not isinstance(exponent, int):
            return True
        if isinstance(base, bool) or isinstance(exponent, bool) or exponent <= 0:
            return True
        # Estimated rather than computed: the estimate is the only number we can
        # afford, since computing the real one is exactly what we are avoiding.
        return base.bit_length() * exponent <= MAX_INT_BITS

    @staticmethod
    def _multiplication_is_safe(left: Any, right: Any) -> bool:
        for sequence, count in ((left, right), (right, left)):
            if (
                isinstance(sequence, str | bytes | list | tuple)
                and isinstance(count, int)
                and len(sequence) * max(count, 0) > MAX_SIZE
            ):
                return False
        if isinstance(left, int) and isinstance(right, int):
            return left.bit_length() + right.bit_length() <= MAX_INT_BITS
        return True

    def _eval_UnaryOp(self, node: ast.UnaryOp, depth: int) -> Value:  # noqa: N802
        operand = self._eval(node.operand, depth)
        operation = _UNARY_OPS.get(type(node.op))
        if operation is None:
            return Value.unknown(f"unsupported unary operator: {type(node.op).__name__}")
        if not operand.is_literal:
            return Value.unknown("unresolvable operand")
        try:
            return Value.of(operation(operand.literal), env_dependent=operand.env_dependent)
        except Exception:  # target code: any failure means 'we cannot tell'
            return Value.unknown("operation failed")

    # -- calls --------------------------------------------------------------

    def _eval_Call(self, node: ast.Call, depth: int) -> Value:  # noqa: N802
        if isinstance(node.func, ast.Attribute):
            return self._eval_method_call(node, node.func, depth)
        if isinstance(node.func, ast.Name) and node.func.id in _SAFE_BUILTINS:
            return self._eval_builtin(node, node.func.id, depth)
        return Value.unknown("unsupported call")

    def _eval_builtin(self, node: ast.Call, name: str, depth: int) -> Value:
        if node.keywords:
            return Value.unknown(f"unsupported keyword arguments to {name}()")
        args = self._eval_all(node.args, depth)
        if not all(arg.is_literal for arg in args):
            return Value.unknown(f"unresolvable argument to {name}()")
        try:
            result = _SAFE_BUILTINS[name](*[arg.literal for arg in args])
        except Exception:  # target code: any failure means 'we cannot tell'
            return Value.unknown(f"{name}() failed")
        return Value.of(result, env_dependent=any(arg.env_dependent for arg in args))

    def _eval_method_call(self, node: ast.Call, func: ast.Attribute, depth: int) -> Value:
        if func.attr not in _SAFE_STR_METHODS:
            return Value.unknown(f"unsupported method: {func.attr}")

        receiver = self._eval(func.value, depth)
        if not receiver.is_literal:
            return Value.unknown("unresolvable receiver")
        if not isinstance(receiver.literal, str | list | tuple):
            return Value.unknown("method on an unsupported type")

        args = self._eval_all(node.args, depth)
        kwargs = {kw.arg: self._eval(kw.value, depth) for kw in node.keywords if kw.arg}
        if not all(a.is_literal for a in args) or not all(v.is_literal for v in kwargs.values()):
            return Value.unknown("unresolvable method argument")

        try:
            method = getattr(receiver.literal, func.attr)
            result = method(*[a.literal for a in args], **{k: v.literal for k, v in kwargs.items()})
        except Exception:  # target code: any failure means 'we cannot tell'
            return Value.unknown(f"{func.attr}() failed")

        if isinstance(result, str) and len(result) > MAX_SIZE:
            return Value.unknown("result too large to evaluate")
        tainted = receiver.env_dependent or any(a.env_dependent for a in args)
        tainted = tainted or any(v.env_dependent for v in kwargs.values())
        return Value.of(result, env_dependent=tainted)


_BINARY_OPS: dict[type, Any] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
    ast.BitOr: lambda a, b: a | b,
    ast.BitAnd: lambda a, b: a & b,
}

_CONVERSIONS: dict[int, Callable[[Any], str]] = {
    ord("r"): repr,
    ord("s"): str,
    ord("a"): ascii,
}

_UNARY_OPS: dict[type, Any] = {
    ast.Not: lambda a: not a,
    ast.USub: lambda a: -a,
    ast.UAdd: lambda a: +a,
    ast.Invert: lambda a: ~a,
}

_SAFE_BUILTINS: dict[str, Any] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "tuple": tuple,
    "set": set,
    "len": len,
    "sorted": sorted,
}

_SAFE_STR_METHODS = frozenset(
    {
        "format",
        "join",
        "split",
        "rsplit",
        "strip",
        "lstrip",
        "rstrip",
        "lower",
        "upper",
        "replace",
        "startswith",
        "endswith",
        "removeprefix",
        "removesuffix",
    }
)


def evaluate(node: ast.expr, scope: Scope | None = None) -> Value:
    """Convenience wrapper for a one-off evaluation."""
    return Evaluator(scope).evaluate(node)
