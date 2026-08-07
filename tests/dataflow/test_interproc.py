"""One-hop cross-function propagation.

The failure mode this guards against is not a missed finding but a *confident
wrong one*: an argument attributed to the neighbouring parameter looks exactly
like an argument attributed correctly.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.dataflow.chains import def_use_all
from djaudit.dataflow.interproc import Budget, propagate
from djaudit.dataflow.loops import find_loops
from djaudit.dataflow.scopes import build_scopes
from djaudit.graph.nodes import ManagerNode, ModelGraph, ModelNode

BOOK = "library.Book"
AUTHOR = "library.Author"


def graph() -> ModelGraph:
    built = ModelGraph()
    for name in ("Author", "Book"):
        node = ModelNode(name=name, app_label="library", path=None, lineno=1, end_lineno=2)  # type: ignore[arg-type]
        node.managers["objects"] = ManagerNode(
            name="objects", manager_class="Manager", dotted="django.db.models.Manager"
        )
        built.add(node)
    return built


def params(source: str, *, model: ModelGraph | None = None, budget: Budget | None = None):
    """Parameter name -> resolved model label, for the whole module."""
    module = build_scopes(ast.parse(source))
    chains = def_use_all(module)
    found = propagate(module, chains, model or graph(), budget=budget)
    return {value.arg.arg: value.value.model for value in found.values()}


def loop_models(source: str) -> dict[str, str | None]:
    """Loop target name -> model, with propagation switched on."""
    module = build_scopes(ast.parse(source))
    chains = def_use_all(module)
    built = graph()
    supplied = {key: value.value for key, value in propagate(module, chains, built).items()}
    out: dict[str, str | None] = {}
    for scope in module.walk():
        chain = chains.get(id(scope.node))
        if chain is None:
            continue
        for loop in find_loops(scope, chain, built, parameters=supplied):
            element = loop.element
            if element is not None:
                out[element.name] = loop.over_model
    return out


class TestThePlainCase:
    def test_a_caller_names_the_parameter(self) -> None:
        assert params(
            "def render(books):\n"
            "    for b in books:\n"
            "        print(b)\n"
            "def view():\n"
            "    return render(Book.objects.all())\n"
        ) == {"books": BOOK}

    def test_the_loop_then_resolves(self) -> None:
        assert loop_models(
            "def render(books):\n"
            "    for b in books:\n"
            "        print(b.author)\n"
            "def view():\n"
            "    return render(Book.objects.all())\n"
        ) == {"b": BOOK}

    def test_a_keyword_argument_lands_on_its_own_parameter(self) -> None:
        assert params(
            "def render(title, books):\n"
            "    for b in books:\n"
            "        print(b)\n"
            "def view():\n"
            "    return render(books=Book.objects.all(), title='x')\n"
        ) == {"books": BOOK}

    def test_the_chain_survives_the_hop(self) -> None:
        module = build_scopes(
            ast.parse(
                "def render(books):\n"
                "    for b in books:\n"
                "        print(b)\n"
                "def view():\n"
                "    return render(Book.objects.select_related('author'))\n"
            )
        )
        chains = def_use_all(module)
        found = propagate(module, chains, graph())
        value = next(iter(found.values()))
        assert "select_related" in value.value.methods

    def test_it_counts_the_agreeing_call_sites(self) -> None:
        module = build_scopes(
            ast.parse(
                "def render(books):\n"
                "    for b in books:\n"
                "        print(b)\n"
                "def one():\n"
                "    return render(Book.objects.all())\n"
                "def two():\n"
                "    return render(Book.objects.all())\n"
            )
        )
        found = propagate(module, def_use_all(module), graph())
        assert next(iter(found.values())).call_sites == 2


class TestPositionalShifts:
    """A method's first parameter is supplied by the call, not written at it.
    Getting this wrong moves every argument by one, silently."""

    def test_self_shifts_the_arguments(self) -> None:
        assert params(
            "class View:\n"
            "    def render(self, books):\n"
            "        for b in books:\n"
            "            print(b)\n"
            "    def get(self):\n"
            "        return self.render(Book.objects.all())\n"
        ) == {"books": BOOK}

    def test_self_is_never_itself_resolved(self) -> None:
        assert "self" not in params(
            "class View:\n"
            "    def render(self, books):\n"
            "        pass\n"
            "    def get(self):\n"
            "        return self.render(Book.objects.all())\n"
        )

    def test_a_staticmethod_has_no_implicit_first(self) -> None:
        assert params(
            "class View:\n"
            "    @staticmethod\n"
            "    def render(books):\n"
            "        for b in books:\n"
            "            print(b)\n"
            "    def get(self):\n"
            "        return self.render(Book.objects.all())\n"
        ) == {"books": BOOK}

    def test_a_classmethod_still_shifts(self) -> None:
        assert params(
            "class View:\n"
            "    @classmethod\n"
            "    def render(cls, books):\n"
            "        for b in books:\n"
            "            print(b)\n"
            "    def get(self):\n"
            "        return self.render(Book.objects.all())\n"
        ) == {"books": BOOK}

    def test_the_wrong_parameter_is_not_claimed(self) -> None:
        """The precise failure a positional shift produces: the model attached
        to the neighbour of the parameter that actually received it."""
        found = params(
            "class View:\n"
            "    def render(self, before, books):\n"
            "        pass\n"
            "    def get(self):\n"
            "        return self.render(None, Book.objects.all())\n"
        )
        assert found == {"books": BOOK}
        assert "before" not in found


class TestWhereItRefuses:
    def test_no_call_site_resolves_nothing(self) -> None:
        assert params("def render(books):\n    for b in books:\n        print(b)\n") == {}

    def test_disagreeing_callers_resolve_nothing(self) -> None:
        assert (
            params(
                "def render(rows):\n"
                "    for r in rows:\n"
                "        print(r)\n"
                "def a():\n"
                "    return render(Book.objects.all())\n"
                "def b():\n"
                "    return render(Author.objects.all())\n"
            )
            == {}
        )

    def test_disagreeing_chains_resolve_nothing(self) -> None:
        """One caller prefetching and another not is exactly the case where
        choosing produces a false positive or a false negative, never a fact."""
        assert (
            params(
                "def render(rows):\n"
                "    for r in rows:\n"
                "        print(r.author)\n"
                "def a():\n"
                "    return render(Book.objects.select_related('author'))\n"
                "def b():\n"
                "    return render(Book.objects.all())\n"
            )
            == {}
        )

    def test_one_caller_passing_a_non_queryset_silences_the_rest(self) -> None:
        assert (
            params(
                "def render(rows):\n"
                "    for r in rows:\n"
                "        print(r)\n"
                "def a():\n"
                "    return render(Book.objects.all())\n"
                "def b():\n"
                "    return render(['a', 'b'])\n"
            )
            == {}
        )

    def test_a_starred_argument_is_unmappable(self) -> None:
        assert params("def render(books):\n    pass\ndef view():\n    return render(*args)\n") == {}

    def test_a_spread_before_an_argument_moves_it(self) -> None:
        """`rows` has unknown length, so the queryset's position is unknown.
        Binding it to the slot its *written* index suggests attributes it to a
        parameter that may never receive it."""
        assert (
            params(
                "def render(a, b):\n"
                "    pass\n"
                "def view():\n"
                "    return render(*rows, Book.objects.all())\n"
            )
            == {}
        )

    def test_a_star_kwargs_call_is_unmappable(self) -> None:
        assert (
            params("def render(books):\n    pass\ndef view():\n    return render(**kwargs)\n") == {}
        )

    def test_a_function_taking_star_args_is_refused(self) -> None:
        assert (
            params(
                "def render(*books):\n"
                "    pass\n"
                "def view():\n"
                "    return render(Book.objects.all())\n"
            )
            == {}
        )

    def test_two_definitions_of_a_name_are_ambiguous(self) -> None:
        assert (
            params(
                "if X:\n"
                "    def render(books):\n"
                "        pass\n"
                "else:\n"
                "    def render(books):\n"
                "        pass\n"
                "def view():\n"
                "    return render(Book.objects.all())\n"
            )
            == {}
        )

    def test_a_terminal_argument_is_not_a_queryset(self) -> None:
        assert (
            params(
                "def render(book):\n"
                "    pass\n"
                "def view():\n"
                "    return render(Book.objects.get(pk=1))\n"
            )
            == {}
        )

    def test_a_base_class_method_is_not_guessed(self) -> None:
        """The base may live in another module, so its body is not ours to
        attribute. Only methods defined in this class are targets."""
        assert (
            params(
                "class Base:\n"
                "    def render(self, books):\n"
                "        pass\n"
                "class View(Base):\n"
                "    def get(self):\n"
                "        return self.render(Book.objects.all())\n"
            )
            == {}
        )

    def test_an_unknown_keyword_is_refused(self) -> None:
        assert (
            params(
                "def render(books):\n"
                "    pass\n"
                "def view():\n"
                "    return render(rows=Book.objects.all())\n"
            )
            == {}
        )

    def test_too_many_positional_arguments_are_refused(self) -> None:
        assert (
            params(
                "def render(books):\n"
                "    pass\n"
                "def view():\n"
                "    return render(Book.objects.all(), 1, 2)\n"
            )
            == {}
        )


class TestOneHopOnly:
    def test_it_does_not_chain_through_two_functions(self) -> None:
        """`outer` is told, `inner` is not. Following the second hop is the
        unbounded walk this substep exists to avoid."""
        found = params(
            "def inner(rows):\n"
            "    for r in rows:\n"
            "        print(r)\n"
            "def outer(rows):\n"
            "    return inner(rows)\n"
            "def view():\n"
            "    return outer(Book.objects.all())\n"
        )
        assert found == {"rows": BOOK}, "only outer's parameter, not inner's"

    def test_recursion_terminates(self) -> None:
        params(
            "def render(rows):\n"
            "    return render(rows)\n"
            "def view():\n"
            "    return render(Book.objects.all())\n"
        )


class TestTheBudget:
    def test_call_sites_are_capped(self) -> None:
        calls = "\n".join(
            f"def caller{i}():\n    return render(Book.objects.all())" for i in range(20)
        )
        source = "def render(rows):\n    pass\n" + calls
        module = build_scopes(ast.parse(source))
        found = propagate(module, def_use_all(module), graph(), budget=Budget(max_call_sites=3))
        assert next(iter(found.values())).call_sites == 3

    def test_a_zero_function_budget_yields_nothing(self) -> None:
        assert (
            params(
                "def render(books):\n"
                "    pass\n"
                "def view():\n"
                "    return render(Book.objects.all())\n",
                budget=Budget(max_functions=0),
            )
            == {}
        )


class TestTheEmptyGraphControl:
    """Risk 13. With no model graph there is no queryset to propagate."""

    @pytest.mark.parametrize(
        "source",
        [
            "def render(books):\n"
            "    for b in books:\n"
            "        print(b)\n"
            "def view():\n"
            "    return render(Book.objects.all())\n",
            "class View:\n"
            "    def render(self, books):\n"
            "        pass\n"
            "    def get(self):\n"
            "        return self.render(Book.objects.all())\n",
        ],
    )
    def test_nothing_is_propagated_without_a_graph(self, source: str) -> None:
        module = build_scopes(ast.parse(source))
        assert propagate(module, def_use_all(module), ModelGraph()) == {}


class TestRobustness:
    @pytest.mark.parametrize(
        "source",
        [
            "",
            "x = 1",
            "def f():\n    pass\n",
            "def f(a, /, b, *, c):\n    pass\nf(1, 2, c=3)\n",
            "lambda x: x",
            "class C:\n    pass\n",
            "def f(a=Book.objects.all()):\n    for x in a:\n        pass\n",
            "async def f(rows):\n    pass\nasync def g():\n    await f(Book.objects.all())\n",
            "f = lambda rows: rows\nf(Book.objects.all())\n",
            "def f(rows):\n    pass\nobj.f(Book.objects.all())\n",
            "def f(rows):\n    pass\nf()\n",
        ],
    )
    def test_it_survives(self, source: str) -> None:
        module = build_scopes(ast.parse(source))
        propagate(module, def_use_all(module), graph())

    def test_a_local_shadowing_a_parameter_name_is_not_given_its_value(self) -> None:
        """`rows` is a parameter in one function and a local in another. The
        propagated value belongs only to the parameter."""
        found = loop_models(
            "def render(rows):\n"
            "    for r in rows:\n"
            "        print(r)\n"
            "def other():\n"
            "    rows = ['a']\n"
            "    for r in rows:\n"
            "        print(r)\n"
            "def view():\n"
            "    return render(Book.objects.all())\n"
        )
        assert found["r"] in {BOOK, None}

    def test_an_async_method_resolves(self) -> None:
        assert params(
            "class View:\n"
            "    async def render(self, books):\n"
            "        pass\n"
            "    async def get(self):\n"
            "        return await self.render(Book.objects.all())\n"
        ) == {"books": BOOK}
