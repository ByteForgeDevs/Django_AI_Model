"""Print the triage baselines and the headroom above them.

Run in the gate. It exists so the floor a model has to clear is a number
printed on every commit rather than a claim in a commit message, and so that a
change to the benchmark corpus that quietly makes triage trivial is noticed at
the moment it happens.

Two things it guards. A baseline that reads nothing must not do well on both
directions at once, or the corpus has stopped discriminating and nothing
measured against it means anything. And the contested set -- the findings under
a rule the reviewer did not always judge the same way -- must not shrink to
nothing, because that is the only region where a model can change an answer for
the better.
"""

from __future__ import annotations

import sys
from pathlib import Path

from djaudit.llm.evaluate import Verdict, baselines, contested_rules, headroom, load_ground_truth

ROOT = Path(__file__).resolve().parents[1]

# No baseline that reads nothing may clear this on *both* directions at once.
# Either alone is trivial: say one word and you win that class outright. The
# fitted by-rule baseline is exempt because it is deliberately trained on its
# own test set as an optimistic bound; the held-out one is the real check.
CEILING = 0.75
EXEMPT = {"by-rule"}

# Below this, the corpus is effectively decided by rule id and there is nothing
# left for judgement to do.
MINIMUM_CONTESTED = 10


def main() -> int:
    findings = load_ground_truth(ROOT / "benchmarks")
    reviewed = len(findings)
    true_positives = sum(1 for f in findings if f.is_true_positive)

    print(
        f"triage ground truth: {reviewed} reviewed findings \u00b7 "
        f"{true_positives} true positive \u00b7 {reviewed - true_positives} accepted risk"
    )
    print()
    print(f"{'baseline':24} {'TP recall':>10} {'AR recall':>10} {'downgrades':>11} {'upgrades':>9}")

    failures: list[str] = []
    for result in baselines(findings):
        tp = result.recall(Verdict.TRUE_POSITIVE)
        ar = result.recall(Verdict.ACCEPTED_RISK)
        print(
            f"{result.name:24} {tp:>9.1%} {ar:>10.1%} {result.downgrades:>11} {result.upgrades:>9}"
        )
        if result.name not in EXEMPT and min(tp, ar) > CEILING:
            failures.append(
                f"{result.name} scores {tp:.1%}/{ar:.1%} without reading anything: "
                "the corpus no longer discriminates"
            )

    contested = contested_rules(findings)
    affected, total = headroom(findings)
    rules = len({f.rule_id for f in findings})
    print()
    print(f"contested rules: {len(contested)} of {rules} ({', '.join(sorted(contested))})")
    print(
        f"headroom: {affected} of {total} findings sit under a contested rule; "
        f"the other {total - affected} are decided by rule id alone"
    )

    if len(contested) < 2 or affected < MINIMUM_CONTESTED:
        failures.append(
            f"only {affected} findings are contested: there is nothing left for a model to do"
        )

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(f"no reading-free baseline clears {CEILING:.0%} on both directions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
