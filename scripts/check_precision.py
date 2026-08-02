#!/usr/bin/env python
"""Fail if an audit of a known-clean repository reported anything.

Used by the precision gate in CI. The targets are mature, well-audited Django
projects, so any finding is a false positive by construction, and any rule crash
is a robustness bug. Both must fail the build.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_precision.py REPORT.json", file=sys.stderr)
        return 2

    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    findings = report["findings"]
    rule_errors = report["rule_errors"]
    parse_errors = report["parse_errors"]
    project = report["project"]

    print(
        f"{project['python_files']} python files  ·  "
        f"{len(findings)} findings  ·  "
        f"{len(rule_errors)} rule errors  ·  "
        f"{len(parse_errors)} parse errors"
    )

    failed = False

    for finding in findings:
        location = finding["location"]
        print(
            f"FALSE POSITIVE {finding['rule_id']} "
            f"{location['file']}:{location['line']} -- {finding['message']}"
        )
        failed = True

    for rule_id, error in sorted(rule_errors.items()):
        print(f"RULE CRASHED {rule_id}: {error}")
        failed = True

    # Parse failures are reported but not fatal: real repositories legitimately
    # contain deliberately broken files inside their own test suites.
    for path, error in list(parse_errors.items())[:10]:
        print(f"note: could not parse {path}: {error}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
