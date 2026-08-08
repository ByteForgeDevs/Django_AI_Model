"""Fail when the live suite ran nothing, or ran less than all of it.

Every test in `tests/live` carries `skipif(not DJAUDIT_TEST_POSTGRES)`, which
is right for a laptop and dangerous in CI: a typo in the env block, a service
container that never came up, a renamed variable, and the job goes green having
run nothing. pytest reports that as `0 failed` in exactly the same words as a
job that ran everything.

So the CI job asserts three things this script can see and pytest's exit code
cannot: that tests were collected at all, that none of them skipped, and that
the ones which skipped did not do so for the one reason that means the gate is
switched off.

Reads the JUnit XML rather than the terminal summary, because that summary is
prose and has changed shape between pytest releases.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

#: Below this, something collected wrongly rather than the suite shrinking.
FLOOR = 100


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: live_gate.py <junit.xml>", file=sys.stderr)
        return 2

    report = Path(argv[1])
    if not report.is_file():
        print(f"no report at {report}: pytest did not get far enough to write one")
        return 1

    root = ElementTree.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)

    total = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failures = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)

    print(f"live tier: {total} tests, {skipped} skipped, {failures} failed")

    if total == 0:
        print("nothing ran. A gate with nothing to check is not a gate.")
        return 1

    if total < FLOOR:
        print(f"only {total} tests collected, expected at least {FLOOR}.")
        return 1

    if skipped:
        for case in root.iter("testcase"):
            for skip in case.iter("skipped"):
                name = f"{case.get('classname', '')}::{case.get('name', '')}"
                print(f"  skipped {name}: {skip.get('message', '')}")
        print("A skipped live test is an unverified claim about PostgreSQL.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
