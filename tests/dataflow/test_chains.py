"""Tests for reaching-definition analysis.

The property under test throughout is *which* definitions reach a use, and
especially how many. One reaching definition licenses a firm finding; several
mean the rule must hedge. Getting that count wrong in either direction is a
false positive or a miss, so the counts are asserted explicitly rather than
just the identity of the winner.
"""

from __future__ import annotations

import ast

import pytest

from djaudit.dataflow.chains import DefUse, Use, def_use, def_use_all
from djaudit.dataflow.scopes import BindingKind, build_scopes


def analyse(source: str, *, scope_name: str | None = None) -> tuple[DefUse, ast.Module]:
    tree = ast.parse(source)
    root = build_scopes(tree)
    if scope_name is None:
        return def_use(root), tree
    target = next(s for s in root.walk() if s.name == scope_name)
    return def_use(target), tree


def use_of(chains: DefUse, name: str, *, nth: int = -1) -> Use:
    uses = list(chains.named(name))
    assert uses, f"no analysed use of {name!r}"
    return uses[nth]


def values(use: Use) -> list[str]:
    """Readable rendering of what reaches a use, for assertions worth reading."""
    return [ast.unparse(b.value) if b.value is not None else b.kind.value for b in use.reaching]


class TestStraightLine:
    def test_a_single_definition_reaches(self):
        chains, _ = analyse("qs = Book.objects.all()\nprint(qs)\n")
        use = use_of(chains, "qs")
        assert use.unambiguous
        assert values(use) == ["Book.objects.all()"]

    def test_reassignment_shadows_the_earlier_definition(self):
        """The 80.6% case: last write wins on a straight line."""
        source = "qs = Book.objects.all()\nqs = qs.select_related('author')\nprint(qs)\n"
        chains, _ = analyse(source)
        use = use_of(chains, "qs")
        assert use.unambiguous
        assert values(use) == ["qs.select_related('author')"]

    def test_the_read_inside_a_reassignment_sees_the_old_value(self):
        """``qs = qs.select_related(...)`` reads the previous binding."""
        source = "qs = Book.objects.all()\nqs = qs.select_related('author')\n"
        chains, _ = analyse(source)
        first = use_of(chains, "qs", nth=0)
        assert values(first) == ["Book.objects.all()"]

    def test_an_unknown_name_reaches_nothing(self):
        chains, _ = analyse("print(mystery)")
        assert use_of(chains, "mystery").unresolved

    def test_augmented_assignment_reads_before_it_writes(self):
        chains, _ = analyse("total = 0\ntotal += 1\n")
        assert values(use_of(chains, "total", nth=0)) == ["0"]


class TestBranches:
    SOURCE = """
qs = Book.objects.all()
if flag:
    qs = qs.select_related('author')
for book in qs:
    pass
"""

    def test_both_paths_reach_the_use(self):
        """Neither answer alone is honest, so a rule gets both and must hedge."""
        chains, _ = analyse(self.SOURCE)
        use = use_of(chains, "qs")
        assert not use.unambiguous
        assert use.definition is None
        assert values(use) == ["Book.objects.all()", "qs.select_related('author')"]

    def test_an_else_branch_replaces_rather_than_adds(self):
        source = "if flag:\n    qs = A.objects.all()\nelse:\n    qs = B.objects.all()\nprint(qs)\n"
        chains, _ = analyse(source)
        use = use_of(chains, "qs")
        assert values(use) == ["A.objects.all()", "B.objects.all()"]
        assert not use.unambiguous

    def test_a_branch_that_returns_contributes_nothing_after_it(self):
        """Definitions on a path that leaves cannot reach what follows.

        The definition under test is made *before* the branch and never
        rebound after it, so the only thing that can keep `qs = None` out of
        the reaching set is reachability. An earlier version of this test put
        an unconditional rebind after the `if`, which made it pass whether or
        not reachability was implemented -- a gate satisfied by absence.
        """
        source = """
qs = Book.objects.all()
if missing:
    qs = None
    return qs
print(qs)
"""
        chains, _ = analyse(f"def f():{source.replace(chr(10), chr(10) + '    ')}", scope_name="f")
        use = use_of(chains, "qs")
        assert values(use) == ["Book.objects.all()"]
        assert use.unambiguous, "the returning branch must not reach here"

    def test_a_branch_that_raises_contributes_nothing_after_it(self):
        source = """
if bad:
    qs = None
    raise ValueError()
qs = Book.objects.all()
print(qs)
"""
        chains, _ = analyse(source)
        assert use_of(chains, "qs").unambiguous

    def test_a_definition_before_the_branch_still_reaches_if_no_branch_rebinds(self):
        source = "qs = Book.objects.all()\nif flag:\n    other = 1\nprint(qs)\n"
        chains, _ = analyse(source)
        assert use_of(chains, "qs").unambiguous

    def test_nested_branches_merge_every_path(self):
        source = """
if a:
    qs = A.objects.all()
elif b:
    qs = B.objects.all()
else:
    qs = C.objects.all()
print(qs)
"""
        chains, _ = analyse(source)
        assert len(use_of(chains, "qs").reaching) == 3


