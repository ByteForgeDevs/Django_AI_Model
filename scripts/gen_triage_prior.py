"""Regenerate `llm/triage.py`'s corpus prior from the recorded verdicts.

The prior is a summary of `benchmarks/*.json`, and a summary that drifts from
what it summarises is worse than no summary: it would keep suppressing
questions on the authority of verdicts that have since been revised.

`--check` runs in CI and fails if the constant no longer matches the corpus, so
retriaging a finding cannot silently leave a stale table behind.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from djaudit.llm.evaluate import Verdict, load_ground_truth
from djaudit.llm.triage import CORPUS_PRIOR, MINIMUM_OBSERVATIONS

ROOT = Path(__file__).resolve().parent.parent


def derive(directory: Path, minimum: int) -> dict[str, tuple[Verdict, int]]:
    """Unanimous rules with enough observations to stand in for a reviewer."""
    tallies: dict[str, Counter[Verdict]] = {}
    for finding in load_ground_truth(directory):
        tallies.setdefault(finding.rule_id, Counter())[finding.verdict] += 1
    return {
        rule: (next(iter(seen)), sum(seen.values()))
        for rule, seen in sorted(tallies.items())
        if len(seen) == 1 and sum(seen.values()) >= minimum
    }


def render(prior: dict[str, tuple[Verdict, int]]) -> str:
    lines = [f'    "{rule}": (Verdict.{v.name}, {n}),' for rule, (v, n) in sorted(prior.items())]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the constant is stale.")
    parser.add_argument("--benchmarks", type=Path, default=ROOT / "benchmarks")
    args = parser.parse_args()

    derived = derive(args.benchmarks, MINIMUM_OBSERVATIONS)

    if not args.check:
        print(f"CORPUS_PRIOR: dict[str, tuple[Verdict, int]] = {{\n{render(derived)}\n}}")
        return 0

    if derived == CORPUS_PRIOR:
        covered = sum(n for _, n in derived.values())
        print(
            f"triage prior current: {len(derived)} rules covering {covered} reviewed "
            f"findings, each unanimous over at least {MINIMUM_OBSERVATIONS}"
        )
        return 0

    print("triage prior is stale: llm/triage.py does not match benchmarks/", file=sys.stderr)
    for rule in sorted(set(derived) | set(CORPUS_PRIOR)):
        was, now = CORPUS_PRIOR.get(rule), derived.get(rule)
        if was != now:
            print(f"  {rule}: shipped {was} but the corpus says {now}", file=sys.stderr)
    print(f"\nregenerate with: uv run python {Path(__file__).name}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
