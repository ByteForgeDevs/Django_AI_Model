"""The per-run caches on `ProjectContext`.

These exist for one reason -- four rules were recomputing the same three pure
functions over the same scopes, and a whole-repository run paid for each. The
tests assert *identity*, not equality: a cache that quietly stopped being used
would still return equal results, and the run would silently get slower again
with every gate still green.
"""

from __future__ import annotations

import pytest

SOURCE = {
    "shop/__init__.py": "",
    "shop/models.py": """
        from django.db import models


        class Book(models.Model):
            title = models.CharField(max_length=100)
    """,
    "shop/views.py": """
        from .models import Book


        def listing():
            books = Book.objects.all()
            return len(books)


        def other():
            return Book.objects.count()
    """,
}


@pytest.fixture
def ctx(make_project):
    return make_project(SOURCE)


def views(ctx):
    return ctx.root / "shop" / "views.py"


class TestScopeCache:
    def test_the_same_tree_is_handed_to_every_caller(self, ctx):
        assert ctx.scopes(views(ctx)) is ctx.scopes(views(ctx))

    def test_a_file_that_cannot_be_parsed_caches_its_absence(self, ctx):
        broken = ctx.root / "shop" / "broken.py"
        broken.write_text("def (:\n", encoding="utf-8")
        assert ctx.scopes(broken) is None
        assert ctx.scopes(broken) is None
        assert broken in ctx.parse_errors


class TestDefUseCache:
    def test_chains_are_built_once_per_scope(self, ctx):
        root = ctx.scopes(views(ctx))
        assert ctx.def_use(root) is ctx.def_use(root)

    def test_each_scope_gets_its_own_chains(self, ctx):
        root = ctx.scopes(views(ctx))
        nested = [scope for scope in root.walk() if scope is not root]
        assert nested, "the fixture defines two functions"
        assert all(ctx.def_use(scope) is not ctx.def_use(root) for scope in nested)


class TestTrackedCache:
    def test_queryset_values_are_resolved_once_per_scope(self, ctx):
        root = ctx.scopes(views(ctx))
        listing = next(
            scope for scope in root.walk() if getattr(scope.node, "name", "") == "listing"
        )
        assert ctx.tracked(views(ctx), listing) is ctx.tracked(views(ctx), listing)

    def test_the_cache_still_finds_the_queryset(self, ctx):
        """Identity is worthless if the cached value is empty.

        A cache returning the same wrong answer twice passes every test above,
        so this asserts the answer as well as its stability.
        """
        root = ctx.scopes(views(ctx))
        listing = next(
            scope for scope in root.walk() if getattr(scope.node, "name", "") == "listing"
        )
        models = {value.model for value in ctx.tracked(views(ctx), listing).values()}
        assert "shop.Book" in models

    def test_two_contexts_over_one_project_do_not_share(self, make_project):
        """Caches are keyed by `id`, so a stale entry would be a wrong answer.

        Each context holds its own trees and its own caches. Building the same
        project twice must give two independent sets, or an id freed by one run
        could be reused by another.
        """
        first, second = make_project(SOURCE), make_project(SOURCE)
        one = first.scopes(first.root / "shop" / "views.py")
        two = second.scopes(second.root / "shop" / "views.py")
        assert one is not two
        assert first.def_use(one) is not second.def_use(two)
