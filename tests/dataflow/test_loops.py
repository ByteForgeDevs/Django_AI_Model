"""Which variable holds a row, and which rows it holds."""

from __future__ import annotations

import ast

import pytest

from djaudit.dataflow.chains import def_use_all
from djaudit.dataflow.loops import Bind, LoopKind, find_loops
from djaudit.dataflow.scopes import build_scopes
from djaudit.graph.nodes import ManagerNode, ModelGraph, ModelNode

BOOK = "library.Book"


def graph_with_book() -> ModelGraph:
    graph = ModelGraph()
    for name in ("Author", "Book"):
        node = ModelNode(name=name, app_label="library", path=None, lineno=1, end_lineno=2)  # type: ignore[arg-type]
        node.managers["objects"] = ManagerNode(
            name="objects", manager_class="Manager", dotted="django.db.models.Manager"
        )
        graph.add(node)
    return graph


def loops_in(source: str):
    tree = ast.parse(source)
    scopes = build_scopes(tree)
    chains = def_use_all(scopes)
    graph = graph_with_book()
    found = []
    for scope in scopes.walk():
        chain = chains.get(id(scope.node))
        if chain is None:
            continue
        found.extend(find_loops(scope, chain, graph))
    return found


def only_loop(source: str):
    found = loops_in(source)
    assert len(found) == 1, [ast.unparse(loop.iterable) for loop in found]
    return found[0]


class TestThePlainCase:
    def test_a_for_over_a_manager_binds_a_row(self) -> None:
        loop = only_loop("for book in Book.objects.all():\n    print(book)\n")
        assert loop.kind is LoopKind.FOR
        assert loop.over_model == BOOK
        element = loop.element
        assert element is not None
        assert element.name == "book"
        assert element.binds is Bind.ELEMENT

    def test_it_resolves_through_assignment(self) -> None:
        loop = only_loop(
            "qs = Book.objects.all()\n"
            "qs = qs.select_related('author')\n"
            "for book in qs:\n"
            "    print(book.author)\n"
        )
        assert loop.over_model == BOOK
        element = loop.element
        assert element is not None
        assert element.spec is not None
        assert element.spec.covers("author")

    def test_an_unknown_iterable_yields_no_model(self) -> None:
        loop = only_loop("for thing in whatever():\n    print(thing)\n")
        assert loop.over_model is None
        assert loop.element is not None
        assert loop.element.binds is Bind.ELEMENT

    def test_an_async_for_is_still_a_loop(self) -> None:
        loop = only_loop(
            "async def go():\n    async for book in Book.objects.all():\n        print(book)\n"
        )
        assert loop.kind is LoopKind.ASYNC_FOR
        assert loop.over_model == BOOK


class TestWrappers:
    @pytest.mark.parametrize(
        "expr,wrapper",
        [
            ("list(Book.objects.all())", "list"),
            ("tuple(Book.objects.all())", "tuple"),
            ("set(Book.objects.all())", "set"),
            ("sorted(Book.objects.all(), key=str)", "sorted"),
            ("reversed(Book.objects.all())", "reversed"),
            ("iter(Book.objects.all())", "iter"),
        ],
    )
    def test_a_builtin_wrapper_iterates_the_same_rows(self, expr: str, wrapper: str) -> None:
        loop = only_loop(f"for book in {expr}:\n    print(book)\n")
        assert loop.over_model == BOOK, expr
        assert wrapper in loop.wrappers

    @pytest.mark.parametrize("method", ["iterator", "aiterator"])
    def test_a_row_iterator_method_stays_in_the_chain(self, method: str) -> None:
        """These are queryset methods, so they are chain steps rather than
        wrappers. Either way the loop walks rows of the model."""
        loop = only_loop(f"for book in Book.objects.all().{method}():\n    print(book)\n")
        assert loop.over_model == BOOK
        assert loop.element is not None
        assert loop.element.value is not None
        assert method in loop.element.value.methods

    def test_a_row_iterator_on_a_named_queryset_resolves(self) -> None:
        loop = only_loop("qs = Book.objects.all()\nfor book in qs.iterator():\n    print(book)\n")
        assert loop.over_model == BOOK

    def test_wrappers_nest(self) -> None:
        loop = only_loop(
            "for book in reversed(sorted(list(Book.objects.all()))):\n    print(book)\n"
        )
        assert loop.over_model == BOOK
        assert loop.wrappers == ("reversed", "sorted", "list")

    def test_a_wrapper_composes_with_assignment(self) -> None:
        loop = only_loop(
            "qs = Book.objects.select_related('author')\n"
            "rows = list(qs)\n"
            "for book in rows:\n"
            "    print(book.author)\n"
        )
        assert loop.over_model == BOOK
        assert loop.wrappers == ("list",)
        assert loop.element is not None
        assert loop.element.spec is not None
        assert loop.element.spec.covers("author")

    def test_a_call_that_is_not_row_preserving_is_not_unwrapped(self) -> None:
        loop = only_loop("for x in summarise(Book.objects.all()):\n    print(x)\n")
        assert loop.over_model is None
        assert loop.wrappers == ()

    def test_an_empty_wrapper_call_does_not_crash(self) -> None:
        loop = only_loop("for x in list():\n    print(x)\n")
        assert loop.over_model is None


