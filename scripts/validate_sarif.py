#!/usr/bin/env python
"""Validate a SARIF file against the official OASIS 2.1.0 schema.

SARIF is the whole CI integration story: if the structure drifts, GitHub code
scanning silently stops ingesting findings and nothing else fails. Unit tests
assert the fields we thought to check; this asserts conformance to the spec.

Run with the schema already downloaded so CI controls the network call:

    validate_sarif.py REPORT.sarif SCHEMA.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: validate_sarif.py REPORT.sarif SCHEMA.json", file=sys.stderr)
        return 2

    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    schema = json.loads(Path(argv[2]).read_text(encoding="utf-8"))

    errors = sorted(
        jsonschema.Draft7Validator(schema).iter_errors(report),
        key=lambda e: list(e.absolute_path),
    )
    for error in errors:
        location = "/".join(str(p) for p in error.absolute_path) or "<root>"
        print(f"SCHEMA VIOLATION {location}: {error.message}")

    run = report["runs"][0]
    rule_ids = [rule["id"] for rule in run["tool"]["driver"]["rules"]]
    print(
        f"{len(run['results'])} results · {len(rule_ids)} rule descriptors · "
        f"{len(errors)} schema violations"
    )

    # A reportingDescriptorReference that resolves to nothing is technically
    # schema-valid but semantically broken, so check it explicitly.
    dangling = _dangling_references(run, rule_ids)
    for message in dangling:
        print(f"DANGLING REFERENCE {message}")

    return 1 if errors or dangling else 0


def _dangling_references(run: dict[str, Any], rule_ids: list[str]) -> list[str]:
    problems = []
    for result in run["results"]:
        index = result.get("ruleIndex")
        if index is None or not 0 <= index < len(rule_ids):
            problems.append(f"result ruleIndex {index} out of range")
        elif rule_ids[index] != result["ruleId"]:
            problems.append(f"result {result['ruleId']} points at {rule_ids[index]}")

    for invocation in run.get("invocations", []):
        for notification in invocation.get("toolExecutionNotifications", []):
            associated = notification.get("associatedRule")
            if associated is None:
                continue
            if associated["id"] not in rule_ids:
                problems.append(f"notification rule {associated['id']} has no descriptor")
            index = associated.get("index")
            if index is not None and (
                not 0 <= index < len(rule_ids) or rule_ids[index] != associated["id"]
            ):
                problems.append(f"notification index {index} does not match {associated['id']}")
    return problems


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
