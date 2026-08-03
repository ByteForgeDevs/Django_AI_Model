"""Tests for the settings expression evaluator.

Every expression here is written the way it appears in a real settings module.
The evaluator's value is entirely in how much real code it can resolve, so
tests built on synthetic shapes would prove nothing.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.evaluator import (
    MAX_SIZE,
    Budget,
    Evaluator,
    Scope,
    collect_env_objects,
    collect_functions,
    collect_imports,
    evaluate,
)
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


def module_scope(source: str, names: dict[str, Value] | None = None) -> Scope:
    """Build a scope from module source, so imports are recognised as written."""
    tree = ast.parse(source)
    imports = collect_imports(tree)
    return Scope(
        names=names or {},
        imports=imports,
        functions=collect_functions(tree),
        env_objects=collect_env_objects(tree, imports),
    )


class TestImportTracking:
    def test_plain_import(self) -> None:
        scope = module_scope("import os")
        node = ast.parse("os.environ", mode="eval").body
        assert scope.origin(node) == "os.environ"

    def test_from_import(self) -> None:
        scope = module_scope("from os import environ")
        node = ast.parse("environ.get", mode="eval").body
        assert scope.origin(node) == "os.environ.get"

    def test_aliased_import(self) -> None:
        scope = module_scope("import os.path as p")
        node = ast.parse("p.join", mode="eval").body
        assert scope.origin(node) == "os.path.join"

    def test_an_unimported_name_has_no_origin(self) -> None:
        # Otherwise a local variable called `environ` would be mistaken for
        # os.environ and silently change a rule's answer.
        scope = module_scope("x = 1")
        node = ast.parse("environ.get", mode="eval").body
        assert scope.origin(node) is None


class TestEnvironmentAccess:
    def test_getenv_with_a_default_resolves_to_the_default(self) -> None:
        # The default is what a fresh deployment gets, which is exactly the
        # case worth warning about.
        scope = module_scope("import os")
        result = value_of("os.getenv('DEBUG', 'True')", scope)
        assert result.is_literal
        assert result.literal == "True"
        assert result.env_dependent

    def test_environ_get_with_a_default(self) -> None:
        scope = module_scope("import os")
        result = value_of("os.environ.get('ALLOWED_HOSTS', 'localhost')", scope)
        assert result.literal == "localhost"
        assert result.env_dependent

    def test_keyword_default(self) -> None:
        # The form healthchecks uses: os.getenv(s, default=default)
        scope = module_scope("import os")
        result = value_of("os.getenv('DEBUG', default='True')", scope)
        assert result.literal == "True"
        assert result.env_dependent

    def test_from_os_import_environ(self) -> None:
        scope = module_scope("from os import environ")
        result = value_of("environ.get('DEBUG', 'True')", scope)
        assert result.literal == "True"

    def test_from_os_import_getenv(self) -> None:
        scope = module_scope("from os import getenv")
        result = value_of("getenv('DEBUG', 'True')", scope)
        assert result.literal == "True"

    def test_getenv_without_a_default_resolves_to_none(self) -> None:
        # os.getenv returns None when unset. That is a value, not the absence
        # of one, and a rule asking "is SECRET_KEY unset" needs it.
        scope = module_scope("import os")
        result = value_of("os.getenv('SECRET_KEY')", scope)
        assert result.is_literal
        assert result.literal is None
        assert result.env_dependent

    def test_environ_get_without_a_default_resolves_to_none(self) -> None:
        scope = module_scope("import os")
        result = value_of("os.environ.get('SECRET_KEY')", scope)
        assert result.literal is None
        assert result.env_dependent

    def test_direct_subscript_has_no_default(self) -> None:
        # os.environ["DEBUG"] raises KeyError when unset, so unlike .get there
        # genuinely is no value -- only a dependency to record.
        scope = module_scope("import os")
        result = value_of("os.environ['SECRET_KEY']", scope)
        assert result.is_unknown
        assert result.env_dependent

    def test_taint_survives_being_used(self) -> None:
        scope = module_scope("import os")
        result = value_of("os.getenv('HOST', 'localhost') + '/api'", scope)
        assert result.literal == "localhost/api"
        assert result.env_dependent

    def test_an_unresolvable_default_is_unknown(self) -> None:
        scope = module_scope("import os")
        assert value_of("os.getenv('X', mystery())", scope).is_unknown

    def test_a_local_variable_named_environ_is_not_the_environment(self) -> None:
        scope = module_scope("x = 1", names={"environ": Value.of({"A": "b"})})
        result = value_of("environ.get('A', 'fallback')", scope)
        assert not result.env_dependent


class TestSubscript:
    def test_indexing_a_resolved_container(self) -> None:
        scope = Scope(names={"DATABASES": Value.of({"default": {"NAME": "app"}})})
        assert literal("DATABASES['default']", scope) == {"NAME": "app"}

    def test_list_index(self) -> None:
        scope = Scope(names={"HOSTS": Value.of(["a", "b"])})
        assert literal("HOSTS[0]", scope) == "a"

    def test_a_missing_key_is_unknown_not_a_crash(self) -> None:
        scope = Scope(names={"D": Value.of({"a": 1})})
        assert value_of("D['nope']", scope).is_unknown


class TestConditionalExpressions:
    def test_a_resolvable_test_takes_one_branch(self) -> None:
        assert literal("'a' if True else 'b'") == "a"

    def test_an_unresolvable_test_keeps_both_branches(self) -> None:
        result = value_of("'a' if MYSTERY else 'b'")
        assert result.is_conditional
        assert set(result.possible()) == {"a", "b"}

    def test_an_environment_dependent_test_taints_the_outcome(self) -> None:
        # Which branch we took depended on the environment, even though the
        # branch itself is a plain literal.
        scope = module_scope("import os")
        result = value_of("True if os.getenv('DEBUG') else False", scope)
        assert result.env_dependent

    def test_agreeing_branches_collapse(self) -> None:
        result = value_of("True if MYSTERY else True")
        assert result.is_literal
        assert result.literal is True


class TestBooleanOperators:
    def test_or_returns_the_first_truthy_operand(self) -> None:
        assert literal("'' or 'fallback'") == "fallback"

    def test_or_short_circuits(self) -> None:
        assert literal("'set' or 'fallback'") == "set"

    def test_and_returns_the_first_falsy_operand(self) -> None:
        assert literal("'' and 'unused'") == ""

    def test_and_returns_the_last_operand_when_all_truthy(self) -> None:
        assert literal("'a' and 'b'") == "b"

    def test_environment_fallback_idiom(self) -> None:
        # `os.getenv("X") or "default"` is everywhere in real settings.
        scope = module_scope("import os")
        result = value_of("os.getenv('SECRET_KEY') or 'insecure-default'", scope)
        assert result.literal == "insecure-default"
        assert result.env_dependent

    def test_an_unresolvable_operand_keeps_every_possibility(self) -> None:
        result = value_of("MYSTERY or 'fallback'")
        assert result.is_conditional
        assert "fallback" in result.possible()

    def test_could_be_is_honest_about_a_partly_unknown_or(self) -> None:
        assert value_of("MYSTERY or 'fallback'").could_be("anything")


class TestComparisons:
    def test_equality(self) -> None:
        assert literal("'a' == 'a'") is True

    def test_inequality(self) -> None:
        assert literal("1 != 2") is True

    def test_membership(self) -> None:
        assert literal("'*' in ['*', 'localhost']") is True

    def test_chained(self) -> None:
        assert literal("1 < 2 < 3") is True

    def test_chained_short_circuits_to_false(self) -> None:
        assert literal("1 < 5 < 3") is False

    def test_identity(self) -> None:
        assert literal("None is None") is True

    def test_the_envbool_idiom(self) -> None:
        # The comparison at the heart of the healthchecks helper.
        scope = module_scope("import os")
        result = value_of("os.getenv('DEBUG', 'True') == 'True'", scope)
        assert result.literal is True
        assert result.env_dependent

    def test_an_unresolvable_operand_is_unknown(self) -> None:
        assert value_of("MYSTERY == 'a'").is_unknown

    def test_a_failing_comparison_is_unknown_not_a_crash(self) -> None:
        assert value_of("1 < 'a'").is_unknown


class TestComprehensions:
    def test_list_comprehension(self) -> None:
        scope = Scope(names={"HOSTS": Value.of(["A", "B"])})
        assert literal("[h.lower() for h in HOSTS]", scope) == ["a", "b"]

    def test_filter(self) -> None:
        scope = Scope(names={"HOSTS": Value.of(["a", "", "b"])})
        assert literal("[h for h in HOSTS if h]", scope) == ["a", "b"]

    def test_dict_comprehension(self) -> None:
        scope = Scope(names={"KEYS": Value.of(["a", "b"])})
        assert literal("{k: k.upper() for k in KEYS}", scope) == {"a": "A", "b": "B"}

    def test_set_comprehension(self) -> None:
        scope = Scope(names={"X": Value.of([1, 2, 2])})
        assert literal("{i for i in X}", scope) == {1, 2}

    def test_over_a_split_string(self) -> None:
        # ALLOWED_HOSTS = [h.strip() for h in os.getenv("HOSTS", "a,b").split(",")]
        scope = module_scope("import os")
        assert literal("[h.strip() for h in os.getenv('HOSTS', 'a, b').split(',')]", scope) == [
            "a",
            "b",
        ]

    def test_taint_propagates_from_the_source(self) -> None:
        scope = module_scope("import os")
        result = value_of("[h for h in os.getenv('HOSTS', 'a').split(',')]", scope)
        assert result.env_dependent

    def test_an_unresolvable_source_is_unknown(self) -> None:
        assert value_of("[h for h in MYSTERY]").is_unknown

    def test_the_loop_variable_does_not_leak(self) -> None:
        scope = Scope(names={"X": Value.of([1, 2])})
        value_of("[i for i in X]", scope)
        assert "i" not in scope.names

    def test_nested_comprehension_is_unknown_not_wrong(self) -> None:
        scope = Scope(names={"X": Value.of([[1], [2]])})
        assert value_of("[i for row in X for i in row]", scope).is_unknown


HEALTHCHECKS_HELPERS = """
import os

