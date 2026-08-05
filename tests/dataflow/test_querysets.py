"""Queryset origin tracking.

Every source shape here is one that occurs in the benchmark targets; where a
test encodes a limit rather than a capability it says so, because a limit
nobody wrote down gets "fixed" into a false positive later.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.dataflow.chains import def_use
from djaudit.dataflow.querysets import Origin, QuerysetTracker, QuerysetValue, track
from djaudit.dataflow.scopes import build_scopes
from djaudit.graph.nodes import ManagerNode, ModelGraph, ModelNode, RelationEdge


def graph_with(*models: str, managers: dict[str, list[str]] | None = None) -> ModelGraph:
    """A graph holding just the models a test needs."""
    graph = ModelGraph()
    managers = managers or {}
    for label in models:
        app_label, name = label.split(".")
        node = ModelNode(name=name, app_label=app_label, path=None, lineno=1, end_lineno=2)  # type: ignore[arg-type]
        for manager in managers.get(label, ["objects"]):
            node.managers[manager] = ManagerNode(
                name=manager, manager_class="Manager", dotted="django.db.models.Manager"
            )
        graph.add(node)
    return graph


def analyse(
    source: str, graph: ModelGraph, *, scope_name: str | None = None
) -> tuple[dict[int, QuerysetValue], ast.Module]:
    tree = ast.parse(source)
    root = build_scopes(tree)
    scope = root if scope_name is None else next(s for s in root.walk() if s.name == scope_name)
    return track(scope, def_use(scope), graph), tree


def only(found: dict[int, QuerysetValue]) -> QuerysetValue:
    assert len(found) == 1, f"expected exactly one queryset, got {len(found)}"
    return next(iter(found.values()))


class TestOrigins:
    def test_a_plain_manager_query(self):
        found, _ = analyse("qs = Book.objects.all()", graph_with("shop.Book"))
        value = only(found)
        assert value.origin is Origin.MANAGER
        assert value.model == "shop.Book"
        assert value.manager == "objects"
        assert value.chain == ("all",)

    def test_the_default_manager(self):
        found, _ = analyse("qs = Book._default_manager.all()", graph_with("shop.Book"))
        value = only(found)
        assert value.origin is Origin.DEFAULT_MANAGER
        assert value.model == "shop.Book"

    def test_a_declared_custom_manager(self):
        graph = graph_with("shop.Book", managers={"shop.Book": ["objects", "published"]})
        found, _ = analyse("qs = Book.published.filter(x=1)", graph)
        value = only(found)
        assert value.manager == "published"
        assert value.chain == ("filter",)

    def test_an_attribute_that_is_not_a_manager_is_not_a_queryset(self):
        """`Book.DoesNotExist` is not a query, and treating it as one would put
        a DJP finding on an exception class."""
        found, _ = analyse("x = Book.DoesNotExist", graph_with("shop.Book"))
        assert found == {}

    def test_a_name_that_is_not_a_model_is_not_a_queryset(self):
        found, _ = analyse("qs = Widget.objects.all()", graph_with("shop.Book"))
        assert found == {}

    def test_self_get_queryset_is_recognised_without_a_model(self):
        source = "class V:\n    def get(self):\n        qs = self.get_queryset()\n"
        found, _ = analyse(source, graph_with("shop.Book"), scope_name="get")
        value = only(found)
        assert value.origin is Origin.SELF
        assert value.model is None, "the view's model is not visible from here"

    def test_super_get_queryset_is_recognised(self):
        source = "class V:\n    def get_queryset(self):\n        qs = super().get_queryset()\n"
        found, _ = analyse(source, graph_with("shop.Book"), scope_name="get_queryset")
        assert only(found).origin is Origin.SELF


class TestFollowingAssignment:
    def test_one_hop(self):
        source = "qs = Book.objects.all()\nfor b in qs:\n    pass\n"
        found, _ = analyse(source, graph_with("shop.Book"))
        use = next(v for v in found.values() if isinstance(v.node, ast.Name) and v.node.lineno == 2)
        assert use.model == "shop.Book"
        assert use.indirect

    def test_two_hops_accumulate_the_chain(self):
        source = "qs = Book.objects.all()\nqs2 = qs.select_related('a')\nfor b in qs2:\n    pass\n"
        found, _ = analyse(source, graph_with("shop.Book"))
        use = next(v for v in found.values() if isinstance(v.node, ast.Name) and v.node.lineno == 3)
        assert use.model == "shop.Book"
        assert use.chain == ("all", "select_related")
        assert len(use.via) == 2, "two assignments were walked through"

    def test_an_ambiguous_definition_stops_resolution(self):
        """Two definitions reach, one prefetched and one not. Picking either is
        a guess, and both guesses are wrong on some path."""
        source = (
            "qs = Book.objects.all()\n"
            "if flag:\n"
            "    qs = Book.objects.select_related('a')\n"
            "for b in qs:\n"
            "    pass\n"
        )
        found, _ = analyse(source, graph_with("shop.Book"))
        assert not [
            v for v in found.values() if isinstance(v.node, ast.Name) and v.node.lineno == 4
        ]

    def test_a_parameter_is_not_followed(self):
        """A queryset handed in is unknowable without interprocedural analysis,
        which 3.1.6 bounds deliberately."""
        source = "def f(qs):\n    for b in qs:\n        pass\n"
        found, _ = analyse(source, graph_with("shop.Book"), scope_name="f")
        assert found == {}

    def test_a_walrus_definition_is_followed(self):
        source = "if (qs := Book.objects.all()):\n    for b in qs:\n        pass\n"
        found, _ = analyse(source, graph_with("shop.Book"))
        assert any(v.model == "shop.Book" and isinstance(v.node, ast.Name) for v in found.values())


class TestSelfReference:
    def test_accumulating_in_a_loop_terminates(self):
        """`qs = qs.filter(...)` makes a definition depend on itself. Without a
        guard this recurses until the stack ends."""
        source = "qs = Book.objects.all()\nfor f in fs:\n    qs = qs.filter(**f)\n"
        found, _ = analyse(source, graph_with("shop.Book"))
        assert found is not None

    def test_the_ambiguous_use_inside_the_loop_is_not_claimed(self):
        source = "qs = Book.objects.all()\nfor f in fs:\n    qs = qs.filter(**f)\n"
        found, _ = analyse(source, graph_with("shop.Book"))
        inside = [v for v in found.values() if isinstance(v.node, ast.Name) and v.node.lineno == 3]
        assert inside == [], "two definitions reach qs there, so we say nothing"


class TestChainShape:
    def test_only_the_outermost_expression_is_reported(self):
        """A rule wants the whole chain. Reporting each prefix would make one
        query look like four."""
        found, _ = analyse(
            "qs = Book.objects.filter(x=1).select_related('a')", graph_with("shop.Book")
        )
        value = only(found)
        assert value.chain == ("filter", "select_related")

    def test_a_terminal_call_is_marked(self):
        found, _ = analyse("b = Book.objects.filter(x=1).first()", graph_with("shop.Book"))
        value = only(found)
        assert value.terminal
        assert value.chain == ("filter", "first")

    def test_an_unknown_method_breaks_the_chain(self):
        """`.render()` is not a queryset method. `qs` is still a queryset, but
        `qs.render()` is not one, and continuing through it would claim a model
        for an object that no longer has rows."""
        source = "qs = Book.objects.all()\nx = qs.render()\n"
        found, _ = analyse(source, graph_with("shop.Book"))
        at_line_2 = [v for v in found.values() if v.node.lineno == 2]
        assert all(isinstance(v.node, ast.Name) for v in at_line_2)
        assert not any("render" in v.chain for v in found.values())


class TestRelatedAccessors:
    def test_a_reverse_accessor_resolves_through_the_graph(self):
        graph = graph_with("shop.Book", "shop.Author")
        graph.incoming["shop.Author"] = [
            RelationEdge(
                source="shop.Book",
                field_name="author",
                kind="ForeignKey",
                target_ref="Author",
                lineno=1,
                end_lineno=1,
                target="shop.Author",
                accessor="books",
            )
        ]
        tracker = QuerysetTracker(
            build_scopes(ast.parse("")), def_use(build_scopes(ast.parse(""))), graph
        )
        assert tracker.related_model("shop.Author", "books") == "shop.Book"

    def test_an_unknown_accessor_resolves_to_nothing(self):
        graph = graph_with("shop.Author")
        tracker = QuerysetTracker(
            build_scopes(ast.parse("")), def_use(build_scopes(ast.parse(""))), graph
        )
        assert tracker.related_model("shop.Author", "nope") is None


class TestTheEmptyGraphControl:
    """Run the detector with its real signal removed and count what survives.

    With no models in the graph, every `Model.objects...` origin is by
    construction unrecognisable, so anything still detected is noise. This is
    how the first version's `Origin.SELF` rule was caught claiming ordinary
    method calls -- `self.get(...)` on a view, `self.update()` on a form --
    as querysets. Measured only against a populated graph it was invisible,
    buried under thousands of true positives.
    """

    ORDINARY = """
