"""Compare a run against a benchmark repository with its recorded verdicts.

This is the precision half of the evaluation strategy. Mature open-source Django
projects measure precision and crash-resistance: they are well audited, so most
of what we report on them is noise, and anything that crashes on 1,200 files
would crash on a user's repository too. They cannot measure recall -- we have no
way to know what they contain that we missed. That is what the planted-defect
fixtures are for.

The gate fails on three distinct conditions, each catching a different way the
tool can quietly get worse:

``untriaged``
    A finding nobody has judged. Either a new rule fired or an existing one
    changed behaviour. Both need a human to look before the build is green.

``false positive rate``
    Measured per family, because families fail differently -- an N+1 heuristic
    and a settings literal check do not deserve the same budget.

``regressed``
    A finding previously judged real that has stopped firing. Ordinary
    regression tests cannot catch this, because the defect lives in somebody
    else's repository.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from djaudit import engine
from djaudit.engine import RunResult
from djaudit.models import Confidence, Finding, Severity
from djaudit.triage import Triage, TriageEntry, Verdict


@dataclass(frozen=True, slots=True)
class FamilyScore:
    """Precision for one rule family on one benchmark repository."""

    family: str
    true_positives: int
    false_positives: int
    accepted_risks: int

    @property
    def reported(self) -> int:
        return self.true_positives + self.false_positives + self.accepted_risks

    @property
    def correct(self) -> int:
        return self.true_positives + self.accepted_risks

    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / self.reported if self.reported else 0.0

    @property
    def precision(self) -> float:
        return self.correct / self.reported if self.reported else 1.0


@dataclass(slots=True)
class BenchmarkReport:
    """What the benchmark found, and whether that is acceptable."""

    target: str
    python_files: int
    reported: int
    untriaged: list[Finding] = field(default_factory=list)
    regressed: list[TriageEntry] = field(default_factory=list)
    resolved: list[TriageEntry] = field(default_factory=list)
    scores: list[FamilyScore] = field(default_factory=list)
    rule_errors: dict[str, str] = field(default_factory=dict)
    parse_errors: dict[str, str] = field(default_factory=dict)
    max_false_positive_rate: float = 0.0

    @property
    def over_budget(self) -> list[FamilyScore]:
        return [s for s in self.scores if s.false_positive_rate > self.max_false_positive_rate]

    @property
    def precision(self) -> float:
        reported = sum(s.reported for s in self.scores)
        return sum(s.correct for s in self.scores) / reported if reported else 1.0

    @property
    def scored(self) -> int:
        """Findings carrying a verdict. Precision is meaningless without these."""
        return sum(s.reported for s in self.scores)

    @property
    def precision_display(self) -> str:
        # Reporting "100%" when nothing has been judged invites the reader to
        # conclude the tool is perfect, on a run that may be entirely untriaged.
        return f"{self.precision:.1%}" if self.scored else "not measured"

    @property
    def ok(self) -> bool:
        return not (self.untriaged or self.regressed or self.over_budget or self.rule_errors)

    def summary(self) -> str:
        return (
            f"{self.target}: {self.python_files} files · {self.reported} reported · "
            f"precision {self.precision_display} · {len(self.untriaged)} untriaged · "
            f"{len(self.regressed)} regressed · {len(self.rule_errors)} rule errors"
        )


def compare(result: RunResult, triage: Triage) -> BenchmarkReport:
    """Score a run against recorded verdicts."""
    verdicts = triage.by_fingerprint
    seen: set[str] = set()

    counts: dict[str, Counter[Verdict]] = defaultdict(Counter)
    untriaged: list[Finding] = []

    for finding in result.findings:
        seen.add(finding.fingerprint)
        entry = verdicts.get(finding.fingerprint)
        if entry is None:
            untriaged.append(finding)
            continue
        counts[finding.family.value][entry.verdict] += 1

    # A verdict whose finding no longer appears. Only correct detections count
    # as a regression -- a false positive that stopped firing is a fix, not a
    # loss, and reporting it as one would discourage exactly the right work.
    regressed = [
        entry
        for entry in triage.entries
        if entry.fingerprint not in seen and entry.verdict.is_correct_detection
    ]
    resolved = [
        entry
        for entry in triage.entries
        if entry.fingerprint not in seen and not entry.verdict.is_correct_detection
    ]

    scores = [
        FamilyScore(
            family=family,
            true_positives=tally[Verdict.TRUE_POSITIVE],
            false_positives=tally[Verdict.FALSE_POSITIVE],
            accepted_risks=tally[Verdict.ACCEPTED_RISK],
        )
        for family, tally in sorted(counts.items())
    ]

    return BenchmarkReport(
        target=triage.target,
        python_files=len(result.context.python_files),
        reported=len(result.findings),
        untriaged=untriaged,
        regressed=sorted(regressed, key=lambda e: (e.file, e.line, e.rule_id)),
        resolved=sorted(resolved, key=lambda e: (e.file, e.line, e.rule_id)),
        scores=scores,
        rule_errors=dict(result.rule_errors),
        parse_errors={result.context.rel(p): msg for p, msg in result.context.parse_errors.items()},
        max_false_positive_rate=triage.max_false_positive_rate,
    )


def run_benchmark(root: Path, triage_path: Path) -> BenchmarkReport:
    """Audit ``root`` at the widest thresholds and score it against its triage.

    Thresholds are deliberately wide open. A benchmark that only looks at what
    the default thresholds surface would let a low-confidence rule accumulate
    false positives unseen, and then promote them the day somebody raises its
    confidence.
    """
    triage = Triage.load(triage_path)
    result = engine.run(root, min_severity=Severity.INFO, min_confidence=Confidence.TENTATIVE)
    return compare(result, triage)
