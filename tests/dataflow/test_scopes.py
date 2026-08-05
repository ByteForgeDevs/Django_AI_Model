"""Tests for the lexical scope model.

Organised around the four rules a naive resolver gets wrong, because each of
them is a false positive generator rather than a technicality.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.dataflow.scopes import (
    ELEMENT_KINDS,
    Binding,
    BindingKind,
    ScopeKind,
    build_scopes,
)


def scopes(source: str):
    return build_scopes(ast.parse(source))


def only_child(scope, name: str):
    matches = [child for child in scope.children if child.name == name]
    assert len(matches) == 1, f"expected one {name!r}, got {[c.name for c in scope.children]}"
    return matches[0]


class TestModuleLevelBindings:
    def test_a_plain_assignment_binds(self):
        root = scopes("qs = Model.objects.all()")
        binding = root.resolve("qs")
        assert binding is not None
        assert binding.kind is BindingKind.ASSIGNMENT
        assert isinstance(binding.value, ast.Call)

    def test_an_unbound_name_resolves_to_nothing(self):
        assert scopes("x = 1").resolve("nowhere") is None

    def test_imports_bind_the_local_name(self):
        root = scopes("import os.path\nfrom a.b import c as d\n")
        assert root.resolve("os").kind is BindingKind.IMPORT
        assert root.resolve("d").kind is BindingKind.IMPORT
        assert root.resolve("c") is None, "the alias replaced it"

    def test_a_star_import_binds_no_name(self):
        assert scopes("from a import *").bindings == {}

    def test_chained_assignment_binds_both_names_to_one_value(self):
        root = scopes("a = b = Model.objects.all()")
        assert root.resolve("a").value is root.resolve("b").value

    def test_tuple_unpacking_is_marked_unpacked(self):
        root = scopes("a, b = pair()")
        assert root.resolve("a").unpacked
        assert root.resolve("b").unpacked

    def test_a_starred_target_is_unpacked(self):
        root = scopes("first, *rest = things()")
        assert root.resolve("rest").unpacked

    def test_attribute_assignment_binds_no_name(self):
        assert scopes("obj.attr = 1").bindings == {}

    def test_a_bare_annotation_does_not_bind(self):
        """``x: int`` declares a type. It does not create a value."""
        assert scopes("x: int").bindings == {}

    def test_an_annotated_assignment_does_bind(self):
        root = scopes("x: int = 5")
        assert root.resolve("x").kind is BindingKind.ASSIGNMENT


class TestClassBodiesAreNotEnclosingScopes:
    """The trap that would invent N+1s against querysets a method never reads."""

    SOURCE = """
class View:
    queryset = Model.objects.all()

    def get(self):
        return queryset