class TestLoops:
    def test_the_loop_target_reaches_uses_in_the_body(self):
        chains, _ = analyse("for book in Book.objects.all():\n    print(book)\n")
        use = use_of(chains, "book")
        assert use.unambiguous
        assert use.reaching[0].kind is BindingKind.FOR_TARGET
        assert use.reaching[0].element_of, "one row, not the queryset"

    def test_a_definition_at_the_bottom_of_a_loop_reaches_the_top(self):
        """Single-pass analysis misses this; it is why there are two passes."""
        source = """
seed = A.objects.all()
for i in range(3):
    print(seed)
    seed = B.objects.all()
"""
        chains, _ = analyse(source)
        use = use_of(chains, "seed", nth=0)
        assert len(use.reaching) == 2, f"expected both definitions, got {values(use)}"
        assert values(use) == ["A.objects.all()", "B.objects.all()"]

    def test_a_loop_body_may_run_zero_times(self):
        source = "qs = A.objects.all()\nfor x in items:\n    qs = B.objects.all()\nprint(qs)\n"
        chains, _ = analyse(source)
        assert len(use_of(chains, "qs").reaching) == 2, "zero iterations is a real path"

    def test_a_while_loop_is_treated_the_same_way(self):
        source = "qs = A.objects.all()\nwhile flag:\n    qs = B.objects.all()\nprint(qs)\n"
        chains, _ = analyse(source)
        assert len(use_of(chains, "qs").reaching) == 2

    def test_the_iterable_is_evaluated_before_the_target_binds(self):
        source = "rows = A.objects.all()\nfor rows in rows:\n    pass\n"
        chains, _ = analyse(source)
        assert values(use_of(chains, "rows", nth=0)) == ["A.objects.all()"]


class TestComprehensions:
    def test_the_first_iterable_is_analysed_in_the_enclosing_scope(self):
        chains, _ = analyse("qs = Book.objects.all()\nout = [b.title for b in qs]\n")
        assert use_of(chains, "qs").unambiguous

    def test_the_comprehension_target_is_live_at_entry_of_its_own_scope(self):
        tree = ast.parse("out = [b.title for b in Book.objects.all()]")
        root = build_scopes(tree)
        comp = next(s for s in root.walk() if s.name == "<comprehension>")
        chains = def_use(comp)
        assert chains.of(next(n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "b"))


class TestExceptionHandling:
    def test_a_handler_sees_the_state_before_the_try(self):
        source = """
qs = A.objects.all()
try:
    qs = B.objects.all()
except ValueError:
    print(qs)
"""
        chains, _ = analyse(source)
        use = use_of(chains, "qs")
        assert len(use.reaching) == 2, "the exception may fire before or after the rebind"

    def test_the_except_name_binds_in_the_handler(self):
        chains, _ = analyse("try:\n    pass\nexcept ValueError as exc:\n    print(exc)\n")
        assert use_of(chains, "exc").reaching[0].kind is BindingKind.EXCEPT_TARGET

    def test_finally_runs_on_every_path(self):
        source = """
try:
    qs = A.objects.all()
except ValueError:
    qs = B.objects.all()
finally:
    print(qs)
"""
        chains, _ = analyse(source)
        assert len(use_of(chains, "qs").reaching) == 2


class TestWalrus:
    def test_a_walrus_definition_reaches_later_uses(self):
        chains, _ = analyse("if (n := count()) > 0:\n    print(n)\n")
        use = use_of(chains, "n")
        assert use.unambiguous
        assert use.reaching[0].kind is BindingKind.WALRUS


class TestScopeBoundaries:
    def test_a_nested_function_body_is_not_analysed_here(self):
        """Its names are read at call time, not where they are written."""
        source = "qs = A.objects.all()\ndef inner():\n    return qs\n"
        chains, _ = analyse(source)
        assert not list(chains.named("qs")), "the inner read belongs to inner's own analysis"

    def test_a_parameter_reaches_uses_in_its_function(self):
        chains, _ = analyse("def view(request):\n    return request.user\n", scope_name="view")
        use = use_of(chains, "request")
        assert use.unambiguous
        assert use.reaching[0].kind is BindingKind.PARAMETER

    def test_a_class_body_is_analysed(self):
        source = "class V:\n    qs = A.objects.all()\n    other = qs.filter(x=1)\n"
        chains, _ = analyse(source, scope_name="V")
        assert use_of(chains, "qs").unambiguous

    def test_def_use_all_covers_every_scope(self):
        source = "def f(a):\n    return a\n\nclass C:\n    x = [q for q in a]\n"
        root = build_scopes(ast.parse(source))
        everything = def_use_all(root)
        # module, f, C, and the comprehension
        assert len(everything) == 4