class TestTupleTargets:
    def test_enumerate_puts_the_row_at_index_one(self) -> None:
        loop = only_loop("for i, book in enumerate(Book.objects.all()):\n    print(i, book)\n")
        by_name = {t.name: t for t in loop.targets}
        assert by_name["i"].binds is Bind.COUNTER
        assert by_name["i"].model is None
        assert by_name["book"].binds is Bind.ELEMENT
        assert by_name["book"].model == BOOK
        assert loop.over_model == BOOK

    def test_enumerate_with_a_start_still_works(self) -> None:
        loop = only_loop("for i, book in enumerate(Book.objects.all(), 1):\n    print(i, book)\n")
        assert loop.over_model == BOOK

    def test_enumerate_into_one_name_binds_a_tuple_not_a_row(self) -> None:
        loop = only_loop("for pair in enumerate(Book.objects.all()):\n    print(pair)\n")
        assert loop.over_model is None
        assert loop.targets[0].binds is Bind.OTHER

    def test_zip_attributes_each_position_to_its_own_iterable(self) -> None:
        loop = only_loop("for tag, book in zip(tags, Book.objects.all()):\n    print(tag, book)\n")
        by_name = {t.name: t for t in loop.targets}
        assert by_name["tag"].binds is Bind.OTHER
        assert by_name["tag"].model is None
        assert by_name["book"].binds is Bind.ELEMENT
        assert by_name["book"].model == BOOK

    def test_unpacking_a_row_never_claims_a_model(self) -> None:
        loop = only_loop("for a, b in Book.objects.values_list('id', 'title'):\n    print(a, b)\n")
        assert loop.over_model is None
        assert all(t.binds is Bind.OTHER for t in loop.targets)

    def test_a_starred_target_is_not_a_row(self) -> None:
        loop = only_loop(
            "for first, *rest in zip(Book.objects.all(), tags, more):\n    print(first, rest)\n"
        )
        by_name = {t.name: t for t in loop.targets}
        assert by_name["first"].binds is Bind.ELEMENT
        assert by_name["rest"].binds is Bind.OTHER


class TestNonInstanceRows:
    def test_values_rows_are_not_model_rows(self) -> None:
        loop = only_loop("for row in Book.objects.values('id'):\n    print(row)\n")
        assert loop.element is not None
        assert loop.element.binds is Bind.ELEMENT
        assert loop.over_model is None, "dicts have no related attributes"

    def test_values_list_rows_are_not_model_rows(self) -> None:
        loop = only_loop("for row in Book.objects.values_list('id'):\n    print(row)\n")
        assert loop.over_model is None

    def test_a_wrapped_values_call_is_still_not_a_model_row(self) -> None:
        loop = only_loop("for row in list(Book.objects.values('id')):\n    print(row)\n")
        assert loop.over_model is None