"""

    def test_the_class_attribute_is_bound_in_the_class_scope(self):
        root = scopes(self.SOURCE)
        view = only_child(root, "View")
        assert view.kind is ScopeKind.CLASS
        assert view.binds("queryset")

    def test_a_method_cannot_see_it(self):
        root = scopes(self.SOURCE)
        get = only_child(only_child(root, "View"), "get")
        assert get.resolve("queryset") is None, (
            "resolving this to the class attribute is how a rule reports an N+1 "
            "against a queryset the method never touches"
        )

    def test_the_class_body_itself_can_see_it(self):
        root = scopes(self.SOURCE + "\n")
        view = only_child(root, "View")
        assert view.resolve("queryset") is not None

    def test_a_method_still_reaches_module_globals_through_the_class(self):
        root = scopes("SETTING = 1\n\nclass A:\n    def m(self):\n        return SETTING\n")
        method = only_child(only_child(root, "A"), "m")
        assert method.resolve("SETTING") is not None

    def test_a_class_shadowing_a_global_does_not_shadow_it_for_methods(self):
        source = (
            "qs = 'module'\n\nclass A:\n    qs = 'class'\n    def m(self):\n        return qs\n"
        )
        root = scopes(source)
        method = only_child(only_child(root, "A"), "m")
        resolved = method.resolve("qs")
        assert resolved is not None
        assert ast.literal_eval(resolved.value) == "module"

    def test_a_nested_class_in_a_method_still_sees_the_method(self):
        source = "def outer():\n    local = 1\n    class Inner:\n        x = local\n"
        root = scopes(source)
        inner = only_child(only_child(root, "outer"), "Inner")
        assert inner.resolve("local") is not None


class TestFunctionScopes:
    def test_parameters_are_bound(self):
        root = scopes("def view(request, pk):\n    pass\n")
        view = only_child(root, "view")
        assert view.resolve("request").kind is BindingKind.PARAMETER
        assert view.resolve("pk").kind is BindingKind.PARAMETER

    def test_a_parameter_carries_its_default(self):
        root = scopes("def f(a, b=10, *, c=20):\n    pass\n")
        f = only_child(root, "f")
        assert f.resolve("a").value is None
        assert ast.literal_eval(f.resolve("b").value) == 10
        assert ast.literal_eval(f.resolve("c").value) == 20

    def test_defaults_line_up_from_the_right(self):
        """Off by one here silently attaches the wrong default to a parameter."""
        root = scopes("def f(a, b, c=1, d=2):\n    pass\n")
        f = only_child(root, "f")
        assert f.resolve("a").value is None
        assert f.resolve("b").value is None
        assert ast.literal_eval(f.resolve("c").value) == 1
        assert ast.literal_eval(f.resolve("d").value) == 2

    def test_varargs_and_kwargs_bind(self):
        root = scopes("def f(*args, **kwargs):\n    pass\n")
        f = only_child(root, "f")
        assert f.resolve("args").kind is BindingKind.PARAMETER
        assert f.resolve("kwargs").kind is BindingKind.PARAMETER

    def test_positional_only_parameters_bind(self):
        f = only_child(scopes("def f(a, /, b):\n    pass\n"), "f")
        assert f.resolve("a") is not None

    def test_a_local_does_not_escape_to_the_module(self):
        root = scopes("def f():\n    local = 1\n")
        assert root.resolve("local") is None

    def test_a_name_assigned_anywhere_in_a_function_is_local_throughout(self):
        """Python decides locality per function, not per line."""
        root = scopes("qs = 'module'\n\ndef f():\n    read = qs\n    qs = 'local'\n")
        f = only_child(root, "f")
        assert f.resolve_scope("qs") is f

    def test_the_function_name_binds_in_the_enclosing_scope(self):
        root = scopes("def f():\n    pass\n")
        assert root.resolve("f").kind is BindingKind.FUNCTION_DEF

    def test_a_default_is_evaluated_in_the_enclosing_scope(self):
        """``def f(x=name)`` reads the outer ``name`` at definition time."""
        root = scopes("def f(x=lambda: 1):\n    pass\n")
        assert any(child.kind is ScopeKind.LAMBDA for child in root.children)

    def test_nested_functions_see_the_enclosing_function(self):
        root = scopes("def outer():\n    qs = 1\n    def inner():\n        return qs\n")
        inner = only_child(only_child(root, "outer"), "inner")
        assert inner.resolve("qs") is not None

    def test_an_async_function_is_a_scope_like_any_other(self):
        root = scopes("async def f(request):\n    pass\n")
        assert only_child(root, "f").resolve("request") is not None


class TestLoopTargets:
    """A for-target holds an *element*, and confusing the two breaks N+1."""

    def test_a_for_target_points_at_the_iterable_and_is_flagged(self):
        root = scopes("for book in Book.objects.all():\n    pass\n")
        binding = root.resolve("book")
        assert binding.kind is BindingKind.FOR_TARGET
        assert binding.element_of, (
            "without this flag a rule reads `book` as the queryset itself "
            "rather than one row from it"
        )
        assert isinstance(binding.value, ast.Call)

    def test_tuple_targets_are_both_element_and_unpacked(self):
        root = scopes("for key, value in mapping.items():\n    pass\n")
        assert root.resolve("key").element_of
        assert root.resolve("key").unpacked

    def test_an_async_for_binds_the_same_way(self):
        root = scopes("async def f():\n    async for row in qs:\n        pass\n")
        f = only_child(root, "f")
        assert f.resolve("row").element_of

    def test_names_bound_in_a_loop_body_belong_to_the_enclosing_scope(self):
        """Unlike comprehensions, a for statement introduces no scope."""
        root = scopes("for x in y:\n    found = x\n")
        assert root.binds("found")


class TestComprehensionScopes:
    def test_the_target_does_not_leak(self):
        root = scopes("names = [b.title for b in Book.objects.all()]")
        assert root.resolve("b") is None, "Python 3 comprehensions do not leak their target"

    def test_the_target_is_bound_inside_the_comprehension(self):
        root = scopes("names = [b.title for b in Book.objects.all()]")
        inner = only_child(root, "<comprehension>")
        assert inner.resolve("b").element_of

    def test_the_first_iterable_is_evaluated_in_the_enclosing_scope(self):
        """``[x for x in x]`` reads the outer ``x`` in the iterable position."""
        root = scopes("x = Book.objects.all()\nnames = [x for x in x]")
        comp = only_child(root, "<comprehension>")
        assert comp.resolve("x").kind is BindingKind.COMPREHENSION_TARGET
        assert root.resolve("x").kind is BindingKind.ASSIGNMENT

    def test_a_later_generator_sees_the_earlier_target(self):
        root = scopes("out = [c for b in books for c in b.chapters.all()]")
        comp = only_child(root, "<comprehension>")
        assert comp.binds("b")
        assert comp.binds("c")

    def test_dict_comprehensions_bind_their_targets(self):
        root = scopes("m = {k: v for k, v in pairs}")
        comp = only_child(root, "<comprehension>")
        assert comp.binds("k")
        assert comp.binds("v")
        assert root.resolve("k") is None

    def test_generator_expressions_get_a_scope(self):
        root = scopes("total = sum(b.pages for b in Book.objects.all())")
        assert only_child(root, "<comprehension>").binds("b")

    def test_a_comprehension_still_sees_the_enclosing_function(self):
        root = scopes("def f():\n    limit = 5\n    return [x for x in y if x < limit]\n")
        comp = only_child(only_child(root, "f"), "<comprehension>")
        assert comp.resolve("limit") is not None


class TestWalrus:
    def test_a_walrus_binds_in_the_current_scope(self):
        root = scopes("if (n := count()) > 0:\n    pass\n")
        assert root.resolve("n").kind is BindingKind.WALRUS

    def test_a_walrus_in_a_comprehension_binds_outside_it(self):
        """PEP 572: the one way a comprehension leaks a name."""
        root = scopes("def f():\n    out = [y for x in items if (y := x.total) > 0]\n")
        f = only_child(root, "f")
        comp = only_child(f, "<comprehension>")
        assert f.binds("y"), "the walrus target belongs to the function"
        assert not comp.binds("y"), "and not to the comprehension"

    def test_a_walrus_in_a_module_level_comprehension_binds_at_module_level(self):
        root = scopes("out = [y for x in items if (y := x.total) > 0]")
        assert root.binds("y")

    def test_a_walrus_in_a_nested_comprehension_escapes_all_of_them(self):
        root = scopes("def f():\n    out = [[y for _ in a if (y := 1)] for a in b]\n")
        assert only_child(root, "f").binds("y")


class TestGlobalAndNonlocal:
    def test_a_global_assignment_lands_in_the_module_scope(self):
        """``global qs; qs = ...`` rebinds the module's name, not a local one."""
        root = scopes("qs = 'module'\n\ndef f():\n    global qs\n    qs = 'set'\n")
        f = only_child(root, "f")
        assert not f.binds("qs"), "the assignment is not local to f"
        assert [ast.literal_eval(b.value) for b in root.own_all("qs")] == ["module", "set"]

    def test_resolve_and_resolve_scope_agree_about_a_global(self):
        """Two rules asking related questions must not get unrelated answers."""
        root = scopes("qs = 'module'\n\ndef f():\n    global qs\n    qs = 'set'\n")
        f = only_child(root, "f")
        assert f.resolve_scope("qs") is root
        assert f.resolve("qs") is root.own("qs")

    def test_global_on_an_unbound_name_resolves_to_nothing(self):
        root = scopes("def f():\n    global missing\n    return missing\n")
        assert only_child(root, "f").resolve("missing") is None

    def test_nonlocal_reaches_the_enclosing_function(self):
        source = "def outer():\n    qs = 1\n    def inner():\n        nonlocal qs\n        qs = 2\n"
        root = scopes(source)
        outer = only_child(root, "outer")
        inner = only_child(outer, "inner")
        assert not inner.binds("qs")
        assert inner.resolve_scope("qs") is outer
        assert [ast.literal_eval(b.value) for b in outer.own_all("qs")] == [1, 2]

    def test_nonlocal_does_not_reach_the_module(self):
        """``nonlocal`` at this depth is a compile error, but it still parses.

        We must not resolve it to the module binding, because that would be a
        confident answer about code Python refuses to run.
        """
        root = scopes("qs = 1\n\ndef f():\n    nonlocal qs\n    qs = 2\n")
        f = only_child(root, "f")
        assert f.resolve_scope("qs") is f
        assert ast.literal_eval(root.own("qs").value) == 1, "the module binding is untouched"