class View:
    def post(self, request):
        obj = self.get(pk=1)
        self.update(request.data)
        n = self.count()
        self.delete()
        self.all()
        form.filter(x=1)
        return obj
"""

    def test_ordinary_method_calls_are_not_querysets(self):
        found, _ = analyse(self.ORDINARY, ModelGraph(), scope_name="post")
        assert found == {}, f"detected {len(found)} querysets where no model exists"

    def test_the_control_still_holds_with_models_present(self):
        """The same source is still silent when the graph is full, so the
        detections above were noise rather than a resolution failure."""
        found, _ = analyse(self.ORDINARY, graph_with("shop.Book"), scope_name="post")
        assert found == {}

    def test_get_queryset_is_the_deliberate_exception(self):
        source = "class V:\n    def post(self):\n        return self.get_queryset()\n"
        found, _ = analyse(source, ModelGraph(), scope_name="post")
        assert only(found).origin is Origin.SELF


class TestRobustness:
    @pytest.mark.parametrize(
        "source",
        [
            "",
            "qs = Book.objects",
            "qs = Book().objects.all()",
            "qs = (Book).objects.all()",
            "qs = Book.objects.all()[0]",
            "qs = [Book.objects.all()]",
            "qs = {'a': Book.objects.all()}",
            "qs = Book.objects.all() if x else Book.objects.none()",
            "async def f():\n    qs = [b async for b in Book.objects.all()]\n",
            "qs = obj.a.b.c.d.e.f()",
            "del Book",
        ],
    )
    def test_it_survives(self, source):
        found, _ = analyse(source, graph_with("shop.Book"))
        assert found is not None

    def test_a_bare_manager_with_no_chain_is_still_a_queryset(self):
        found, _ = analyse("qs = Book.objects", graph_with("shop.Book"))
        value = only(found)
        assert value.model == "shop.Book"
        assert value.chain == ()