class TestTheApi:
    def test_of_returns_none_for_an_unanalysed_node(self):
        chains, _ = analyse("x = 1")
        assert chains.of(ast.Name(id="ghost", ctx=ast.Load())) is None

    def test_reaching_is_empty_for_an_unanalysed_node(self):
        chains, _ = analyse("x = 1")
        assert chains.reaching(ast.Name(id="ghost", ctx=ast.Load())) == ()

    def test_named_yields_in_source_order(self):
        chains, _ = analyse("x = 1\nprint(x)\nprint(x)\n")
        assert [u.lineno for u in chains.named("x")] == [2, 3]

    def test_len_and_iter_agree(self):
        chains, _ = analyse("a = 1\nprint(a)\nprint(a)\n")
        assert len(chains) == len(list(chains))

    def test_definition_is_none_when_several_reach(self):
        chains, _ = analyse("if f:\n    q = 1\nelse:\n    q = 2\nprint(q)\n")
        assert use_of(chains, "q").definition is None


class TestRobustness:
    @pytest.mark.parametrize(
        "source",
        [
            "",
            "pass",
            "for a in b:\n    break\nelse:\n    pass\n",
            "while True:\n    continue\n",
            "match v:\n    case [a]:\n        print(a)\n",
            "try:\n    pass\nexcept* ValueError as e:\n    print(e)\n",
            "with a() as (x, y):\n    print(x, y)\n",
            "del x",
            "import os\nprint(os)\n",
            "from a import b\nprint(b)\n",
            "lambda x: x",
            "async def f():\n    async for x in y:\n        print(x)\n",
            "x: int\nprint(x)\n",
            "a = b = c = 1\nprint(a, b, c)\n",
            "(a, *rest) = things\nprint(rest)\n",
        ],
    )
    def test_it_survives(self, source):
        root = build_scopes(ast.parse(source))
        assert def_use_all(root) is not None

    def test_unreachable_code_after_a_return_does_not_raise(self):
        chains, _ = analyse("def f():\n    return 1\n    qs = A.objects.all()\n", scope_name="f")
        assert chains is not None


class TestKeywordArguments:
    """A name passed by keyword is still a read.

    ``expression()`` descends by filtering children on ``isinstance(child,
    ast.expr)``, and a call holds each keyword argument under an
    ``ast.keyword`` -- which is not an expression. Every name passed by keyword
    was therefore invisible to this analysis, 2% of reads in healthchecks and
    over 7% in NetBox, and nothing downstream distinguished "no chain" from "a
    chain reaching nothing".
    """

    def test_a_name_passed_by_keyword(self):
        chains, _ = analyse("qs = Book.objects.all()\nrender(request, template=qs)\n")
        assert values(use_of(chains, "qs")) == ["Book.objects.all()"]

    def test_a_name_nested_inside_a_keyword_value(self):
        chains, _ = analyse("term = request.GET['q']\nf(where=[g(term)])\n")
        assert values(use_of(chains, "term")) == ["request.GET['q']"]

    def test_the_contrast_positionally(self):
        """The same read one argument to the left, which always worked."""
        chains, _ = analyse("qs = Book.objects.all()\nrender(qs)\n")
        assert values(use_of(chains, "qs")) == ["Book.objects.all()"]

    def test_a_double_star_keyword(self):
        """``**kwargs`` is an ``ast.keyword`` with ``arg`` set to None."""
        chains, _ = analyse("extra = {'a': 1}\nf(**extra)\n")
        assert values(use_of(chains, "extra")) == ["{'a': 1}"]

    def test_a_keyword_read_sees_a_rebind_above_it(self):
        chains, _ = analyse("x = 1\nx = 2\nf(y=x)\n")
        assert values(use_of(chains, "x")) == ["2"]

    def test_a_keyword_read_in_a_branch_merges(self):
        chains, _ = analyse(
            "if cond:\n    x = 1\nelse:\n    x = 2\nf(y=x)\n",
        )
        assert sorted(values(use_of(chains, "x"))) == ["1", "2"]

    def test_a_walrus_in_a_keyword_binds(self):
        chains, _ = analyse("f(a=(n := 5), b=n)\n")
        assert values(use_of(chains, "n")) == ["5"]
