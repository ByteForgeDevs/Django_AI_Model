"""Fail the build when an analysis run stops being fast enough.

Risk 8 claimed a ten-second budget "enforced in CI from Phase 3" for the whole
of Phase 2, and no workflow step timed anything. This is that step. It exists
because performance is the one property that degrades without anybody making a
mistake: every rule added in Phase 3 and after walks the same trees, so the
cost grows a little at a time and no single commit ever looks like the one that
broke it.

**The budget binds the slowest target, not a chosen one.** A ceiling the worst
case is allowed to exceed is not a ceiling. Each target is checked against the
same number, so the gate fails wherever the project is slowest rather than
wherever it is most convenient.

**Best of several runs, not the mean.** A shared CI runner is noisy and the
noise is one-sided -- a run can be arbitrarily slow because of a neighbour, and
cannot be faster than the work takes. The minimum is therefore the most stable
estimate of the real cost and the least likely to fail a build for someone
else's reason.

**Startup is excluded deliberately.** What is timed is the analysis, not the
interpreter: the budget is a claim about how this tool scales with the size of
a codebase, and process startup is a constant that would only dilute it.

One asymmetry is deliberate and worth stating plainly rather than dressing up.
Exceeding the budget fails the build. Beating it by a wide margin only warns,
because failing CI for being fast would punish the improvement it is meant to
encourage. That makes the slack check advisory, which this repository is
otherwise sceptical of -- so it is reported as a GitHub warning annotation and
written into the job summary, where it is visible on the pull request rather
than buried in a log nobody opens.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from djaudit import engine

TIGHTEN_BELOW = 0.5
"""Warn when the slowest run uses less than half the budget.

A budget nothing ever approaches has stopped measuring anything. Half is loose
enough that ordinary runner variance will not trip it and tight enough that a
step change in speed gets noticed while somebody still remembers causing it.
"""


@dataclass(frozen=True)
class Timing:
    """What one target cost, and whether that is acceptable."""

    name: str
    budget: float
    runs: tuple[float, ...]
    findings: int
    incomplete: tuple[str, ...]
    rule_errors: tuple[str, ...]

    @property
    def best(self) -> float:
        return min(self.runs)

    @property
    def over_budget(self) -> bool:
        return self.best > self.budget

    @property
    def headroom(self) -> float:
        """Fraction of the budget left unused, negative when over."""
        return (self.budget - self.best) / self.budget

    @property
    def should_tighten(self) -> bool:
        return self.best < self.budget * TIGHTEN_BELOW

    @property
    def partial(self) -> tuple[str, ...]:
        """Every reason this run did less work than a whole one."""
        return self.incomplete + self.rule_errors

    @property
    def ok(self) -> bool:
        return not self.over_budget and not self.partial


def measure(target: Path, budget: float, name: str, runs: int) -> Timing:
    """Time the analysis, and refuse to time one that did not happen.

    An incomplete run is fast for the worst possible reason. Without this
    check a project the tool cannot read -- settings in a class body, say --
    would post the best number on the board, and a rule that crashed on every
    module would look like an optimisation.
    """
    times: list[float] = []
    findings = 0
    incomplete: tuple[str, ...] = ()
    rule_errors: tuple[str, ...] = ()
    for _ in range(runs):
        started = time.perf_counter()
        result = engine.run(target)
        times.append(time.perf_counter() - started)
        findings = len(result.findings)
        incomplete = tuple(d.code for d in result.context.diagnostics if d.blocking)
        rule_errors = tuple(sorted(result.rule_errors))
    return Timing(name, budget, tuple(times), findings, incomplete, rule_errors)


def report(timing: Timing) -> str:
    """A one-line verdict, and the numbers behind it."""
    lines = [
        f"### timing gate — {timing.name}",
        "",
        f"| budget | best of {len(timing.runs)} | headroom | verdict |",
        "| --- | --- | --- | --- |",
    ]
    if timing.partial:
        verdict = "**FAIL** — analysis incomplete"
    elif timing.over_budget:
        verdict = "**FAIL** — over budget"
    else:
        verdict = "pass"
    lines.append(
        f"| {timing.budget:.1f}s | {timing.best:.2f}s | {timing.headroom:+.0%} | {verdict} |"
    )
    if timing.partial:
        lines += [
            "",
            f"The run reported {', '.join(timing.partial)}, so it analysed less than the "
            "whole project and its speed means nothing.",
        ]
    elif timing.should_tighten:
        lines += [
            "",
            f"`{timing.name}` now uses under half its budget. Tighten it towards "
            f"{timing.best:.1f}s, because a budget nothing approaches has stopped "
            "measuring anything.",
        ]
    lines += [
        "",
        f"All runs: {', '.join(f'{t:.2f}s' for t in timing.runs)} · {timing.findings} findings.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--name", required=True, help="target name, for the report")
    parser.add_argument(
        "--budget",
        type=float,
        required=True,
        help="seconds the analysis may take; binds every target equally",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="how many times to run; the fastest is the estimate",
    )
    parser.add_argument("--summary", type=Path, help="append a markdown report here")
    args = parser.parse_args(argv)

    if not args.target.is_dir():
        print(f"error: not a directory: {args.target}", file=sys.stderr)
        return 2
    if args.budget <= 0:
        # Not a pedantic check: a zero budget divides by zero computing
        # headroom, and a negative one would fail every run for a reason that
        # has nothing to do with the code being measured.
        print(f"error: budget must be positive, got {args.budget}", file=sys.stderr)
        return 2
    if args.runs < 1:
        print(f"error: need at least one run, got {args.runs}", file=sys.stderr)
        return 2

    timing = measure(args.target, args.budget, args.name, args.runs)
    text = report(timing)
    print(text)

    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n\n")

    if timing.partial:
        print(
            f"::error::{timing.name} did not complete "
            f"({', '.join(timing.partial)}); its timing is meaningless",
        )
        return 1
    if timing.over_budget:
        print(
            f"::error::{timing.name} took {timing.best:.2f}s against a {timing.budget:.1f}s budget",
        )
        return 1
    if timing.should_tighten:
        print(
            f"::warning::{timing.name} took {timing.best:.2f}s, under half its "
            f"{timing.budget:.1f}s budget — tighten it",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
