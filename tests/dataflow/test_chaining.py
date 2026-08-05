"""Chain analysis.

Each test here encodes a piece of Django semantics where getting it wrong
produces a *confident* false positive -- the expensive kind, because the rule
will have said `firm`.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.dataflow.chaining import ALL_FORWARD, ChainSpec, analyse
from djaudit.dataflow.chains import def_use
from djaudit.dataflow.querysets import track
from djaudit.dataflow.scopes import build_scopes
from djaudit.graph.nodes import ManagerNode, ModelGraph, ModelNode


def graph_with(*labels: str) -> ModelGraph:
    graph = ModelGraph()
    for label in labels:
        app_label, name = label.split(".")
        node = ModelNode(name=name, app_label=app_label, path=None, lineno=1, end_lineno=2)  # type: ignore[arg-type]
        node.managers["objects"] = ManagerNode(
            name="objects", manager_class="Manager", dotted="django.db.models.Manager"
        )
        graph.add(node)
    return graph


def spec_for(source: str) -> ChainSpec:
    """The ChainSpec of the single queryset in `source`."""
    root = build_scopes(ast.parse(source))
    found = track(root, def_use(root), graph_with("shop.Book"))
    assert len(found) == 1, f"expected one queryset, got {len(found)}"
    return analyse(next(iter(found.values())))


class TestSelectRelated:
    def test_a_named_path_is_recorded(self):
        spec = spec_for("qs = Book.objects.select_related('author')")
        assert spec.select_related == {"author"}
        assert spec.covers("author")

    def test_a_bare_call_covers_everything(self):
        """`select_related()` follows every non-null forward relation. Treating
        it as select_related of nothing flags all of them."""
        spec = spec_for("qs = Book.objects.select_related()")
        assert ALL_FORWARD in spec.select_related
        assert spec.covers("anything")
        assert spec.covers("deeply__nested__path")

    def test_none_clears_rather_than_adds(self):
        """`select_related(None)` is a reset. Accumulating it reports the exact
        opposite of what the code does."""
        spec = spec_for("qs = Book.objects.select_related('author').select_related(None)")
        assert spec.select_related == frozenset()
        assert not spec.covers("author")

    def test_calls_accumulate(self):
        spec = spec_for("qs = Book.objects.select_related('a').select_related('b')")
        assert spec.select_related == {"a", "b"}

    def test_a_longer_path_covers_its_prefix(self):
        """`select_related('a__b')` loads `a` on the way to `b`, so flagging
        `obj.a` would be a false positive."""
        spec = spec_for("qs = Book.objects.select_related('a__b')")
        assert spec.covers("a__b")
        assert spec.covers("a")

    def test_a_prefix_does_not_cover_a_longer_path(self):
        spec = spec_for("qs = Book.objects.select_related('a')")
        assert not spec.covers("a__b")

    def test_an_unreadable_argument_is_recorded_not_ignored(self):
        """ "we saw select_related(*paths)" and "we saw no select_related" must
        not look alike to a rule choosing its confidence."""
        spec = spec_for("qs = Book.objects.select_related(*paths)")
        assert not spec.confident
        assert "select_related" in spec.unreadable


class TestPrefetchRelated:
    def test_a_string_lookup(self):
        spec = spec_for("qs = Book.objects.prefetch_related('tags')")
        assert spec.covers("tags")

    def test_a_prefetch_object_carries_the_path(self):
        """The Prefetch(...) shape is common in exactly the mature code where a
        false positive costs the most trust."""
        spec = spec_for("qs = Book.objects.prefetch_related(Prefetch('tags'))")
        assert spec.prefetch_related == {"tags"}

    def test_a_prefetch_object_with_a_queryset_argument(self):
        source = "qs = Book.objects.prefetch_related(Prefetch('tags', queryset=T.objects.all()))"
        root = build_scopes(ast.parse(source))
        found = track(root, def_use(root), graph_with("shop.Book"))
        outer = next(v for v in found.values() if v.model == "shop.Book")
        assert analyse(outer).prefetch_related == {"tags"}

    def test_a_keyword_lookup(self):
        spec = spec_for("qs = Book.objects.prefetch_related(Prefetch(lookup='tags'))")
        assert spec.prefetch_related == {"tags"}

    def test_none_clears(self):
        spec = spec_for("qs = Book.objects.prefetch_related('tags').prefetch_related(None)")
        assert not spec.covers("tags")


class TestDeferredFields:
    def test_only_defers_everything_absent(self):
        """A field left out of only() is loaded lazily, one query per row --
        the same defect as an N+1 and invisible to a select_related check."""
        spec = spec_for("qs = Book.objects.only('title')")
        assert not spec.deferred_access("title")
        assert spec.deferred_access("body")

    def test_defer_names_what_is_absent(self):
        spec = spec_for("qs = Book.objects.defer('body')")
        assert spec.deferred_access("body")
        assert not spec.deferred_access("title")

    def test_an_annotation_is_not_deferred_under_only(self):
        spec = spec_for("qs = Book.objects.only('title').annotate(n=Count('x'))")
        assert not spec.deferred_access("n")

    def test_nothing_is_deferred_by_default(self):
        spec = spec_for("qs = Book.objects.all()")
        assert not spec.deferred_access("anything")


class TestInstanceYield:
    @pytest.mark.parametrize("method", ["values", "values_list", "dates", "datetimes"])
    def test_values_stops_yielding_instances(self, method):
        """There is no attribute access on a dict, so there is no N+1 to report.
        A rule that skips this check reports N+1s on dictionaries."""
        spec = spec_for(f"qs = Book.objects.{method}('title')")
        assert not spec.yields_instances

    def test_a_plain_queryset_yields_instances(self):
        assert spec_for("qs = Book.objects.all()").yields_instances


class TestOtherSteps:
    def test_filters_are_collected_by_keyword(self):
        spec = spec_for("qs = Book.objects.filter(author__name='x', year=2020)")
        assert set(spec.filters) == {"author__name", "year"}

    def test_a_splatted_filter_is_unreadable(self):
        spec = spec_for("qs = Book.objects.filter(**request.GET)")
        assert not spec.confident

    def test_slicing_is_tracked(self):
        spec = spec_for("qs = Book.objects.all()[:10]")
        assert spec.sliced
        assert spec.yields_instances

    def test_indexing_yields_one_instance_not_a_queryset(self):
        spec = spec_for("b = Book.objects.all()[0]")
        assert spec.sliced
        assert not spec.yields_instances

    def test_ordering_and_distinct(self):
        spec = spec_for("qs = Book.objects.order_by('title').distinct()")
        assert spec.ordered
        assert spec.distinct


class TestThroughAssignment:
    def test_a_chain_split_across_statements_is_still_one_spec(self):
        """This is the shape 3.1.3 exists for: the fetch and the use are in
        different statements, which is how real code is written."""
        source = (
            "qs = Book.objects.select_related('author')\n"
            "qs = qs.prefetch_related('tags')\n"
            "for b in qs:\n"
            "    pass\n"
        )
        root = build_scopes(ast.parse(source))
        found = track(root, def_use(root), graph_with("shop.Book"))
        at_loop = next(
            v for v in found.values() if isinstance(v.node, ast.Name) and v.node.lineno == 3
        )
        spec = analyse(at_loop)
        assert spec.covers("author")
        assert spec.covers("tags")


class TestTheEmptySpec:
    def test_an_untouched_spec_covers_nothing(self):
        """The default must be 'fetched nothing', because a spec that covers by
        default silently suppresses every finding."""
        spec = ChainSpec()
        assert not spec.covers("author")
        assert not spec.fetches_anything
        assert spec.confident
        assert spec.yields_instances
