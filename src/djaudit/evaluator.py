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
from dataclasses import dataclass, field, replace
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
MAX_CALL_DEPTH = 4
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
    ``envbool``-style helper resolve. ``imports`` maps a local name to the
    dotted path it came from, so ``from os import environ`` and ``import os``
    are recognised as the same thing rather than needing two patterns each.
    """

    names: dict[str, Value] = field(default_factory=dict)
    functions: dict[str, ast.FunctionDef] = field(default_factory=dict)
    imports: dict[str, str] = field(default_factory=dict)
    env_objects: dict[str, dict[str, ast.expr]] = field(default_factory=dict)

    def child(self, names: dict[str, Value]) -> Scope:
        return Scope(
            names={**self.names, **names},
            functions=self.functions,
            imports=self.imports,
            env_objects=self.env_objects,
        )

    def origin(self, node: ast.expr) -> str | None:
        """Dotted path an expression refers to, following imports.

        ``os.environ.get`` and a module that did ``from os import environ``
        both resolve to ``os.environ.get``. Returns None for anything that is
        not a plain attribute chain rooted in a known import.
        """
        if isinstance(node, ast.Name):
            return self.imports.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.origin(node.value)
            return f"{base}.{node.attr}" if base else None
        return None


def collect_imports(module: ast.Module) -> dict[str, str]:
    """Map every locally bound import name to its dotted origin."""
    imports: dict[str, str] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return imports


def collect_functions(module: ast.Module) -> dict[str, ast.FunctionDef]:
    """Module-level function definitions the evaluator may follow."""
    return {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}


def collect_env_objects(
    module: ast.Module, imports: dict[str, str]
) -> dict[str, dict[str, ast.expr]]:
    """Locate ``env = environ.Env(DEBUG=(bool, False))`` bindings and their schema.

    The schema matters because ``env("DEBUG")`` with no inline default falls
    back to it, so without this the most common django-environ idiom resolves
    to nothing.
    """
    scope = Scope(imports=imports)
    found: dict[str, dict[str, ast.expr]] = {}

    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        if scope.origin(value.func) not in _ENV_CLASS_PATHS:
            continue

        schema: dict[str, ast.expr] = {}
        for keyword in value.keywords:
            # Env(DEBUG=(bool, False)) -- the second element is the default.
            if (
                keyword.arg
                and isinstance(keyword.value, ast.Tuple)
                and len(keyword.value.elts) == 2
            ):
                schema[keyword.arg] = keyword.value.elts[1]
        found[target.id] = schema

    return found


class _UnresolvableError(Exception):
    """Raised inside a followed function body when execution cannot continue."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Evaluator:
    """Resolves expressions against a :class:`Scope`."""

    def __init__(self, scope: Scope | None = None, budget: Budget | None = None) -> None:
        self.scope = scope or Scope()
        self.budget = budget or Budget()
        self._calls: list[str] = []

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

    # -- environment --------------------------------------------------------

    def _eval_Subscript(self, node: ast.Subscript, depth: int) -> Value:  # noqa: N802
        if self.scope.origin(node.value) in _ENVIRON_PATHS:
            # os.environ["DEBUG"] with no default. The deployment we care about
            # is the one where nobody set it, and there it raises KeyError --
            # so there is no value to reason about, only a dependency to record.
            return Value.unknown("environment variable with no default", env_dependent=True)

        container = self._eval(node.value, depth)
        index = self._eval(node.slice, depth)
        if not container.is_literal or not index.is_literal:
            return Value.unknown("unresolvable subscript")
        try:
            result = container.literal[index.literal]
        except Exception:  # target code: any failure means 'we cannot tell'
            return Value.unknown("subscript failed")
        return Value.of(result, env_dependent=container.env_dependent or index.env_dependent)

    def _environment_lookup(self, node: ast.Call, depth: int) -> Value:
        """Resolve an environment read to its default.

        Deliberately the default rather than UNKNOWN. `os.environ.get("DEBUG",
        "True")` is concretely "True" on any machine where nobody set the
        variable, which is exactly the deployment worth warning about; treating
        it as unknowable would silence every rule on the case that matters.

        The env_dependent taint carries the caveat, so rules report it at lower
        confidence rather than as fact.
        """
        default: Value | None = None
        if len(node.args) > 1:
            default = self._eval(node.args[1], depth)
        for keyword in node.keywords:
            if keyword.arg == "default":
                default = self._eval(keyword.value, depth)

        if default is None:
            # `os.getenv("X")` and `os.environ.get("X")` return None when unset.
            # None is the default, not the absence of one -- and a rule asking
            # "is SECRET_KEY unset" needs that answer, not a shrug. Twenty-five
            # settings in healthchecks alone take this form.
            return Value.of(None, env_dependent=True)
        if not default.is_literal:
            return Value.unknown("unresolvable environment default", env_dependent=True)
        return Value.of(default.literal, env_dependent=True)

    def _eval_NamedExpr(self, node: ast.NamedExpr, depth: int) -> Value:  # noqa: N802
        value = self._eval(node.value, depth)
        if isinstance(node.target, ast.Name):
            self.scope.names[node.target.id] = value
        return value

    # -- conditionals -------------------------------------------------------

    def _eval_IfExp(self, node: ast.IfExp, depth: int) -> Value:  # noqa: N802
        test = self._eval(node.test, depth)
        if test.is_literal:
            taken = node.body if test.literal else node.orelse
            # The test being environment-dependent taints the outcome even
            # though the branch itself may be a plain literal: which branch we
            # took is the part that depended on the environment.
            return _taint(self._eval(taken, depth), test.env_dependent)
        return Value.conditional([self._eval(node.body, depth), self._eval(node.orelse, depth)])

    def _eval_BoolOp(self, node: ast.BoolOp, depth: int) -> Value:  # noqa: N802
        """``and``/``or``, which in Python yield an operand rather than a bool."""
        is_and = isinstance(node.op, ast.And)
        outcomes: list[Value] = []
        # An operand that short-circuits away still decided the outcome, so its
        # taint has to survive being discarded.
        tainted = False

        for index, operand_node in enumerate(node.values):
            operand = self._eval(operand_node, depth)
            tainted |= operand.env_dependent
            last = index == len(node.values) - 1

            if not operand.is_literal:
                # Cannot tell whether evaluation short-circuits here, so every
                # remaining operand stays possible.
                outcomes.append(operand)
                outcomes.extend(self._eval(rest, depth) for rest in node.values[index + 1 :])
                return _taint(Value.conditional(outcomes), tainted)

            short_circuits = (not operand.literal) if is_and else bool(operand.literal)
            if short_circuits or last:
                outcomes.append(operand)
                return _taint(Value.conditional(outcomes), tainted)

        return Value.unknown("empty boolean expression")

    def _eval_Compare(self, node: ast.Compare, depth: int) -> Value:  # noqa: N802
        operands = [self._eval(node.left, depth)]
        operands.extend(self._eval_all(list(node.comparators), depth))
        if not all(operand.is_literal for operand in operands):
            return Value.unknown("unresolvable comparison operand")

        tainted = any(operand.env_dependent for operand in operands)
        literals = [operand.literal for operand in operands]

        result = True
        for index, op in enumerate(node.ops):
            comparison = _COMPARE_OPS.get(type(op))
            if comparison is None:
                return Value.unknown(f"unsupported comparison: {type(op).__name__}")
            try:
                if not comparison(literals[index], literals[index + 1]):
                    result = False
                    break
            except Exception:  # target code: any failure means 'we cannot tell'
                return Value.unknown("comparison failed")

        return Value.of(result, env_dependent=tainted)

    # -- comprehensions -----------------------------------------------------

    def _eval_ListComp(self, node: ast.ListComp, depth: int) -> Value:  # noqa: N802
        return self._comprehension(node.generators, node.elt, None, list, depth)

    def _eval_SetComp(self, node: ast.SetComp, depth: int) -> Value:  # noqa: N802
        return self._comprehension(node.generators, node.elt, None, set, depth)

    def _eval_GeneratorExp(self, node: ast.GeneratorExp, depth: int) -> Value:  # noqa: N802
        # A generator is lazy, but every consumer we model materialises it.
        return self._comprehension(node.generators, node.elt, None, list, depth)

    def _eval_DictComp(self, node: ast.DictComp, depth: int) -> Value:  # noqa: N802
        return self._comprehension(node.generators, node.key, node.value, dict, depth)

    def _comprehension(
        self,
        generators: list[ast.comprehension],
        element: ast.expr,
        value_node: ast.expr | None,
        build: Any,
        depth: int,
    ) -> Value:
        if len(generators) != 1:
            return Value.unknown("nested comprehension")

        generator = generators[0]
        if generator.is_async or not isinstance(generator.target, ast.Name):
            return Value.unknown("unsupported comprehension target")

        iterable = self._eval(generator.iter, depth)
        if not iterable.is_literal or not isinstance(
            iterable.literal, list | tuple | set | dict | str
        ):
            return Value.unknown("unresolvable comprehension source")

        items = list(iterable.literal)
        if len(items) > MAX_SIZE:
            return Value.unknown("comprehension source too large")

        outer = self.scope
        collected: list[Any] = []
        tainted = iterable.env_dependent
        try:
            for item in items:
                self.scope = outer.child({generator.target.id: Value.of(item)})

                keep = True
                for condition in generator.ifs:
                    test = self._eval(condition, depth)
                    if not test.is_literal:
                        return Value.unknown("unresolvable comprehension filter")
                    tainted |= test.env_dependent
                    if not test.literal:
                        keep = False
                        break
                if not keep:
                    continue

                evaluated = self._eval(element, depth)
                if not evaluated.is_literal:
                    return Value.unknown("unresolvable comprehension element")
                tainted |= evaluated.env_dependent

                if value_node is None:
                    collected.append(evaluated.literal)
                    continue

                mapped = self._eval(value_node, depth)
                if not mapped.is_literal:
                    return Value.unknown("unresolvable comprehension value")
                tainted |= mapped.env_dependent
                collected.append((evaluated.literal, mapped.literal))
        finally:
            self.scope = outer

        try:
            return Value.of(build(collected), env_dependent=tainted)
        except TypeError:
            return Value.unknown("comprehension produced unhashable items")

    # -- calls --------------------------------------------------------------

    def _eval_Call(self, node: ast.Call, depth: int) -> Value:  # noqa: N802
        path = self.scope.origin(node.func)
        if path in _ENVIRON_GET_PATHS:
            return self._environment_lookup(node, depth)
        if path in _DECOUPLE_PATHS:
            return self._decouple_config(node, depth)

        if isinstance(node.func, ast.Name):
            if node.func.id in self.scope.env_objects:
                return self._environ_instance_call(node, node.func.id, depth)
            if node.func.id in self.scope.functions:
                return self._call_function(self.scope.functions[node.func.id], node, depth)
            if node.func.id in _SAFE_BUILTINS:
                return self._eval_builtin(node, node.func.id, depth)
            return Value.unknown("unsupported call")

        if isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and receiver.id in self.scope.env_objects:
                return self._environ_method(node, node.func.attr, depth)
            return self._eval_method_call(node, node.func, depth)
        return Value.unknown("unsupported call")

    # -- configuration libraries --------------------------------------------

    def _default_argument(self, node: ast.Call, position: int, depth: int) -> Value | None:
        """The ``default`` argument, whether passed by keyword or position."""
        for keyword in node.keywords:
            if keyword.arg == "default":
                return self._eval(keyword.value, depth)
        if len(node.args) > position:
            return self._eval(node.args[position], depth)
        return None

    def _environ_method(self, node: ast.Call, method: str, depth: int) -> Value:
        """``env.bool("DEBUG", False)`` and friends.

        django-environ's signatures are not uniform: ``bool``/``int``/``str``
        take the default second, while ``list``/``tuple``/``dict`` take a cast
        there and the default third. Getting that backwards would read a cast
        as a value.
        """
        if method in _ENVIRON_OPAQUE_METHODS:
            # These parse the default into something else entirely (db() turns
            # a URL into a connection dict), so the literal we can see is not
            # the value the setting ends up with.
            return Value.unknown(f"env.{method}() transforms its default", env_dependent=True)

        position = _ENVIRON_DEFAULT_POSITION.get(method)
        if position is None:
            return Value.unknown(f"unsupported django-environ method: {method}")
        return self._environ_default(self._default_argument(node, position, depth))

    def _environ_instance_call(self, node: ast.Call, name: str, depth: int) -> Value:
        """``env("DEBUG")``, which falls back to the schema given to ``Env()``."""
        default = self._default_argument(node, 2, depth)
        if default is None and node.args:
            variable = self._eval(node.args[0], depth)
            schema = self.scope.env_objects[name]
            if variable.is_literal and isinstance(variable.literal, str):
                declared = schema.get(variable.literal)
                if declared is not None:
                    default = self._eval(declared, depth)
        return self._environ_default(default)

    def _environ_default(self, default: Value | None) -> Value:
        if default is None:
            # django-environ raises ImproperlyConfigured when a variable has no
            # default and is unset, so there is no value to reason about.
            return Value.unknown("environment variable is required", env_dependent=True)
        if not default.is_literal:
            return Value.unknown("unresolvable environment default", env_dependent=True)
        # Deliberately uncast: django-environ returns the default untouched, so
        # env.bool("DEBUG", "True") really is the string "True" at runtime.
        return Value.of(default.literal, env_dependent=True)

    def _decouple_config(self, node: ast.Call, depth: int) -> Value:
        """``config("DEBUG", default=False, cast=bool)``.

        Unlike django-environ, python-decouple *does* apply the cast to the
        default, so the two libraries need different handling.
        """
        default = self._default_argument(node, 1, depth)
        if default is None:
            return Value.unknown("configuration value is required", env_dependent=True)
        if not default.is_literal:
            return Value.unknown("unresolvable configuration default", env_dependent=True)

        cast = self._cast_name(node, depth)
        if cast is None:
            return Value.of(default.literal, env_dependent=True)
        if cast not in _DECOUPLE_CASTS:
            return Value.unknown(f"unsupported cast: {cast}", env_dependent=True)
        try:
            return Value.of(_DECOUPLE_CASTS[cast](default.literal), env_dependent=True)
        except Exception:  # target code: any failure means 'we cannot tell'
            return Value.unknown("configuration cast failed", env_dependent=True)

    def _cast_name(self, node: ast.Call, depth: int) -> str | None:
        for keyword in node.keywords:
            if keyword.arg == "cast":
                return keyword.value.id if isinstance(keyword.value, ast.Name) else "?"
        if len(node.args) > 2:
            return node.args[2].id if isinstance(node.args[2], ast.Name) else "?"
        return None

    # -- following local functions ------------------------------------------

    def _call_function(self, function: ast.FunctionDef, node: ast.Call, depth: int) -> Value:
        """Follow a project's own helper, e.g. healthchecks' ``envbool``.

        Every real settings module wraps environment access in a helper, so a
        resolver that stops at the call boundary sees almost nothing.
        """
        if len(self._calls) >= MAX_CALL_DEPTH:
            return Value.unknown("call nesting too deep")
        if function.name in self._calls:
            return Value.unknown("recursive call")

        bindings = self._bind_arguments(function, node, depth)
        if bindings is None:
            return Value.unknown(f"unresolvable call to {function.name}()")

        outer = self.scope
        self.scope = outer.child(bindings)
        self._calls.append(function.name)
        try:
            returned = self._exec_block(function.body, depth)
        except _UnresolvableError as bail:
            returned = Value.unknown(f"{function.name}(): {bail.reason}")
        finally:
            self.scope = outer
            self._calls.pop()
        # Falling off the end of a function returns None.
        return returned if returned is not None else Value.of(None)

    def _bind_arguments(
        self, function: ast.FunctionDef, node: ast.Call, depth: int
    ) -> dict[str, Value] | None:
        spec = function.args
        if spec.vararg or spec.kwarg or spec.posonlyargs:
            return None

        positional = [argument.arg for argument in spec.args]
        keyword_only = [argument.arg for argument in spec.kwonlyargs]
        if len(node.args) > len(positional):
            return None

        bindings: dict[str, Value] = {}
        for name, argument in zip(positional, node.args, strict=False):
            bindings[name] = self._eval(argument, depth)
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg not in positional + keyword_only:
                return None
            bindings[keyword.arg] = self._eval(keyword.value, depth)

        offset = len(positional) - len(spec.defaults)
        for name, fallback in zip(positional[offset:], spec.defaults, strict=True):
            bindings.setdefault(name, self._eval(fallback, depth))
        for name, optional in zip(keyword_only, spec.kw_defaults, strict=True):
            if optional is not None:
                bindings.setdefault(name, self._eval(optional, depth))

        if not set(positional) | set(keyword_only) <= set(bindings):
            return None
        return bindings

    def _exec_block(self, body: list[ast.stmt], depth: int) -> Value | None:
        """Run statements until one returns. ``None`` means control fell through."""
        tainted = False

        for statement in body:
            self.budget.charge()

            if isinstance(statement, ast.Return):
                if statement.value is None:
                    return _taint(Value.of(None), tainted)
                return _taint(self._eval(statement.value, depth), tainted)

            if isinstance(statement, ast.Assign):
                target = statement.targets[0]
                if len(statement.targets) != 1 or not isinstance(target, ast.Name):
                    raise _UnresolvableError("unsupported assignment")
                self.scope.names[target.id] = self._eval(statement.value, depth)

            elif isinstance(statement, ast.AnnAssign):
                if not isinstance(statement.target, ast.Name) or statement.value is None:
                    raise _UnresolvableError("unsupported annotated assignment")
                self.scope.names[statement.target.id] = self._eval(statement.value, depth)

            elif isinstance(statement, ast.If):
                test = self._eval(statement.test, depth)
                if not test.is_literal:
                    raise _UnresolvableError("unresolvable branch")
                # Which branch ran depended on the environment, so the value
                # produced by either one inherits that.
                tainted |= test.env_dependent
                returned = self._exec_block(
                    statement.body if test.literal else statement.orelse, depth
                )
                if returned is not None:
                    return _taint(returned, tainted)

            elif isinstance(statement, ast.Raise):
                # Reaching a raise means the settings module does not import,
                # so the setting never takes a value at all.
                raise _UnresolvableError("raises")

            elif not isinstance(statement, ast.Pass | ast.Expr):
                raise _UnresolvableError(f"unsupported statement: {type(statement).__name__}")

        return None

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


