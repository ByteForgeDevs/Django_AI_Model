"""One pass over the project's loops, shared by every performance rule.

Four `DJP` rules ask the same first question -- which loops iterate rows of a
known model, and what did the queryset already fetch -- and answering it four
times would cost four times as much for exactly one answer.

The pass is lazy twice over. A file with no loop is never scoped, and a scope
with no loop never has def-use chains built for it, which is 91.6% of the
36,913 scopes across the three benchmark corpora. Chains are the expensive
part: building them for every scope in NetBox costs seconds, and almost all of
that work would be for scopes no performance rule can say anything about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from djaudit.dataflow.chains import DefUse, def_use
from djaudit.dataflow.loops import Loop, find_loops, scope_has_loop
from djaudit.dataflow.scopes import Scope, build_scopes

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext


@dataclass(frozen=True)
class LoopSite:
    """One loop, with everything a rule needs to report on it."""

    path: Path
    scope: Scope
    loop: Loop
    chains: DefUse
    """The owning scope's def-use chains, kept so a rule can resolve names in
    the loop body without rebuilding them."""

    @property
    def model(self) -> str | None:
        """The model whose rows this loop iterates, when that is known."""
        return self.loop.over_model

    @property
    def lineno(self) -> int:
        return self.loop.lineno


def build_loop_inventory(ctx: ProjectContext) -> tuple[LoopSite, ...]:
    """Every loop in the project, in file order."""
    from djaudit.graph.builder import app_label_for  # noqa: PLC0415  (cycle)

    graph = ctx.model_graph
    found: list[LoopSite] = []
    for path in ctx.python_files:
        source = ctx.source(path)
        if source is None or "for" not in source:
            # Every loop Python has -- `for`, `async for`, and all four
            # comprehensions -- is written with the `for` keyword, so a file
            # whose text does not contain it cannot contain a loop. Checking
            # the text costs a substring search against a parse of a file
            # nothing here could ever report on, which is roughly half of a
            # large project.
            continue
        tree = ctx.parse(path)
        if tree is None:
            continue
        module = build_scopes(tree)
        label: str | None = None
        for scope in module.walk():
            if not scope_has_loop(scope):
                continue
            if label is None:
                # Deferred until a scope is known to be worth reading, because
                # resolving an app label walks the filesystem looking for the
                # application root.
                label = app_label_for(path, ctx)
            chains = def_use(scope)
            for loop in find_loops(scope, chains, graph, app_label=label):
                found.append(LoopSite(path=path, scope=scope, loop=loop, chains=chains))
    return tuple(found)
