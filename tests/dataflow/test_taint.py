"""Tests for the taint lattice.

Three answers, and the tests are organised around them, because the whole
design rests on ``UNKNOWN`` being a distinct answer rather than a synonym for
either neighbour. A test that only checked "tainted or not" would pass just as
well against a two-value analysis, which is the analysis this module exists to
avoid being.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.dataflow.chains import DefUse, def_use
from djaudit.dataflow.scopes import build_scopes
from djaudit.dataflow.taint import (
    Taint,
    handles_request,
    join,
    request_source,
    taint_of,
    tainted_parts,
)


def last_expression(source: str, *, scope_name: str | None = None) -> tuple[ast.expr, DefUse]:
    """The final expression statement of ``source``, with chains that reach it."""
    tree = ast.parse(source)
    root = build_scopes(tree)
    scope = root
    if scope_name is not None:
        scope = next(s for s in root.walk() if s.name == scope_name)
    chains = def_use(scope)
    target = next(
        node.value for node in reversed(list(ast.walk(scope.node))) if isinstance(node, ast.Expr)
    )
    return target, chains


def taint(source: str, *, scope_name: str | None = None) -> Taint:
    node, chains = last_expression(source, scope_name=scope_name)
    return taint_of(node, chains)


def function(body: str, *, signature: str = "def view(request):") -> str:
    lines = "\n".join(f"    {line}" for line in body.strip().splitlines())
    return f"{signature}\n{lines}\n"


def in_view(body: str) -> Taint:
    return taint(function(body), scope_name="view")


class TestWhatReachesTheClient:
    @pytest.mark.parametrize(
        "source",
        [
            "request.GET",
            "request.POST",
            "request.data",
            "request.body",
            "request.headers",
            "request.COOKIES",
            "request.FILES",
            "request.META",
            "request.query_params",
        ],
    )
    def test_each_wire_attribute(self, source: str) -> None:
        assert in_view(f"{source}") is Taint.TAINTED

    def test_the_same_attributes_through_self(self) -> None:
        """pretix reads `self.request` 3,735 times against 2,401 bare."""
        assert taint("class V:\n    def get(self):\n        self.request.GET\n") is Taint.TAINTED

    def test_view_kwargs(self) -> None:
        assert taint("class V:\n    def get(self):\n        self.kwargs\n") is Taint.TAINTED

    def test_a_lookup_into_a_source(self) -> None:
        assert in_view("request.GET.get('q')") is Taint.TAINTED

    def test_a_subscript_of_a_source(self) -> None:
        assert in_view("request.GET['q']") is Taint.TAINTED

    def test_taint_carried_by_a_name(self) -> None:
        assert in_view("q = request.GET['q']\nq") is Taint.TAINTED

    def test_taint_carried_through_a_composition(self) -> None:
        assert in_view("q = request.GET['q']\nf'WHERE a = {q}'") is Taint.TAINTED

    def test_taint_through_an_opaque_call(self) -> None:
        """The function may sanitise; nothing here shows that it does."""
        assert in_view("clean(request.GET['q'])") is Taint.TAINTED

    def test_a_loop_variable_over_a_source(self) -> None:
        assert in_view("for k in request.GET:\n        k") is Taint.TAINTED

    def test_one_tainted_part_taints_the_whole(self) -> None:
        assert in_view("f'{table}{request.GET[\"q\"]}'") is Taint.TAINTED

    def test_a_conditional_with_one_tainted_branch(self) -> None:
        assert in_view("x if y else request.GET['q']") is Taint.TAINTED

    def test_a_comprehension_over_a_source(self) -> None:
        assert in_view("[v for v in request.GET.getlist('x')]") is Taint.TAINTED


class TestWhatIsShownSafe:
    def test_a_literal(self) -> None:
        assert in_view("'SELECT 1'") is Taint.SAFE

    def test_a_local_bound_to_a_literal(self) -> None:
        assert in_view("mode = 'READ ONLY'\nmode") is Taint.SAFE

    def test_a_conditional_over_two_literals(self) -> None:
        """The NetBox shape: `'READ WRITE' if allow_write else 'READ ONLY'`."""
        assert in_view("mode = 'RW' if flag else 'RO'\nmode") is Taint.SAFE

    def test_model_metadata(self) -> None:
        assert in_view("Voucher._meta.db_table") is Taint.SAFE

    def test_the_placeholder_join_idiom(self) -> None:
        """The pretix shape: `", ".join(["%s"] * len(rows))`."""
        assert in_view("t = 'VALUES ' + ', '.join(['(%s)'] * len(rows))\nt") is Taint.SAFE

    def test_a_sanitiser_ends_taint(self) -> None:
        assert in_view("int(request.GET['n'])") is Taint.SAFE

    def test_a_sanitiser_inside_a_composition(self) -> None:
        assert in_view("f'LIMIT {int(request.GET[\"n\"])}'") is Taint.SAFE

    def test_a_comparison_is_a_bool(self) -> None:
        assert in_view("request.GET['a'] == 'b'") is Taint.SAFE

    def test_a_middleware_attribute_is_not_a_wire_attribute(self) -> None:
        """pretix reads `request.event` 1,727 times. It is a model instance."""
        assert in_view("request.event") is Taint.SAFE

    def test_request_user_is_not_client_text(self) -> None:
        assert in_view("request.user") is Taint.SAFE

    def test_an_imported_name(self) -> None:
        assert taint("from x import TABLE\nTABLE\n") is Taint.SAFE


class TestWhatItWillNotGuess:
    def test_a_parameter(self) -> None:
        """The most common real defect of this kind, and the most common safe
        pattern. One function's text does not tell them apart."""
        assert taint(function("name", signature="def helper(name):"), scope_name="helper") is (
            Taint.UNKNOWN
        )

    def test_an_unresolvable_name(self) -> None:
        assert in_view("mystery") is Taint.UNKNOWN

    def test_an_opaque_call_with_safe_arguments(self) -> None:
        assert in_view("build('a')") is Taint.UNKNOWN

    def test_a_module_global_read_inside_a_function(self) -> None:
        """Measured on pretix. Reaching across the boundary would be unsound:
        any importer can rebind a module attribute."""
        source = "TIMEOUT = 5\ndef view(request):\n    TIMEOUT\n"
        assert taint(source, scope_name="view") is Taint.UNKNOWN

    def test_the_same_global_read_at_module_scope_is_safe(self) -> None:
        """The contrast that proves the boundary, not the value, is the reason."""
        assert taint("TIMEOUT = 5\nTIMEOUT\n") is Taint.SAFE

    def test_without_chains_every_name_is_unknown(self) -> None:
        node, _ = last_expression("q = 'safe'\nq\n")
        assert taint_of(node, None) is Taint.UNKNOWN

    def test_but_a_source_needs_no_chains(self) -> None:
        """The contrast: taint is syntactic at the source, resolution is not."""
        node, _ = last_expression("request.GET['q']\n")
        assert taint_of(node, None) is Taint.TAINTED


