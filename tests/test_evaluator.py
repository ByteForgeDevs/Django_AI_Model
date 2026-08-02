"""Tests for the settings expression evaluator.

Every expression here is written the way it appears in a real settings module.
The evaluator's value is entirely in how much real code it can resolve, so
tests built on synthetic shapes would prove nothing.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.evaluator import MAX_SIZE, Budget, Evaluator, Scope, evaluate
from djaudit.values import Value


def value_of(source: str, scope: Scope | None = None) -> Value:
    """Evaluate a single expression written as source."""
    node = ast.parse(source, mode="eval").body
    return evaluate(node, scope)


def literal(source: str, scope: Scope | None = None):
    result = value_of(source, scope)
    assert result.is_literal, f"expected a literal, got {result.describe()}"
    return result.literal


class TestLiterals:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("True", True),
            ("False", False),
            ("None", None),
            ("42", 42),
            ("3.5", 3.5),
            ("'abc'", "abc"),
            ("b'abc'", b"abc"),
        ],
    )
    def test_constants(self, source, expected) -> None:
        assert literal(source) == expected

    def test_booleans_keep_their_type(self) -> None:
        # Distinguishing True from 1 is the whole point of Value._same.
        assert value_of("True").is_always(True)
        assert not value_of("1").is_always(True)


class TestContainers:
    def test_list(self) -> None:
        assert literal("['a', 'b']") == ["a", "b"]

    def test_tuple(self) -> None:
        assert literal("('a', 'b')") == ("a", "b")

    def test_set(self) -> None:
        assert literal("{'a', 'b'}") == {"a", "b"}

    def test_dict(self) -> None:
        assert literal("{'ENGINE': 'django.db.backends.postgresql', 'PORT': 5432}") == {
            "ENGINE": "django.db.backends.postgresql",
            "PORT": 5432,
        }

    def test_nested(self) -> None:
        assert literal("{'default': {'HOSTS': ['a', 'b']}}") == {"default": {"HOSTS": ["a", "b"]}}

    def test_one_unresolvable_element_makes_the_container_unknown(self) -> None:
        # A partial list would answer "is '*' in ALLOWED_HOSTS" with "no" when
        # the honest answer is "we cannot see".
        assert value_of("['localhost', mystery]").is_unknown

    def test_starred_element_is_expanded(self) -> None:
        scope = Scope(names={"BASE": Value.of(["a", "b"])})
        assert literal("[*BASE, 'c']", scope) == ["a", "b", "c"]

    def test_dict_unpacking_is_expanded(self) -> None:
        scope = Scope(names={"BASE": Value.of({"a": 1})})
        assert literal("{**BASE, 'b': 2}", scope) == {"a": 1, "b": 2}

    def test_unhashable_set_element_is_unknown_not_a_crash(self) -> None:
        assert value_of("{['a']}").is_unknown


class TestNames:
    def test_a_bound_name_resolves(self) -> None:
        scope = Scope(names={"BASE_DIR": Value.of("/srv/app")})
        assert literal("BASE_DIR", scope) == "/srv/app"

    def test_an_unbound_name_is_unknown(self) -> None:
        assert value_of("MYSTERY").is_unknown
        assert "MYSTERY" in value_of("MYSTERY").reason


class TestStrings:
    def test_concatenation(self) -> None:
        assert literal("'django.' + 'contrib'") == "django.contrib"

    def test_implicit_concatenation_of_adjacent_literals(self) -> None:
        assert literal("'a' 'b'") == "ab"

    def test_percent_formatting(self) -> None:
        assert literal("'%s://%s' % ('https', 'example.com')") == "https://example.com"

    def test_format_method(self) -> None:
        assert literal("'{}:{}'.format('localhost', 5432)") == "localhost:5432"

    def test_f_string(self) -> None:
        scope = Scope(names={"HOST": Value.of("db.internal")})
        assert literal("f'postgres://{HOST}:5432'", scope) == "postgres://db.internal:5432"

    def test_f_string_with_an_unresolvable_part(self) -> None:
        assert value_of("f'postgres://{MYSTERY}'").is_unknown

    def test_f_string_format_spec(self) -> None:
        assert literal("f'{3.14159:.2f}'") == "3.14"

    def test_join(self) -> None:
        assert literal("','.join(['a', 'b'])") == "a,b"

    def test_path_style_concatenation(self) -> None:
        scope = Scope(names={"BASE_DIR": Value.of("/srv")})
        assert literal("BASE_DIR + '/static'", scope) == "/srv/static"

    def test_chained_methods(self) -> None:
        assert literal("'  A,B '.strip().lower().split(',')") == ["a", "b"]


class TestBuiltins:
    def test_int(self) -> None:
        assert literal("int('5432')") == 5432

    def test_bool_of_empty_string(self) -> None:
        assert literal("bool('')") is False

    def test_list_of_tuple(self) -> None:
        assert literal("list(('a', 'b'))") == ["a", "b"]

    def test_an_unmodelled_builtin_is_unknown(self) -> None:
        assert value_of("open('/etc/passwd')").is_unknown

    def test_a_failing_call_is_unknown_not_a_crash(self) -> None:
        assert value_of("int('not a number')").is_unknown


class TestOperators:
    def test_arithmetic(self) -> None:
        assert literal("60 * 60 * 24") == 86400

    def test_not(self) -> None:
        assert literal("not True") is False

    def test_unsupported_operator_is_unknown(self) -> None:
        assert value_of("1 << 2").is_unknown

    def test_division_by_zero_is_unknown_not_a_crash(self) -> None:
        assert value_of("1 / 0").is_unknown


class TestEnvironmentTaint:
    def test_taint_propagates_through_concatenation(self) -> None:
        scope = Scope(names={"HOST": Value.of("example.com", env_dependent=True)})
        assert value_of("'https://' + HOST", scope).env_dependent

    def test_taint_propagates_into_a_container(self) -> None:
        scope = Scope(names={"HOST": Value.of("example.com", env_dependent=True)})
        assert value_of("[HOST, 'localhost']", scope).env_dependent

    def test_taint_propagates_through_an_f_string(self) -> None:
        scope = Scope(names={"HOST": Value.of("h", env_dependent=True)})
        assert value_of("f'{HOST}/api'", scope).env_dependent


class TestBudget:
    """A settings file is attacker-controlled input as far as we are concerned."""

    def test_deep_nesting_terminates(self) -> None:
        source = "[" * 200 + "]" * 200
        assert value_of(source).is_unknown

    def test_node_budget_terminates_a_wide_expression(self) -> None:
        # Wide, not deep: a flat container exhausts the node budget while
        # staying well inside the depth limit, so this exercises a different
        # guard from the nesting test above.
        node = ast.parse("[" + ",".join(["1"] * 500) + "]", mode="eval").body
        result = Evaluator(budget=Budget(max_nodes=50)).evaluate(node)
        assert result.is_unknown
        assert "budget" in result.reason

    def test_a_long_chain_of_operators_terminates_on_depth(self) -> None:
        # `1+1+1+...` parses as a left-nested BinOp tree, so depth catches it.
        node = ast.parse("+".join(["1"] * 500), mode="eval").body
        result = Evaluator(budget=Budget(max_nodes=100_000)).evaluate(node)
        assert result.is_unknown
        assert "deeply" in result.reason

    def test_string_multiplication_cannot_allocate(self) -> None:
        # Evaluating this for real would allocate a gigabyte before any node
        # budget noticed, because it is a single BinOp.
        result = value_of("'x' * 1000000000")
        assert result.is_unknown
        assert "too large" in result.reason

    def test_list_multiplication_cannot_allocate(self) -> None:
        assert value_of("[0] * 1000000000").is_unknown

    def test_exponentiation_result_is_bounded(self) -> None:
        assert value_of("'ab' * 10").is_literal

    def test_integer_exponentiation_cannot_allocate(self) -> None:
        # `10 ** 10 ** 8` builds a hundred-million-digit integer inside a single
        # CPython call. Neither the node budget nor the size ceiling can see it:
        # one BinOp, one value, and control never returns to us until it is done.
        result = value_of("10 ** 10 ** 8")
        assert result.is_unknown
        assert "too large" in result.reason

    def test_nested_exponentiation_cannot_allocate(self) -> None:
        assert value_of("2 ** 2 ** 2 ** 40").is_unknown

    def test_integer_multiplication_of_huge_operands_is_refused(self) -> None:
        assert value_of("10**2000 * 10**2000").is_unknown

    def test_ordinary_exponentiation_still_resolves(self) -> None:
        assert literal("2 ** 16") == 65536

    def test_ordinary_arithmetic_still_resolves(self) -> None:
        # The shape a real SESSION_COOKIE_AGE takes.
        assert literal("60 * 60 * 24 * 7") == 604800

    def test_a_large_but_legitimate_value_still_resolves(self) -> None:
        assert literal("'x' * 100") == "x" * 100

    def test_oversized_string_concatenation_is_refused(self) -> None:
        scope = Scope(names={"BIG": Value.of("x" * (MAX_SIZE - 1))})
        assert value_of("BIG + BIG", scope).is_unknown

    def test_budget_is_shared_across_one_evaluation(self) -> None:
        evaluator = Evaluator(budget=Budget(max_nodes=10))
        node = ast.parse("[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]", mode="eval").body
        assert evaluator.evaluate(node).is_unknown


class TestUnsupported:
    def test_an_unmodelled_node_is_unknown_with_its_type(self) -> None:
        result = value_of("lambda: 1")
        assert result.is_unknown
        assert "Lambda" in result.reason

    def test_evaluation_never_raises(self) -> None:
        for source in ["await x", "yield x", "x := 1", "[i for i in range(3)]"]:
            try:
                node = ast.parse(f"({source})", mode="eval").body
            except SyntaxError:
                continue
            assert isinstance(evaluate(node), Value)