class TestOtherBindingForms:
    def test_with_binds_its_target(self):
        root = scopes("with open('f') as fh:\n    pass\n")
        binding = root.resolve("fh")
        assert binding.kind is BindingKind.WITH_TARGET
        assert not binding.element_of

    def test_except_binds_its_name(self):
        root = scopes("try:\n    pass\nexcept ValueError as exc:\n    pass\n")
        assert root.resolve("exc").kind is BindingKind.EXCEPT_TARGET

    def test_a_class_name_binds_in_the_enclosing_scope(self):
        root = scopes("class A:\n    pass\n")
        assert root.resolve("A").kind is BindingKind.CLASS_DEF

    def test_augmented_assignment_binds(self):
        root = scopes("total = 0\ntotal += 1\n")
        assert root.resolve("total").kind is BindingKind.AUGMENTED

    def test_match_captures_bind(self):
        root = scopes("match value:\n    case [a, b]:\n        pass\n")
        assert root.binds("a")
        assert root.binds("b")

    def test_a_match_mapping_rest_binds(self):
        root = scopes("match value:\n    case {'k': v, **rest}:\n        pass\n")
        assert root.binds("rest")


class TestReassignment:
    def test_element_of_is_derived_not_stored(self):
        """A stored flag could disagree with the kind it describes.

        A ``FOR_TARGET`` claiming ``element_of=False`` would tell a rule that a
        loop variable *is* the queryset rather than one row of it, which is the
        single confusion this whole distinction exists to prevent.
        """
        for kind in BindingKind:
            binding = Binding("x", kind, ast.parse("x").body[0])
            assert binding.element_of == (kind in ELEMENT_KINDS)

    def test_only_iteration_kinds_are_elements(self):
        assert set(ELEMENT_KINDS) == {
            BindingKind.FOR_TARGET,
            BindingKind.COMPREHENSION_TARGET,
        }

    def test_every_binding_is_kept_in_source_order(self):
        root = scopes("qs = A.objects.all()\nqs = qs.select_related('x')\n")
        all_of_them = root.own_all("qs")
        assert len(all_of_them) == 2
        assert all_of_them[0].lineno < all_of_them[1].lineno

    def test_resolve_returns_the_last_one(self):
        root = scopes("qs = 1\nqs = 2\n")
        assert ast.literal_eval(root.resolve("qs").value) == 2


