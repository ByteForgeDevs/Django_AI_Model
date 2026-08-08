"""Scoring a triage model against 245 findings a human already ruled on.

This exists before anything asks a model a question, because a triage layer
that has never been scored is a triage layer nobody can argue with. The ground
truth is `benchmarks/*.json`: every finding djaudit reports on healthchecks,
netbox and pretix, each with a written verdict and a note explaining it.

**The distribution is why this module is shaped the way it is.** Of the 245
verdicts, 194 are `true_positive` and 51 are `accepted_risk`. A classifier that
says "true positive" to everything and reads nothing therefore scores 79.2%.
Any single accuracy number that a model can reach by agreeing with the majority
is not a measurement of the model, so this module does not produce one. There
is no ``accuracy`` property to reach for, and ``recall`` will not answer without
being told which class is being asked about.

The two errors are also not equally bad, which averaging would hide:

- **A downgrade** — a real defect called accepted risk — is the dangerous one.
  It is a vulnerability that a tool told someone to ignore.
- **An upgrade** — an accepted risk called a real defect — costs a reviewer
  their afternoon.

They are counted apart and reported apart, and ``beats`` requires a model to
win or tie on *both* before it is called an improvement, so a model cannot buy
its way to a better headline by trading one error for the other.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class Verdict(StrEnum):
    """What a reviewer, or a model, concluded about one finding."""

    TRUE_POSITIVE = "true_positive"
    ACCEPTED_RISK = "accepted_risk"
    # Not a verdict a human gives. A model that will not answer must be visible
    # as such, because a model that abstains on everything it finds hard would
    # otherwise post a perfect score on what is left.
    ABSTAINED = "abstained"


@dataclass(frozen=True, slots=True)
class ReviewedFinding:
    """One human verdict, and enough context to ask a model about it."""

    fingerprint: str
    rule_id: str
    verdict: Verdict
    file: str
    line: int
    note: str
    target: str

    @property
    def is_true_positive(self) -> bool:
        return self.verdict is Verdict.TRUE_POSITIVE


class Triager(Protocol):
    """Anything that will offer a verdict on a reviewed finding.

    The note is deliberately not passed to a triager anywhere in this package:
    it contains the answer.
    """

    def classify(self, finding: ReviewedFinding) -> Verdict: ...


def load_ground_truth(directory: Path) -> list[ReviewedFinding]:
    """Every verdict in every benchmark file, in a stable order."""
    findings: list[ReviewedFinding] = []
    for path in sorted(directory.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        target = str(document.get("target", path.stem))
        for entry in document["findings"]:
            findings.append(
                ReviewedFinding(
                    fingerprint=entry["fingerprint"],
                    rule_id=entry["rule_id"],
                    verdict=Verdict(entry["verdict"]),
                    file=entry["file"],
                    line=int(entry["line"]),
                    note=entry.get("note", ""),
                    target=target,
                )
            )
    if not findings:
        raise ValueError(f"no ground truth under {directory}: nothing to score against")
    return findings


@dataclass(frozen=True, slots=True)
class Score:
    """A result with no headline number.

    Every count is kept per true class, so that asking "how did it do" forces
    the asker to say which error they care about. ``as_dict`` renders both.
    """

    name: str
    # Keyed by the reviewer's verdict, then by the model's.
    matrix: dict[Verdict, dict[Verdict, int]]

    @classmethod
    def empty(cls, name: str) -> Score:
        classes = (Verdict.TRUE_POSITIVE, Verdict.ACCEPTED_RISK, Verdict.ABSTAINED)
        return cls(name=name, matrix={actual: dict.fromkeys(classes, 0) for actual in classes})

    def total(self, actual: Verdict) -> int:
        return sum(self.matrix[actual].values())

    def recall(self, actual: Verdict) -> float:
        """Of the findings a human called ``actual``, the share the model agreed on.

        There is no argument-free version of this on purpose. A caller has to
        name the class, which makes the asymmetry impossible to overlook.
        """
        seen = self.total(actual)
        if seen == 0:
            return 0.0
        return self.matrix[actual][actual] / seen

    def abstentions(self) -> int:
        return sum(row[Verdict.ABSTAINED] for row in self.matrix.values())

    def coverage(self) -> float:
        """The share it was willing to answer at all."""
        answered = sum(
            count
            for row in self.matrix.values()
            for verdict, count in row.items()
            if verdict is not Verdict.ABSTAINED
        )
        seen = sum(self.total(actual) for actual in self.matrix)
        return answered / seen if seen else 0.0

    @property
    def downgrades(self) -> int:
        """Real defects called accepted risk. The error that gets someone hurt."""
        return self.matrix[Verdict.TRUE_POSITIVE][Verdict.ACCEPTED_RISK]

    @property
    def upgrades(self) -> int:
        """Accepted risks called real defects. The error that wastes a day."""
        return self.matrix[Verdict.ACCEPTED_RISK][Verdict.TRUE_POSITIVE]

    def beats(self, baseline: Score) -> bool:
        """True only if it wins or ties on both classes, and wins on one.

        A model that trades recall on accepted risks for recall on true
        positives has not improved; it has moved. Requiring both directions is
        what stops the trade from reading as progress.
        """
        directions = (Verdict.TRUE_POSITIVE, Verdict.ACCEPTED_RISK)
        never_worse = all(self.recall(d) >= baseline.recall(d) for d in directions)
        better_somewhere = any(self.recall(d) > baseline.recall(d) for d in directions)
        return never_worse and better_somewhere

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "reviewed": sum(self.total(actual) for actual in self.matrix),
            "coverage": round(self.coverage(), 4),
            "abstained": self.abstentions(),
            "true_positives": {
                "reviewed": self.total(Verdict.TRUE_POSITIVE),
                "agreed": self.matrix[Verdict.TRUE_POSITIVE][Verdict.TRUE_POSITIVE],
                "downgraded_to_accepted_risk": self.downgrades,
                "recall": round(self.recall(Verdict.TRUE_POSITIVE), 4),
            },
            "accepted_risks": {
                "reviewed": self.total(Verdict.ACCEPTED_RISK),
                "agreed": self.matrix[Verdict.ACCEPTED_RISK][Verdict.ACCEPTED_RISK],
                "upgraded_to_true_positive": self.upgrades,
                "recall": round(self.recall(Verdict.ACCEPTED_RISK), 4),
            },
        }


def score(triager: Triager, findings: Iterable[ReviewedFinding], *, name: str) -> Score:
    result = Score.empty(name)
    for finding in findings:
        predicted = triager.classify(finding)
        result.matrix[finding.verdict][predicted] += 1
    return result


@dataclass(frozen=True, slots=True)
class AlwaysSays:
    """The floor every model has to clear.

    Saying ``true_positive`` to all 245 scores 79.2% overall while getting every
    accepted risk wrong -- which is the whole argument for reporting the two
    directions apart, made concrete enough to run.
    """

    verdict: Verdict

    def classify(self, finding: ReviewedFinding) -> Verdict:
        return self.verdict


@dataclass(frozen=True, slots=True)
class ByRule:
    """A deterministic baseline with actual information in it.

    Some rules were accepted-risk more often than not on these three targets. A
    model that cannot beat "remember which rules got waved through last time"
    is not adding judgement, and this is what makes that claim checkable rather
    than rhetorical.

    It is fitted on the same findings it is scored against, which is training
    on the test set and is meant to be. As a *baseline* that inflation is the
    point: it makes this an optimistic bound on what memorising rule ids can
    achieve, so a model that beats it is doing something memorisation cannot.
    Read as a model's own score it would mean nothing, which is why nothing
    here uses it as one.
    """

    lenient_rules: frozenset[str]

    @classmethod
    def learned_from(cls, findings: Sequence[ReviewedFinding]) -> ByRule:
        tallies: dict[str, list[int]] = {}
        for finding in findings:
            row = tallies.setdefault(finding.rule_id, [0, 0])
            row[0 if finding.is_true_positive else 1] += 1
        return cls(frozenset(rule for rule, (tp, ar) in tallies.items() if ar > tp))

    def classify(self, finding: ReviewedFinding) -> Verdict:
        if finding.rule_id in self.lenient_rules:
            return Verdict.ACCEPTED_RISK
        return Verdict.TRUE_POSITIVE


def baselines(findings: Sequence[ReviewedFinding]) -> list[Score]:
    """The scores a model is measured against, not a model's score."""
    return [
        score(AlwaysSays(Verdict.TRUE_POSITIVE), findings, name="always-true-positive"),
        score(AlwaysSays(Verdict.ACCEPTED_RISK), findings, name="always-accepted-risk"),
        score(AlwaysSays(Verdict.ABSTAINED), findings, name="always-abstain"),
        score(ByRule.learned_from(findings), findings, name="by-rule"),
        held_out_by_rule(findings),
    ]