class TestTheLattice:
    def test_join_takes_the_worst(self) -> None:
        assert join([Taint.SAFE, Taint.TAINTED, Taint.UNKNOWN]) is Taint.TAINTED
        assert join([Taint.SAFE, Taint.UNKNOWN]) is Taint.UNKNOWN
        assert join([Taint.SAFE, Taint.SAFE]) is Taint.SAFE

    def test_join_of_nothing_is_safe(self) -> None:
        """An empty composition has nothing that could carry client text."""
        assert join([]) is Taint.SAFE

    def test_ranks_are_ordered(self) -> None:
        assert Taint.SAFE.rank < Taint.UNKNOWN.rank < Taint.TAINTED.rank

    def test_a_cycle_terminates(self) -> None:
        assert in_view("x = 'a'\nx = x + 'b'\nx") is not None

    def test_a_cycle_carrying_taint_is_still_tainted(self) -> None:
        assert in_view("x = request.GET['q']\nx = x + 'b'\nx") is Taint.TAINTED


class TestNamingTheSource:
    def test_it_names_what_it_objected_to(self) -> None:
        node, _ = last_expression("request.GET\n")
        assert request_source(node) == "request.GET"

    def test_through_self(self) -> None:
        tree = ast.parse("self.request.query_params\n")
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.Expr)).value
        assert request_source(node) == "self.request.query_params"

    def test_a_computed_chain_names_nothing(self) -> None:
        node, _ = last_expression("get_request().GET\n")
        assert request_source(node) is None

    def test_a_wire_name_on_something_else(self) -> None:
        node, _ = last_expression("response.headers\n")
        assert request_source(node) is None


class TestWhoHandlesARequest:
    def test_a_request_parameter(self) -> None:
        tree = ast.parse("def view(request):\n    pass\n")
        scope = next(s for s in build_scopes(tree).walk() if s.name == "view")
        assert handles_request(scope)

    def test_self_request_in_the_body(self) -> None:
        tree = ast.parse("class V:\n    def get(self):\n        return self.request.GET\n")
        scope = next(s for s in build_scopes(tree).walk() if s.name == "get")
        assert handles_request(scope)

    def test_a_plain_helper(self) -> None:
        tree = ast.parse("def helper(name):\n    pass\n")
        scope = next(s for s in build_scopes(tree).walk() if s.name == "helper")
        assert not handles_request(scope)

    def test_a_module_is_not_a_function(self) -> None:
        assert not handles_request(build_scopes(ast.parse("request = 1\n")))


class TestSharingOneAnalysis:
    def test_each_part_is_judged_separately(self) -> None:
        node, chains = last_expression(
            function("t = 'x'\nf'{t}{request.GET[\"q\"]}'"), scope_name="view"
        )
        assert isinstance(node, ast.JoinedStr)
        parts = tuple(v.value for v in node.values if isinstance(v, ast.FormattedValue))
        verdicts = [t for _, t in tainted_parts(node, parts, chains)]
        assert verdicts == [Taint.SAFE, Taint.TAINTED]

    def test_no_parts_is_no_verdicts(self) -> None:
        node, chains = last_expression("'x'\n")
        assert tainted_parts(node, (), chains) == ()
