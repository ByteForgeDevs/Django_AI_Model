#!/usr/bin/env python
"""Fail if docs/PROJECT_PLAN.md contradicts itself.

The plan states its own phase, step, substep and rule counts in summary tables.
Those numbers were wrong the first time they were written by hand, and a plan
that miscounts itself is one nobody checks against. Every edit re-derives them
from the document body instead.

Checked:

* per-phase step and substep counts against the progress table
* the totals row
* per-family rule counts against the stated breakdown
* rule ids are contiguous from 001 within each family, so a renumbering that
  drops or duplicates one is caught rather than silently shipped
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent / "docs" / "PROJECT_PLAN.md"

SUBSTEP = re.compile(r"^- \*\*(\d+)\.(\d+)\.(\d+)\*\*", re.M)
STEP = re.compile(r"^### Step (\d+)\.(\d+)", re.M)
ROW = re.compile(r"^\| (\d) \| (.*?) \| (\d+) \| (\d+) \|", re.M)
TOTAL = re.compile(r"\*\*Total\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\*")
RULE = re.compile(r"\b(DJ[SIAPMXD])-(\d{3})\b")
CLAIMED_RULES = re.compile(r"`(DJ[SIAPMXD])` (\d+)")


def main() -> int:
    text = PLAN.read_text(encoding="utf-8")
    problems: list[str] = []

    substeps = SUBSTEP.findall(text)
    steps = STEP.findall(text)
    per_substep = collections.Counter(phase for phase, _, _ in substeps)
    per_step = collections.Counter(phase for phase, _ in steps)

    rows = ROW.findall(text)
    if not rows:
        print("could not find the progress table", file=sys.stderr)
        return 2

    for phase, title, claimed_steps, claimed_substeps in rows:
        if per_step[phase] != int(claimed_steps):
            problems.append(
                f"phase {phase} ({title}): table claims {claimed_steps} steps, "
                f"body has {per_step[phase]}"
            )
        if per_substep[phase] != int(claimed_substeps):
            problems.append(
                f"phase {phase} ({title}): table claims {claimed_substeps} substeps, "
                f"body has {per_substep[phase]}"
            )

    total = TOTAL.search(text)
    if total is None:
        problems.append("progress table has no totals row")
    else:
        if len(steps) != int(total.group(1)):
            problems.append(f"totals claim {total.group(1)} steps, body has {len(steps)}")
        if len(substeps) != int(total.group(2)):
            problems.append(f"totals claim {total.group(2)} substeps, body has {len(substeps)}")

    rules = sorted(set(RULE.findall(text)))
    actual = collections.Counter(family for family, _ in rules)
    claimed = {family: int(count) for family, count in CLAIMED_RULES.findall(text)}

    for family in sorted(set(actual) | set(claimed)):
        if actual.get(family, 0) != claimed.get(family, 0):
            problems.append(
                f"family {family}: breakdown claims {claimed.get(family, 0)} rules, "
                f"body defines {actual.get(family, 0)}"
            )

    for family in sorted(actual):
        numbers = sorted(int(n) for f, n in rules if f == family)
        expected = list(range(1, len(numbers) + 1))
        if numbers != expected:
            missing = sorted(set(expected) - set(numbers))
            extra = sorted(set(numbers) - set(expected))
            problems.append(
                f"family {family} ids are not contiguous from 001: "
                f"missing {missing or 'none'}, unexpected {extra or 'none'}"
            )

    if problems:
        for problem in problems:
            print(f"PLAN INCONSISTENT: {problem}", file=sys.stderr)
        return 1

    print(
        f"plan consistent: {len(rows)} phases · {len(steps)} steps · "
        f"{len(substeps)} substeps · {len(rules)} rules"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