def envbool(s: str, default: str) -> bool:
    v = os.getenv(s, default=default)
    if v not in ("", "True", "False"):
        msg = f"Unexpected value {s}={v}, use 'True' or 'False'"
        raise ImproperlyConfigured(msg)
    return v == "True"


def envint(s: str, default: str):
    v = os.getenv(s, default)
    if v == "None":
        return None
    return int(v)


def envsecret(s: str, default=None):
    if secret_path := os.getenv(s + "_FILE"):
        return secret_path
    return os.getenv(s, default)
"""


class TestFollowingLocalFunctions:
    """Verbatim helpers from healthchecks, which is the point of this feature."""

    def test_envbool_resolves_through_the_helper(self) -> None:
        scope = module_scope(HEALTHCHECKS_HELPERS)
        result = value_of("envbool('DEBUG', 'True')", scope)
        assert result.literal is True
        assert result.env_dependent

    def test_envbool_with_a_false_default(self) -> None:
        scope = module_scope(HEALTHCHECKS_HELPERS)
        assert literal("envbool('DEBUG', 'False')", scope) is False

    def test_envint_casts_through_the_helper(self) -> None:
        scope = module_scope(HEALTHCHECKS_HELPERS)
        assert literal("envint('EMAIL_PORT', '587')", scope) == 587

    def test_envint_takes_the_early_return(self) -> None:
        scope = module_scope(HEALTHCHECKS_HELPERS)
        assert literal("envint('PORT', 'None')", scope) is None

    def test_walrus_binding(self) -> None:
        scope = module_scope(HEALTHCHECKS_HELPERS)
        # SECRET_KEY_FILE is unset, so the walrus binds None and the branch is
        # not taken -- the function falls through to the plain getenv.
        assert literal("envsecret('SECRET_KEY', '---')", scope) == "---"

    def test_keyword_arguments(self) -> None:
        scope = module_scope(HEALTHCHECKS_HELPERS + "\ndef f(a, b='x'):\n    return a + b\n")
        assert literal("f('p', b='q')", scope) == "pq"

    def test_parameter_defaults(self) -> None:
        scope = module_scope("def f(a, b='!'):\n    return a + b\n")
        assert literal("f('hi')", scope) == "hi!"

    def test_falling_off_the_end_returns_none(self) -> None:
        scope = module_scope("def f():\n    x = 1\n")
        assert literal("f()", scope) is None

    def test_an_unconditional_raise_is_unknown(self) -> None:
        scope = module_scope("def f():\n    raise ValueError('no')\n")
        assert value_of("f()", scope).is_unknown

    def test_an_unresolvable_branch_is_unknown(self) -> None:
        scope = module_scope("def f(x):\n    if x:\n        return 1\n    return 2\n")
        assert value_of("f(MYSTERY)", scope).is_unknown

    def test_an_unsupported_statement_is_unknown_not_wrong(self) -> None:
        scope = module_scope("def f():\n    for i in range(3):\n        pass\n    return 1\n")
        assert value_of("f()", scope).is_unknown

    def test_recursion_does_not_hang(self) -> None:
        scope = module_scope("def f(x):\n    return f(x)\n")
        assert value_of("f(1)", scope).is_unknown

    def test_mutual_recursion_does_not_hang(self) -> None:
        scope = module_scope("def a(x):\n    return b(x)\ndef b(x):\n    return a(x)\n")
        assert value_of("a(1)", scope).is_unknown

    def test_locals_do_not_leak_into_the_module(self) -> None:
        scope = module_scope("def f():\n    hidden = 1\n    return hidden\n")
        value_of("f()", scope)
        assert "hidden" not in scope.names

    def test_star_args_are_refused(self) -> None:
        scope = module_scope("def f(*args):\n    return 1\n")
        assert value_of("f(1)", scope).is_unknown

    def test_too_many_arguments_is_unknown(self) -> None:
        scope = module_scope("def f(a):\n    return a\n")
        assert value_of("f(1, 2)", scope).is_unknown

    def test_a_helper_can_see_module_globals(self) -> None:
        scope = module_scope("BASE = '/srv'\ndef f():\n    return BASE\n")
        scope.names["BASE"] = Value.of("/srv")
        assert literal("f()", scope) == "/srv"


class TestDjangoEnviron:
    def test_bool_takes_its_default_second(self) -> None:
        scope = module_scope("import environ\nenv = environ.Env()")
        result = value_of("env.bool('DEBUG', False)", scope)
        assert result.literal is False
        assert result.env_dependent

    def test_a_string_default_is_not_cast(self) -> None:
        # Verified against django-environ: it returns the default untouched,
        # so env.bool("DEBUG", "True") really is the string, not True.
        scope = module_scope("import environ\nenv = environ.Env()")
        assert literal("env.bool('DEBUG', 'True')", scope) == "True"

    def test_int_default_is_not_cast_either(self) -> None:
        scope = module_scope("import environ\nenv = environ.Env()")
        assert literal("env.int('PORT', '8000')", scope) == "8000"

    def test_list_takes_a_cast_second_not_a_default(self) -> None:
        # The signature is list(var, cast=None, default=NOTSET). Reading the
        # second argument as a default would report a cast as the value.
        scope = module_scope("import environ\nenv = environ.Env()")
        assert value_of("env.list('HOSTS', ['a'])", scope).is_unknown

    def test_list_default_by_keyword(self) -> None:
        scope = module_scope("import environ\nenv = environ.Env()")
        assert literal("env.list('HOSTS', default=['a', 'b'])", scope) == ["a", "b"]

    def test_list_default_third_positionally(self) -> None:
        scope = module_scope("import environ\nenv = environ.Env()")
        assert literal("env.list('HOSTS', str, ['a'])", scope) == ["a"]

    def test_call_falls_back_to_the_declared_schema(self) -> None:
        scope = module_scope("import environ\nenv = environ.Env(DEBUG=(bool, False))")
        result = value_of("env('DEBUG')", scope)
        assert result.literal is False
        assert result.env_dependent

    def test_call_takes_a_cast_second_not_a_default(self) -> None:
        scope = module_scope("import environ\nenv = environ.Env()")
        assert value_of("env('DEBUG', bool)", scope).is_unknown

    def test_call_with_an_explicit_keyword_default(self) -> None:
        scope = module_scope("import environ\nenv = environ.Env()")
        assert literal("env('SITE', default='localhost')", scope) == "localhost"

    def test_a_required_variable_has_no_value(self) -> None:
        # django-environ raises ImproperlyConfigured, so there is no value.
        scope = module_scope("import environ\nenv = environ.Env()")
        result = value_of("env.bool('DEBUG')", scope)
        assert result.is_unknown
        assert result.env_dependent

    def test_db_transforms_its_default(self) -> None:
        scope = module_scope("import environ\nenv = environ.Env()")
        assert value_of("env.db('DATABASE_URL', 'sqlite:///x')", scope).is_unknown

    def test_from_environ_import_env(self) -> None:
        scope = module_scope("from environ import Env\nenv = Env(DEBUG=(bool, True))")
        assert literal("env('DEBUG')", scope) is True

    def test_a_local_variable_named_env_is_not_an_env_object(self) -> None:
        scope = module_scope("env = 1")
        assert value_of("env.bool('DEBUG', False)", scope).is_unknown


class TestPythonDecouple:
    def test_default(self) -> None:
        scope = module_scope("from decouple import config")
        result = value_of("config('DEBUG', default=False)", scope)
        assert result.literal is False
        assert result.env_dependent

    def test_default_is_cast_unlike_django_environ(self) -> None:
        # Verified against python-decouple, which does apply the cast.
        scope = module_scope("from decouple import config")
        assert literal("config('DEBUG', default='True', cast=bool)", scope) is True

    def test_falsey_strings(self) -> None:
        scope = module_scope("from decouple import config")
        assert literal("config('DEBUG', default='off', cast=bool)", scope) is False

    def test_int_cast(self) -> None:
        scope = module_scope("from decouple import config")
        assert literal("config('PORT', default='8000', cast=int)", scope) == 8000

    def test_positional_default(self) -> None:
        scope = module_scope("from decouple import config")
        assert literal("config('SITE', 'localhost')", scope) == "localhost"

    def test_an_unknown_cast_is_unknown(self) -> None:
        scope = module_scope("from decouple import config")
        assert value_of("config('HOSTS', default='a,b', cast=Csv())", scope).is_unknown

    def test_an_invalid_truth_value_is_unknown_not_a_crash(self) -> None:
        scope = module_scope("from decouple import config")
        assert value_of("config('DEBUG', default='maybe', cast=bool)", scope).is_unknown

    def test_a_required_value_has_no_default(self) -> None:
        scope = module_scope("from decouple import config")
        assert value_of("config('SECRET_KEY')", scope).is_unknown


class TestAttributeIndirection:
    """``getattr(configuration, "DEBUG", False)`` -- most of NetBox's settings."""

    def test_an_opaque_object_resolves_to_the_default(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        result = value_of("getattr(configuration, 'DEBUG', False)", scope)
        assert result.literal is False
        assert result.env_dependent

    def test_no_default_stays_unknown(self) -> None:
        # NetBox marks these "# Required": getattr raises if absent, so there
        # is no value to fall back to.
        scope = module_scope("configuration = load_configuration()")
        assert value_of("getattr(configuration, 'ALLOWED_HOSTS')", scope).is_unknown

    def test_a_visible_namespace_wins_over_the_default(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        scope.attributes["configuration"] = {"DEBUG": Value.of(True)}
        result = value_of("getattr(configuration, 'DEBUG', False)", scope)
        assert result.literal is True
        assert not result.env_dependent

    def test_a_visible_namespace_without_the_attribute_uses_the_default(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        scope.attributes["configuration"] = {"OTHER": Value.of(1)}
        result = value_of("getattr(configuration, 'DEBUG', False)", scope)
        assert result.literal is False
        assert not result.env_dependent

    def test_a_visible_namespace_without_a_default_is_unknown(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        scope.attributes["configuration"] = {"OTHER": Value.of(1)}
        assert value_of("getattr(configuration, 'DEBUG')", scope).is_unknown

    def test_an_imported_module_namespace(self) -> None:
        scope = module_scope("import myproject.conf")
        scope.attributes["myproject"] = {"DEBUG": Value.of(True)}
        assert literal("getattr(myproject, 'DEBUG', False)", scope) is True

    def test_getattr_on_a_resolved_value_is_unknown(self) -> None:
        scope = Scope(names={"HOSTS": Value.of(["a"])})
        assert value_of("getattr(HOSTS, 'DEBUG', False)", scope).is_unknown

    def test_a_dynamic_attribute_name_is_unknown(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        assert value_of("getattr(configuration, MYSTERY, False)", scope).is_unknown

    def test_the_default_is_evaluated_not_copied(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        assert literal("getattr(configuration, 'PATH', 'a' + 'b')", scope) == "ab"

    def test_an_unresolvable_default_stays_unknown(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        assert value_of("getattr(configuration, 'X', MYSTERY)", scope).is_unknown

    def test_a_shadowed_getattr_is_not_treated_as_the_builtin(self) -> None:
        scope = Scope(names={"getattr": Value.of("shadowed")})
        assert value_of("getattr(configuration, 'DEBUG', True)", scope).is_unknown

    def test_taint_survives_wrapping(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        result = value_of("getattr(configuration, 'BASE_PATH', '/a/').strip('/')", scope)
        assert result.literal == "a"
        assert result.env_dependent


class TestContainerMethods:
    def test_dict_get_with_a_default(self) -> None:
        scope = Scope(names={"REDIS": Value.of({"HOST": "db"})})
        assert literal("REDIS.get('HOST', 'localhost')", scope) == "db"

    def test_dict_get_falls_back(self) -> None:
        scope = Scope(names={"REDIS": Value.of({})})
        assert literal("REDIS.get('HOST', 'localhost')", scope) == "localhost"

    def test_dict_get_without_a_default_is_none(self) -> None:
        scope = Scope(names={"REDIS": Value.of({})})
        assert literal("REDIS.get('URL')", scope) is None

    def test_dict_keys_materialise_to_a_list(self) -> None:
        scope = Scope(names={"D": Value.of({"a": 1, "b": 2})})
        assert literal("D.keys()", scope) == ["a", "b"]

    def test_dict_items_materialise(self) -> None:
        scope = Scope(names={"D": Value.of({"a": 1})})
        assert literal("list(D.items())", scope) == [("a", 1)]

    def test_taint_propagates_through_get(self) -> None:
        scope = module_scope("configuration = load_configuration()")
        result = value_of("getattr(configuration, 'REDIS', {}).get('HOST', 'localhost')", scope)
        assert result.literal == "localhost"
        assert result.env_dependent

    def test_an_unsupported_dict_method_is_unknown(self) -> None:
        scope = Scope(names={"D": Value.of({"a": 1})})
        assert value_of("D.pop('a')", scope).is_unknown

    def test_a_string_method_on_a_dict_is_unknown(self) -> None:
        scope = Scope(names={"D": Value.of({"a": 1})})
        assert value_of("D.upper()", scope).is_unknown

    def test_list_index(self) -> None:
        scope = Scope(names={"X": Value.of(["a", "b"])})
        assert literal("X.index('b')", scope) == 1

    def test_a_mutating_list_method_is_unknown(self) -> None:
        scope = Scope(names={"X": Value.of(["a"])})
        assert value_of("X.append('b')", scope).is_unknown
