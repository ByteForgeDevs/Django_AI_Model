"""Mutation harness: break one module, and check its tests notice.

Mutants are generated from the syntax tree rather than from a hand-written list
of substitutions. That distinction is the whole point. A hand-written list is
written by the same person who wrote the tests, so it tends to contain exactly
the mutations the tests already catch, and it reports a flattering score for a
suite with real holes in it.

A survivor is not a number to be driven to zero by deleting it. It is a claim
the tests do not check, and every one has to be read before it is believed:
some are equivalent mutants, some are unreachable, and some -- often the
boring-looking ones -- are a genuine gap. On `DJM-003` all four survivors were
message integrity, including one that blanked the ``.`` in ``shop.0002_index``
and left every substring assertion passing while producing a citation nobody
could paste back.

Usage::

    uv run python scripts/mutate.py src/djaudit/rules/some_rule.py \\
        tests/rules/test_some_rule.py

The module is rewritten in place while the harness runs and restored
afterwards, including on failure. Nothing else writes to it, so a mutated file
left behind means the process was killed; the harness checks the restore and
says so rather than leaving a corrupted tree looking clean.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FLIP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}


@dataclass(frozen=True)
class Mutant:
    label: str
    source: str


def prose_constants(tree: ast.Module) -> frozenset[int]:
    """Ids of string constants no *unit test* should be asked to kill.

    Docstrings are prose, and a ``RuleMeta``'s rationale, remediation,
    references and limitations are prose too. They are gated by
    ``gen_rule_docs.py --check``, ``check_triage.py`` and the written-sentence
    tests in ``tests/test_rule_docs.py`` -- not by behaviour.

    "Docstring" here means any bare string expression standing as a statement,
    not just the first one in a body. Attribute docstrings -- the string under
    a dataclass field -- are the common case in this codebase and are prose by
    exactly the same argument; skipping only ``body[0]`` reported every one of
    them as a survivor.

    Leaving them in would produce a survivor list two dozen entries long that
    all say "the tests do not assert the docstring", burying the handful that
    mean something. Excluding a category owned by a different gate is what
    makes the remaining survivors worth reading; excluding one owned by *no*
    gate would just be hiding.
    """
    skip: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            skip.add(id(node.value))
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "RuleMeta"
        ):
            skip.update(id(n) for n in ast.walk(node.value) if isinstance(n, ast.Constant))
    return frozenset(skip)


class Mutator(ast.NodeTransformer):
    """Applies the ``target``-th mutation it finds, and counts the rest.

    One mutation per pass, because two at once can cancel out and score as a
    kill for the wrong reason.
    """

    def __init__(self, target: int, skip: frozenset[int]) -> None:
        self.target = target
        self.skip = skip
        self.seen = 0
        self.label = ""

    def _hit(self, label: str) -> bool:
        chosen = self.seen == self.target
        if chosen:
            self.label = label
        self.seen += 1
        return chosen


class Flip(Mutator):
    """Invert decisions: comparisons, `not`, `and`/`or`, and constants."""

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in FLIP:
            replacement = FLIP[type(node.ops[0])]
            if self._hit(f"line {node.lineno}: flip {type(node.ops[0]).__name__}"):
                return ast.Compare(node.left, [replacement()], node.comparators)
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._hit(f"line {node.lineno}: drop `not`"):
            return node.operand
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        self.generic_visit(node)
        if self._hit(f"line {node.lineno}: swap conditional branches"):
            return ast.IfExp(node.test, node.orelse, node.body)
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        flipped: ast.boolop = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        if self._hit(f"line {node.lineno}: and/or"):
            return ast.BoolOp(flipped, node.values)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if id(node) in self.skip:
            return node
        if isinstance(node.value, bool) and self._hit(f"line {node.lineno}: {not node.value}"):
            return ast.Constant(not node.value)
        if (
            isinstance(node.value, str)
            and node.value
            and not node.value.isspace()
            and self._hit(f"line {node.lineno}: blank string {node.value[:30]!r}")
        ):
            return ast.Constant("")
        return node


class DropGuard(Mutator):
    """Delete an ``if ...: return`` guard, which is how a rule over-reports.

    Flipping a guard's condition and deleting it are different mutations: a
    flip can be killed by a test that only exercises one side, while deleting
    it can only be killed by a case that must *not* be reported. Controls are
    what kill these.
    """

    def visit_If(self, node: ast.If) -> ast.AST | list[ast.AST]:
        self.generic_visit(node)
        bare = len(node.body) == 1 and isinstance(node.body[0], ast.Return)
        if bare and not node.orelse and self._hit(f"line {node.lineno}: drop guard"):
            return []
        return node


def mutants(source: str) -> list[Mutant]:
    out: list[Mutant] = []
    for maker in (Flip, DropGuard):
        index = 0
        while True:
            tree = ast.parse(source)
            walker = maker(index, prose_constants(tree))
            mutated = walker.visit(tree)
            if index >= walker.seen:
                break
            ast.fix_missing_locations(mutated)
            out.append(Mutant(walker.label, ast.unparse(mutated)))
            index += 1
    return out


def check_generated(candidates: list[Mutant], original: str) -> None:
    """Prove the harness before trusting what it reports.

    A generator that emitted the original source would report a perfect score
    while testing nothing, and it would look exactly like a well-tested module.
    Unparsing normalises formatting, so the comparison is against the
    round-tripped original rather than the file on disk.
    """
    if not candidates:
        raise SystemExit(f"no mutants generated: {len(original)} characters of unmutated source")

    baseline = ast.unparse(ast.parse(original))
    identical = [m.label for m in candidates if m.source == baseline]
    if identical:
        raise SystemExit(f"harness is broken: mutants identical to the original: {identical}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Mutate a module; check its tests notice.")
    parser.add_argument("module", type=Path, help="the source file to mutate")
    parser.add_argument("tests", nargs="+", help="test paths that must kill each mutant")
    args = parser.parse_args()

    module: Path = args.module
    original = module.read_text()
    candidates = mutants(original)
    check_generated(candidates, original)
    print(f"{len(candidates)} mutants of {module}")

    survivors: list[str] = []
    try:
        for i, mutant in enumerate(candidates, 1):
            module.write_text(mutant.source)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-x",
                    "-q",
                    "--no-header",
                    "-p",
                    "no:cacheprovider",
                    *args.tests,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            killed = result.returncode != 0
            mark = "killed  " if killed else "SURVIVED"
            print(f"  [{i}/{len(candidates)}] {mark} {mutant.label}")
            if not killed:
                survivors.append(mutant.label)
    finally:
        module.write_text(original)
        if module.read_text() != original:
            print(f"\nFAILED TO RESTORE {module} -- the working tree holds a mutant")

    print(f"\n{len(candidates) - len(survivors)}/{len(candidates)} killed")
    for label in survivors:
        print(f"  SURVIVOR {label}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
