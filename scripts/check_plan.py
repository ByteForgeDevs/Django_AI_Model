#!/usr/bin/env python
"""Fail if docs/PROJECT_PLAN.md contradicts itself, or contradicts the code.

The plan states its own phase, step, substep and rule counts in summary tables.
Those numbers were wrong the first time they were written by hand, and a plan
that miscounts itself is one nobody checks against. Every edit re-derives them
from the document body instead.

That was the whole of this script for two phases, and it was not enough. It
compared the plan against the plan: a closed loop that agreed with itself no
matter what the repository contained. It passed on every commit of Phase 2
while the progress table said Phase 2 had not started, because nothing here
had any way to know that fifteen ``DJA`` rules were registered and running. The
same blind spot let the total count ``DJS-028``, which exists only as a sentence
in Phase 4, alongside forty-five rules that exist as code.

So the checks now come in two kinds, and the second kind is the one that
matters.

*Internal* -- the document against itself:

* per-phase step and substep counts against the progress table
* the totals row
* per-family rule counts against the stated breakdown
* rule ids contiguous from 001 within each family, so a renumbering that drops
  or duplicates one is caught rather than silently shipped

*External* -- the document against ``djaudit.registry``:

* every registered rule is described somewhere in the plan
* the stated implemented count matches the number of registered rules
* a phase called *Not started* has no registered rules and no substep marked
  Done -- the exact error this script used to sail past
* a phase called *Complete* has every rule it introduces registered

A rule is attributed to the phase whose text first mentions it, which is where
that rule is specified. Later mentions are cross-references and are ignored, so
the progress table listing a phase's rules does not re-attribute them to
itself.
"""

from __future__ import annotations

import bisect
import collections
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "docs" / "PROJECT_PLAN.md"

SUBSTEP = re.compile(r"^- \*\*(\d+)\.(\d+)\.(\d+)\*\*", re.M)
STEP = re.compile(r"^### Step (\d+)\.(\d+)", re.M)
ROW = re.compile(r"^\| (\d) \| ([^|]*?) \| (\d+) \| (\d+) \| ([^|]*?) \|", re.M)
TOTAL = re.compile(r"\*\*Total\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\*")
RULE = re.compile(r"\b(DJ[SIAPMXD])-(\d{3})\b")
CLAIMED_RULES = re.compile(r"`(DJ[SIAPMXD])` (\d+)")
# Written across a line break as often as not, so the count must survive
# markdown reflow -- a checker that fails when a paragraph is rewrapped gets
# switched off.
CLAIMED_IMPLEMENTED = re.compile(r"\*\*(\d+)\s+rules\s+are\s+implemented\*\*")
PHASE_HEADING = re.compile(r"^# Phase (\d+) ", re.M)
RISK_REGISTER = re.compile(r"^## \d+\. Risk register", re.M)

# Every shape the plan uses to close out a substep: "**Done.**", "*Done.*", and
# the qualified forms such as "**Done, narrowed deliberately.**". Always
# indented, because it sits inside a substep's bullet.
DONE = re.compile(r"^\s+\*\*?Done[.,]", re.M)

NOT_STARTED = "Not started"
COMPLETE = "**Complete**"
IN_PROGRESS = "In progress"


@dataclass
class Report:
    """What the plan says, what the code says, and where they disagree."""

    problems: list[str] = field(default_factory=list)
    phases: int = 0
    steps: int = 0
    substeps: int = 0
    planned_rules: int = 0
    implemented_rules: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def introducing_phase(text: str) -> dict[str, int]:
    """Map each rule id to the number of the phase whose text first mentions it.

    Attribution stops at the risk register, the first section after the last
    phase. Without that bound a rule named only in the progress table would be
    attributed to Phase 7, purely because the table sits below Phase 7.
    """
    numbers: list[int] = []
    offsets: list[int] = []
    for match in PHASE_HEADING.finditer(text):
        numbers.append(int(match.group(1)))
        offsets.append(match.start())
    if not offsets:
        return {}

    end = RISK_REGISTER.search(text)
    limit = end.start() if end else len(text)

    intro: dict[str, int] = {}
    for match in RULE.finditer(text, offsets[0], limit):
        rule_id = f"{match.group(1)}-{match.group(2)}"
        if rule_id not in intro:
            intro[rule_id] = numbers[bisect.bisect_right(offsets, match.start()) - 1]
    return intro


def done_substeps(text: str) -> collections.Counter[str]:
    """Count, per phase, the substeps whose body carries a Done block.

    Read in one direction only. Phase 0 and the first half of Phase 1 were
    written up before the Done convention existed, so a missing marker proves
    nothing; a marker that is *present* proves the phase started, and that is
    the direction in which the progress table can be wrong.
    """
    marks = list(SUBSTEP.finditer(text))
    counts: collections.Counter[str] = collections.Counter()
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        if DONE.search(text, match.start(), end):
            counts[match.group(1)] += 1
    return counts