class TestNesting:
    def test_an_outer_loop_is_depth_one(self) -> None:
        loop = only_loop("for book in Book.objects.all():\n    print(book)\n")
        assert loop.depth == 1
        assert not loop.nested

    def test_an_inner_loop_is_depth_two(self) -> None:
        found = loops_in(
            "for author in Author.objects.all():\n"
            "    for book in Book.objects.all():\n"
            "        print(book)\n"
        )
        depths = {loop.element.name: loop.depth for loop in found if loop.element}
        assert depths == {"author": 1, "book": 2}
        assert next(loop for loop in found if loop.depth == 2).nested

    def test_depth_counts_through_an_if(self) -> None:
        found = loops_in(
            "for author in Author.objects.all():\n"
            "    if author.pk:\n"
            "        for book in Book.objects.all():\n"
            "            print(book)\n"
        )
        depths = {loop.element.name: loop.depth for loop in found if loop.element}
        assert depths["book"] == 2

    def test_depth_counts_through_a_try(self) -> None:
        found = loops_in(
            "for author in Author.objects.all():\n"
            "    try:\n"
            "        for book in Book.objects.all():\n"
            "            print(book)\n"
            "    except ValueError:\n"
            "        pass\n"
        )
        depths = {loop.element.name: loop.depth for loop in found if loop.element}
        assert depths["book"] == 2

    def test_a_sibling_loop_is_not_nested(self) -> None:
        found = loops_in(
            "for author in Author.objects.all():\n"
            "    print(author)\n"
            "for book in Book.objects.all():\n"
            "    print(book)\n"
        )
        depths = {loop.element.name: loop.depth for loop in found if loop.element}
        assert depths == {"author": 1, "book": 1}

    def test_a_nested_function_is_not_walked_by_the_outer_scope(self) -> None:
        source = (
            "def outer():\n"
            "    for author in Author.objects.all():\n"
            "        def inner():\n"
            "            for book in Book.objects.all():\n"
            "                print(book)\n"
        )
        tree = ast.parse(source)
        scopes = build_scopes(tree)
        chains = def_use_all(scopes)
        outer = next(s for s in scopes.walk() if s.name == "outer")
        found = find_loops(outer, chains[id(outer.node)], graph_with_book())
        assert [loop.element.name for loop in found if loop.element] == ["author"]


class TestComprehensions:
    def test_a_list_comprehension_is_a_loop(self) -> None:
        found = loops_in("names = [b.title for b in Book.objects.all()]")
        assert len(found) == 1
        assert found[0].kind is LoopKind.COMPREHENSION
        assert found[0].over_model == BOOK

    @pytest.mark.parametrize(
        "source",
        [
            "x = [b for b in Book.objects.all()]",
            "x = {b for b in Book.objects.all()}",
            "x = {b.pk: b for b in Book.objects.all()}",
            "x = (b for b in Book.objects.all())",
        ],
    )
    def test_every_comprehension_form_is_found(self, source: str) -> None:
        found = loops_in(source)
        assert len(found) == 1
        assert found[0].over_model == BOOK

    def test_a_second_generator_is_nested(self) -> None:
        found = loops_in("x = [b for a in Author.objects.all() for b in Book.objects.all()]")
        depths = {loop.element.name: loop.depth for loop in found if loop.element}
        assert depths == {"a": 1, "b": 2}

    def test_a_comprehension_inside_a_loop_is_nested(self) -> None:
        found = loops_in(
            "for author in Author.objects.all():\n"
            "    titles = [b.title for b in Book.objects.all()]\n"
        )
        depths = {loop.element.name: loop.depth for loop in found if loop.element}
        assert depths == {"author": 1, "b": 2}

    def test_a_comprehension_in_a_loop_header_is_found(self) -> None:
        found = loops_in("for x in [b for b in Book.objects.all()]:\n    print(x)\n")
        assert any(loop.kind is LoopKind.COMPREHENSION for loop in found)


class TestSlicing:
    def test_an_unsliced_loop_is_unbounded(self) -> None:
        loop = only_loop("for book in Book.objects.all():\n    print(book)\n")
        assert loop.unbounded_rows

    def test_a_sliced_loop_is_bounded(self) -> None:
        loop = only_loop("for book in Book.objects.all()[:10]:\n    print(book)\n")
        assert not loop.unbounded_rows
        assert loop.over_model == BOOK


class TestTheIteratorConflict:
    def test_prefetch_then_bare_iterator_is_a_runtime_error(self) -> None:
        loop = only_loop(
            "for book in Book.objects.prefetch_related('tags').iterator():\n    print(book)\n"
        )
        assert loop.prefetch_conflict

    def test_a_chunk_size_makes_it_legal(self) -> None:
        loop = only_loop(
            "for book in "
            "Book.objects.prefetch_related('tags').iterator(chunk_size=100):\n"
            "    print(book)\n"
        )
        assert not loop.prefetch_conflict

    def test_iterator_without_a_prefetch_is_fine(self) -> None:
        loop = only_loop("for book in Book.objects.all().iterator():\n    print(book)\n")
        assert not loop.prefetch_conflict

    def test_select_related_survives_iterator(self) -> None:
        loop = only_loop(
            "for book in Book.objects.select_related('author').iterator():\n"
            "    print(book.author)\n"
        )
        assert not loop.prefetch_conflict
        assert loop.element is not None
        assert loop.element.spec is not None
        assert loop.element.spec.covers("author")


