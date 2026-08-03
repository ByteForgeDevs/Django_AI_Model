#!/usr/bin/env python
"""Fail if a benchmark triage file is not a completed review.

``djaudit benchmark`` already refuses to pass with an untriaged finding, but a
verdict is only worth the reasoning behind it and nothing in the tool can tell
``"verdict": "accepted_risk"`` with three words of note from the same line with
a paragraph of evidence. The first is a rubber stamp, and a rubber stamp on a
precision benchmark is how a tool ends up with a 100% score and no credibility.
So the standard is enforced here, on our own files, rather than imposed on
anybody else's.

Checked:

* every entry carries a written justification, a reviewer and a review date
* the SHA the verdicts were written against is the SHA CI actually clones, so a
  target bump cannot silently leave every note describing a different tree
* the recorded repository matches the one in the workflow matrix
* verdicts are known values and fingerprints are unique
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = ROOT / "benchmarks"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Long enough that a verdict has to say why, short enough that it is not an
# essay quota. Every note in the tree is several times this.
MIN_NOTE = 120

VERDICTS = {"true_positive", "false_positive", "accepted_risk"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# - name: healthchecks
#   repo: healthchecks/healthchecks
#   sha: 5086d28...
MATRIX = re.compile(
    r"- name:\s*(?P<name>\S+)\s*\n\s*repo:\s*(?P<repo>\S+)\s*\n\s*sha:\s*(?P<sha>[0-9a-f]{40})",
    re.M,
)


def pinned_targets() -> dict[str, tuple[str, str]]:
    """What CI actually clones, keyed by target name."""
    text = WORKFLOW.read_text(encoding="utf-8")
    return {m["name"]: (m["repo"], m["sha"]) for m in MATRIX.finditer(text)}


def check(path: Path, pinned: dict[str, tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    raw = json.loads(path.read_text(encoding="utf-8"))
    name = path.stem

    matrix = pinned.get(name)
    if matrix is None:
        problems.append(f"{name}: no target with this name in the CI matrix")
    else:
        repo, sha = matrix
        if not raw.get("repo", "").endswith(repo):
            problems.append(f"{name}: triage repo {raw.get('repo')!r} is not CI's {repo!r}")
        if raw.get("sha") != sha:
            problems.append(
                f"{name}: verdicts were written against {raw.get('sha', '')[:12]} "
                f"but CI clones {sha[:12]} -- every note may describe a different tree"
            )

    seen: set[str] = set()
    for entry in raw.get("findings", []):
        where = f"{name}:{entry.get('rule_id', '?')} at {entry.get('file', '?')}"
        fingerprint = entry.get("fingerprint", "")

        if fingerprint in seen:
            problems.append(f"{where}: duplicate fingerprint {fingerprint}")
        seen.add(fingerprint)

        if entry.get("verdict") not in VERDICTS:
            problems.append(f"{where}: unknown verdict {entry.get('verdict')!r}")
        if len(entry.get("note", "").strip()) < MIN_NOTE:
            problems.append(f"{where}: justification is missing or too short to be a review")
        if not entry.get("reviewer", "").strip():
            problems.append(f"{where}: no reviewer recorded")
        if not DATE.match(entry.get("reviewed", "")):
            problems.append(f"{where}: no review date recorded")

    return problems


def main() -> int:
    files = sorted(BENCHMARKS.glob("*.json"))
    if not files:
        print("TRIAGE INCOMPLETE: no benchmark triage files found", file=sys.stderr)
        return 1

    pinned = pinned_targets()
    problems = [problem for path in files for problem in check(path, pinned)]

    if problems:
        for problem in problems:
            print(f"TRIAGE INCOMPLETE: {problem}", file=sys.stderr)
        return 1

    total = sum(len(json.loads(p.read_text(encoding="utf-8"))["findings"]) for p in files)
    print(f"triage complete: {len(files)} benchmarks · {total} reviewed findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
