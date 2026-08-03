"""Evaluation harness: measure precision and recall against a manifest.

The reason this exists in Phase 0, before the rule catalogue, is that rules
interact. Rule 47 will silently break rule 12's grading, and without a scored
regression gate nobody notices until a user does.

A manifest is an ``expected.json`` beside a project::

    {
      "description": "why this fixture exists",
      "expected": [
        {"rule_id": "DJS-001", "file": "config/settings/production.py",
         "line": 9, "severity": "critical", "confidence": "certain"}
      ],
      "must_not_report": [
        {"rule_id": "DJS-001", "file": "config/settings/development.py"}
      ]
    }

``must_not_report`` entries are control cases -- code that looks like a defect
but is not. Hitting one is a hard failure regardless of the precision score,
because those are exactly the false positives that get a tool uninstalled.

Two conditions fail a fixture without being about any expectation at all: a
blocking diagnostic, and a rule that crashed. Both mean part of the catalogue
never ran, and a rule that never ran satisfies every ``must_not_report`` entry
in the manifest by doing nothing. On a fixture that is mostly control cases
that reads as a pass. Scoring silence is the whole thing this harness exists to
prevent, so it refuses to score a run it knows was incomplete.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from djaudit import engine
from djaudit.context import Diagnostic
from djaudit.models import Confidence, Finding, Severity

MANIFEST_NAME = "expected.json"


class ManifestError(Exception):
    """Raised when a manifest is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Expectation:
    """One expected finding. ``None`` fields are wildcards."""

    rule_id: str
    file: str
    line: int | None = None
    severity: Severity | None = None
    confidence: Confidence | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Expectation:
        try:
            return cls(
                rule_id=str(raw["rule_id"]),
                file=str(raw["file"]),
                line=int(raw["line"]) if raw.get("line") is not None else None,
                severity=Severity(raw["severity"]) if raw.get("severity") else None,
                confidence=Confidence(raw["confidence"]) if raw.get("confidence") else None,
            )
        except (KeyError, ValueError) as exc:
            raise ManifestError(f"invalid expectation {raw!r}: {exc}") from exc

    def locates(self, finding: Finding) -> bool:
        """Whether this expectation points at the same place as ``finding``."""
        if finding.rule_id != self.rule_id or finding.location.file != self.file:
            return False
        return self.line is None or finding.location.line == self.line

    def grades(self, finding: Finding) -> bool:
        """Whether severity and confidence match, where specified."""
        if self.severity is not None and finding.severity is not self.severity:
            return False
        return not (self.confidence is not None and finding.confidence is not self.confidence)

    def describe(self) -> str:
        where = f"{self.file}:{self.line}" if self.line is not None else self.file
        return f"{self.rule_id} at {where}"


@dataclass(frozen=True, slots=True)
class Manifest:
    expected: tuple[Expectation, ...]
    must_not_report: tuple[Expectation, ...]
    description: str = ""

    @classmethod
    def load(cls, path: Path) -> Manifest:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ManifestError(f"manifest not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest is not valid JSON: {path}: {exc}") from exc
        return cls(
            expected=tuple(Expectation.from_dict(e) for e in raw.get("expected", [])),
            must_not_report=tuple(Expectation.from_dict(e) for e in raw.get("must_not_report", [])),
            description=str(raw.get("description", "")),
        )


@dataclass
class EvalReport:
    """Scored outcome of running the analyser against a manifest."""

    project: Path
    matched: list[tuple[Expectation, Finding]] = field(default_factory=list)
    misgraded: list[tuple[Expectation, Finding]] = field(default_factory=list)
    missing: list[Expectation] = field(default_factory=list)
    unexpected: list[Finding] = field(default_factory=list)
    forbidden: list[Finding] = field(default_factory=list)
    incomplete: list[Diagnostic] = field(default_factory=list)
    """Blocking diagnostics: rule families that never ran on this fixture."""

    rule_errors: dict[str, str] = field(default_factory=dict)
    """Rules that crashed. Their silence is not a passed control case."""

    @property
    def true_positives(self) -> int:
        return len(self.matched)

    @property
    def false_positives(self) -> int:
        return len(self.unexpected) + len(self.forbidden)

    @property
    def false_negatives(self) -> int:
        return len(self.missing) + len(self.misgraded)

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 1.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 1.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    @property
    def passed(self) -> bool:
        return not (
            self.incomplete
            or self.rule_errors
            or self.missing
            or self.misgraded
            or self.unexpected
            or self.forbidden
        )

    def failures(self) -> list[str]:
        """Human-readable failure lines, ordered by how much they matter."""
        lines: list[str] = []
        # First, because they invalidate everything below them: a score
        # computed on a run that did not happen is not a low score, it is not
        # a score.
        for diagnostic in self.incomplete:
            lines.append(f"INCOMPLETE {diagnostic.code}: {diagnostic.message}")
        for rule_id, message in sorted(self.rule_errors.items()):
            lines.append(f"RULE CRASHED {rule_id}: {message}")
        for finding in self.forbidden:
            lines.append(
                f"FALSE POSITIVE on a control case: {finding.rule_id} at "
                f"{finding.location} -- {finding.message}"
            )
        for expectation, finding in self.misgraded:
            lines.append(
                f"MISGRADED {expectation.describe()}: expected "
                f"{expectation.severity.value if expectation.severity else '*'}/"
                f"{expectation.confidence.value if expectation.confidence else '*'}, "
                f"got {finding.severity.value}/{finding.confidence.value}"
            )
        for expectation in self.missing:
            lines.append(f"MISSED {expectation.describe()}")
        for finding in self.unexpected:
            lines.append(f"UNEXPECTED {finding.rule_id} at {finding.location} -- {finding.message}")
        return lines


def evaluate(
    project: Path, manifest_path: Path | None = None, include: set[str] | None = None
) -> EvalReport:
    """Audit ``project`` and score the result against its manifest.

    Thresholds are opened all the way up: an evaluation must see everything the
    rules produce, including tentative findings that a normal run would hide.

    ``include`` narrows the run to specific rule ids, which is how one rule's
    recall is measured without the rest of the catalogue's findings counting
    against it as false positives.
    """
    manifest = Manifest.load(manifest_path or project / MANIFEST_NAME)
    result = engine.run(
        project,
        include=include,
        min_severity=Severity.INFO,
        min_confidence=Confidence.TENTATIVE,
    )

    report = EvalReport(
        project=project,
        incomplete=[d for d in result.context.diagnostics if d.blocking],
        rule_errors=dict(result.rule_errors),
    )
    remaining = list(result.findings)

    for finding in list(remaining):
        if any(rule.locates(finding) for rule in manifest.must_not_report):
            report.forbidden.append(finding)
            remaining.remove(finding)

    for expectation in manifest.expected:
        match = next((f for f in remaining if expectation.locates(f)), None)
        if match is None:
            report.missing.append(expectation)
            continue
        remaining.remove(match)
        if expectation.grades(match):
            report.matched.append((expectation, match))
        else:
            report.misgraded.append((expectation, match))

    report.unexpected = remaining
    return report