def _check_counts(text: str, report: Report) -> list[tuple[str, str, str]]:
    """Internal consistency. Returns the progress rows, for the later checks."""
    substeps = SUBSTEP.findall(text)
    steps = STEP.findall(text)
    per_substep = collections.Counter(phase for phase, _, _ in substeps)
    per_step = collections.Counter(phase for phase, _ in steps)

    rows = ROW.findall(text)
    for phase, title, claimed_steps, claimed_substeps, _status in rows:
        if per_step[phase] != int(claimed_steps):
            report.problems.append(
                f"phase {phase} ({title}): table claims {claimed_steps} steps, "
                f"body has {per_step[phase]}"
            )
        if per_substep[phase] != int(claimed_substeps):
            report.problems.append(
                f"phase {phase} ({title}): table claims {claimed_substeps} substeps, "
                f"body has {per_substep[phase]}"
            )

    total = TOTAL.search(text)
    if total is None:
        report.problems.append("progress table has no totals row")
    else:
        if len(steps) != int(total.group(1)):
            report.problems.append(f"totals claim {total.group(1)} steps, body has {len(steps)}")
        if len(substeps) != int(total.group(2)):
            report.problems.append(
                f"totals claim {total.group(2)} substeps, body has {len(substeps)}"
            )

    report.phases = len(rows)
    report.steps = len(steps)
    report.substeps = len(substeps)
    return [(phase, title, status) for phase, title, _, _, status in rows]


def _check_rule_ids(text: str, report: Report) -> None:
    rules = sorted(set(RULE.findall(text)))
    actual = collections.Counter(family for family, _ in rules)
    claimed = {family: int(count) for family, count in CLAIMED_RULES.findall(text)}

    for family in sorted(set(actual) | set(claimed)):
        if actual.get(family, 0) != claimed.get(family, 0):
            report.problems.append(
                f"family {family}: breakdown claims {claimed.get(family, 0)} rules, "
                f"body defines {actual.get(family, 0)}"
            )

    for family in sorted(actual):
        numbers = sorted(int(n) for f, n in rules if f == family)
        expected = list(range(1, len(numbers) + 1))
        if numbers != expected:
            missing = sorted(set(expected) - set(numbers))
            extra = sorted(set(numbers) - set(expected))
            report.problems.append(
                f"family {family} ids are not contiguous from 001: "
                f"missing {missing or 'none'}, unexpected {extra or 'none'}"
            )

    report.planned_rules = len(rules)


def _check_against_registry(
    text: str,
    implemented: set[str],
    rows: list[tuple[str, str, str]],
    report: Report,
) -> None:
    report.implemented_rules = len(implemented)

    planned = {f"{family}-{number}" for family, number in RULE.findall(text)}
    unplanned = sorted(implemented - planned)
    if unplanned:
        report.problems.append(
            f"registered but described nowhere in the plan: {', '.join(unplanned)}"
        )

    stated = CLAIMED_IMPLEMENTED.search(text)
    if stated is None:
        report.problems.append(
            "the plan never states how many rules are implemented "
            "(expected a '**N rules are implemented**' sentence)"
        )
    elif int(stated.group(1)) != len(implemented):
        report.problems.append(
            f"plan claims {stated.group(1)} rules are implemented, "
            f"the registry has {len(implemented)}"
        )

    by_phase: dict[int, set[str]] = collections.defaultdict(set)
    for rule_id, number in introducing_phase(text).items():
        by_phase[number].add(rule_id)
    done = done_substeps(text)

    for phase, title, status in rows:
        introduced = by_phase.get(int(phase), set())
        if status.startswith(NOT_STARTED):
            shipped = sorted(introduced & implemented)
            if shipped:
                report.problems.append(
                    f"phase {phase} ({title}) is marked '{NOT_STARTED}' but its rules "
                    f"are registered and running: {', '.join(shipped)}"
                )
            if done[phase]:
                report.problems.append(
                    f"phase {phase} ({title}) is marked '{NOT_STARTED}' but "
                    f"{done[phase]} of its substeps are written up as done"
                )
        elif status.startswith(COMPLETE):
            missing = sorted(introduced - implemented)
            if missing:
                report.problems.append(
                    f"phase {phase} ({title}) is marked complete but these rules are "
                    f"not registered: {', '.join(missing)}"
                )
        elif not status.startswith(IN_PROGRESS):
            report.problems.append(
                f"phase {phase} ({title}) has unrecognised status {status!r}; "
                f"expected one of {NOT_STARTED!r}, {COMPLETE!r}, {IN_PROGRESS!r}"
            )


def review(text: str, implemented: set[str]) -> Report:
    """Check the plan against itself, and against the rules that actually exist."""
    report = Report()
    rows = _check_counts(text, report)
    if not rows:
        report.problems.append("could not find the progress table")
        return report
    _check_rule_ids(text, report)
    _check_against_registry(text, implemented, rows, report)
    return report


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from djaudit import registry  # noqa: PLC0415 - deferred until sys.path is set

    implemented = {rule.meta.id for rule in registry.all_rules()}
    report = review(PLAN.read_text(encoding="utf-8"), implemented)

    if not report.ok:
        for problem in report.problems:
            print(f"PLAN INCONSISTENT: {problem}", file=sys.stderr)
        return 1

    print(
        f"plan consistent: {report.phases} phases · {report.steps} steps · "
        f"{report.substeps} substeps · {report.planned_rules} rules planned, "
        f"{report.implemented_rules} implemented"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