def held_out_by_rule(findings: Sequence[ReviewedFinding]) -> Score:
    """``ByRule`` fitted on two targets and scored on the third, pooled.

    The honest version of the baseline above, and the difference between them
    is large enough to matter: fitted on its own test set the rule lookup
    reaches 87.3% on accepted risks, and held out it reaches 52.4%. The first
    number is memorisation being graded on its own homework.

    Each target is scored by a table built from the other two, and the three
    results are pooled rather than averaged, so a target with 147 findings
    weighs more than one with 34.

    A rule that only ever fires on one target is invisible to this scheme by
    construction: it is in the training set exactly for the folds that have
    none of its findings to score, and absent from the one that scores all of
    them. Because ``ByRule`` answers ``TRUE_POSITIVE`` for a rule it has not
    seen, such a rule does not merely go unscored -- every one of its findings
    is confidently wrong. That is not a flaw in the baseline, it is the cost of
    a corpus of three, and it is why this number falls when a single-target
    rule lands.
    """
    pooled = Score.empty("by-rule (held out)")
    for held in sorted({f.target for f in findings}):
        train = [f for f in findings if f.target != held]
        test = [f for f in findings if f.target == held]
        fold = score(ByRule.learned_from(train), test, name=held)
        for actual, row in fold.matrix.items():
            for predicted, count in row.items():
                pooled.matrix[actual][predicted] += count
    return pooled


def contested_rules(findings: Sequence[ReviewedFinding]) -> frozenset[str]:
    """Rules whose findings did not all get the same verdict.

    The corpus says 24 of 31 rules are unanimous: every finding that rule
    produced was judged the same way. For those, the rule id already *is* the
    verdict, and asking a model can only introduce a disagreement with a human
    who was right.

    So this is the set worth spending a call on -- and the measurement that
    makes the triage command cheap, because it is the difference between asking
    about every reviewed finding and asking about the ones where the answer was
    ever in doubt.
    """
    verdicts: dict[str, set[Verdict]] = {}
    for finding in findings:
        verdicts.setdefault(finding.rule_id, set()).add(finding.verdict)
    return frozenset(rule for rule, seen in verdicts.items() if len(seen) > 1)


def headroom(findings: Sequence[ReviewedFinding]) -> tuple[int, int]:
    """``(contested findings, total)`` -- how much a model could possibly change.

    Reported because a triage score quoted over the whole corpus is mostly a
    measurement of the unanimous rules, which need no model at all.
    """
    contested = contested_rules(findings)
    return sum(1 for f in findings if f.rule_id in contested), len(findings)