def _decouple_bool(value: Any) -> bool:
    """python-decouple casts with strtobool, which rejects unknown strings."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
        raise ValueError(f"invalid truth value {value!r}")
    return bool(value)


def _taint(value: Value, tainted: bool) -> Value:
    """Mark ``value`` environment-dependent without disturbing its kind."""
    if not tainted or value.env_dependent:
        return value
    return replace(value, env_dependent=True)


_COMPARE_OPS: dict[type, Any] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}

_ENV_CLASS_PATHS = frozenset({"environ.Env", "environ.FileAwareEnv"})

# Position of the `default` parameter, which django-environ does not keep
# consistent: list/tuple/dict take a `cast` second and the default third.
_ENVIRON_DEFAULT_POSITION = {
    "bool": 1,
    "int": 1,
    "float": 1,
    "str": 1,
    "json": 1,
    "list": 2,
    "tuple": 2,
    "dict": 2,
}

# Methods that parse the default into something else, so the visible literal is
# not the resulting value.
_ENVIRON_OPAQUE_METHODS = frozenset({"db", "db_url", "cache", "cache_url", "url", "path"})

_DECOUPLE_PATHS = frozenset({"decouple.config", "decouple.AutoConfig"})

_TRUE_STRINGS = frozenset({"y", "yes", "t", "true", "on", "1"})
_FALSE_STRINGS = frozenset({"n", "no", "f", "false", "off", "0"})

_DECOUPLE_CASTS: dict[str, Any] = {
    "bool": _decouple_bool,
    "int": int,
    "float": float,
    "str": str,
}

_ENVIRON_PATHS = frozenset({"os.environ", "environ"})
"""Dotted paths denoting the process environment mapping itself."""

_ENVIRON_GET_PATHS = frozenset({"os.environ.get", "environ.get", "os.getenv", "getenv"})
"""Reads of a single environment variable that accept a default."""

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