class TestNavigation:
    def test_walk_yields_every_scope(self):
        root = scopes("class A:\n    def m(self):\n        return [x for x in y]\n")
        kinds = [scope.kind for scope in root.walk()]
        assert kinds == [
            ScopeKind.MODULE,
            ScopeKind.CLASS,
            ScopeKind.FUNCTION,
            ScopeKind.COMPREHENSION,
        ]

    def test_qualname_reads_as_a_path(self):
        root = scopes("class A:\n    def m(self):\n        pass\n")
        method = only_child(only_child(root, "A"), "m")
        assert method.qualname == "<module>.A.m"

    def test_visible_scopes_skips_the_class_but_keeps_the_module(self):
        root = scopes("class A:\n    def m(self):\n        pass\n")
        method = only_child(only_child(root, "A"), "m")
        assert [s.kind for s in method.visible_scopes()] == [
            ScopeKind.FUNCTION,
            ScopeKind.MODULE,
        ]

    def test_repr_names_the_scope(self):
        assert "<module>" in repr(scopes("x = 1"))


class TestRobustness:
    """It runs over whatever is in the repository, so it must not crash."""

    @pytest.mark.parametrize(
        "source",
        [
            "",
            "pass",
            "def f(): ...",
            "async def f():\n    async with a() as b:\n        pass\n",
            "x = [i for j in k for i in j]",
            "lambda *a, **k: 0",
            "class A(B, metaclass=M):\n    pass\n",
            "try:\n    pass\nexcept* ValueError as e:\n    pass\n",
            "del x",
            "assert x, 'msg'",
            "raise ValueError() from exc",
            "@decorator\ndef f():\n    pass\n",
            "for a in b:\n    pass\nelse:\n    pass\n",
            "while x:\n    break\nelse:\n    pass\n",
            "with a() as (x, y):\n    pass\n",
        ],
    )
    def test_it_survives(self, source):
        assert build_scopes(ast.parse(source)) is not None