class TestTheEmptyGraphControl:
    """Risk 13. With the model graph removed, nothing may still claim a model."""

    @pytest.mark.parametrize(
        "source",
        [
            "for book in Book.objects.all():\n    print(book)\n",
            "for i, book in enumerate(list(Book.objects.all())):\n    print(book)\n",
            "x = [b for b in Book.objects.select_related('author')]",
        ],
    )
    def test_no_model_survives_an_empty_graph(self, source: str) -> None:
        tree = ast.parse(source)
        scopes = build_scopes(tree)
        chains = def_use_all(scopes)
        for scope in scopes.walk():
            chain = chains.get(id(scope.node))
            if chain is None:
                continue
            for loop in find_loops(scope, chain, ModelGraph()):
                assert loop.over_model is None


class TestRobustness:
    @pytest.mark.parametrize(
        "source",
        [
            "for x in []:\n    pass\n",
            "for x, in Book.objects.all():\n    pass\n",
            "for a.b in Book.objects.all():\n    pass\n",
            "for x in zip():\n    pass\n",
            "for x in enumerate():\n    pass\n",
            "for a, b, c in zip(Book.objects.all()):\n    pass\n",
            "qs = qs.filter(x=1)\nfor b in qs:\n    pass\n",
            "for b in b:\n    pass\n",
            "f = lambda: [b for b in Book.objects.all()]",
            "class C:\n    rows = [b for b in Book.objects.all()]\n",
            "for x in Book.objects.all():\n    pass\nelse:\n    pass\n",
            "with open('f') as fh:\n    for b in Book.objects.all():\n        pass\n",
            "match v:\n    case 1:\n        for b in Book.objects.all():\n            pass\n",
        ],
    )
    def test_it_survives(self, source: str) -> None:
        loops_in(source)

    def test_a_self_referential_definition_terminates(self) -> None:
        loops_in(
            "qs = Book.objects.all()\n"
            "for f in filters:\n"
            "    qs = qs.filter(**f)\n"
            "for book in qs:\n"
            "    print(book)\n"
        )

    def test_a_loop_inside_a_with_is_still_found(self) -> None:
        found = loops_in(
            "with open('f') as fh:\n    for b in Book.objects.all():\n        print(b)\n"
        )
        assert [loop.over_model for loop in found] == [BOOK]

    def test_a_loop_inside_a_match_case_is_still_found(self) -> None:
        found = loops_in(
            "match v:\n    case 1:\n        for b in Book.objects.all():\n            print(b)\n"
        )
        assert [loop.over_model for loop in found] == [BOOK]

    def test_an_orelse_loop_is_found_at_the_outer_depth(self) -> None:
        found = loops_in(
            "for a in Author.objects.all():\n"
            "    pass\n"
            "else:\n"
            "    for b in Book.objects.all():\n"
            "        pass\n"
        )
        depths = {loop.element.name: loop.depth for loop in found if loop.element}
        assert depths == {"a": 1, "b": 1}


class TestALeftQuerysetIsNotRows:
    """A chain that has exited via `get()`, `first()` or `[0]` holds one
    instance or a scalar. Looping over it is not looping over rows, and saying
    otherwise is a confident wrong answer rather than a missed one."""

    @pytest.mark.parametrize(
        "expr",
        [
            "Book.objects.get(pk=1)",
            "Book.objects.first()",
            "Book.objects.all()[0]",
            "Book.objects.get(pk=1).tags",
        ],
    )
    def test_a_terminal_chain_names_no_model(self, expr: str) -> None:
        loop = only_loop(f"for x in {expr}:\n    print(x)\n")
        assert loop.over_model is None, expr

    def test_a_custom_manager_method_is_still_rows(self) -> None:
        loop = only_loop("for b in Book.objects.for_user(u):\n    print(b)\n")
        assert loop.over_model == BOOK

    def test_a_custom_queryset_method_is_still_rows(self) -> None:
        loop = only_loop("for b in Book.objects.filter(a=1).published():\n    print(b)\n")
        assert loop.over_model == BOOK
