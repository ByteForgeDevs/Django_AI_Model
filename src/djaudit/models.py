"""The finding schema.

This module is the project's central contract. Three consumers depend on its
exact shape: the SARIF reporter (and therefore GitHub code scanning), the
baseline format (and therefore every downstream repository that has committed
one), and the future LLM layer, which consumes findings plus evidence and emits
explanations and patches.

Treat changes here as breaking changes and bump ``SCHEMA_VERSION``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 2


class Severity(StrEnum):
    """How much damage the finding can do if left in place."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @property
    def security_severity(self) -> float:
        """CVSS-like score consumed by GitHub code scanning for its own ranking."""
        return _SECURITY_SEVERITY[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}

_SECURITY_SEVERITY: dict[Severity, float] = {
    Severity.CRITICAL: 9.5,
    Severity.HIGH: 7.5,
    Severity.MEDIUM: 5.0,
    Severity.LOW: 3.0,
    Severity.INFO: 1.0,
}


class Confidence(StrEnum):
    """How sure we are that this is a real defect rather than a pattern match.

    Kept deliberately separate from severity. A tentative critical is not the
    same thing as a certain low, and collapsing the two is how static analysis
    tools earn their reputation for noise.
    """

    CERTAIN = "certain"
    """Provable from the source or from tool output. No judgement involved."""

    FIRM = "firm"
    """Strong signal, but depends on an assumption we could not verify."""

    TENTATIVE = "tentative"
    """Worth a human look. Expect false positives; excluded from CI gates by default."""

    @property
    def rank(self) -> int:
        return _CONFIDENCE_RANK[self]


_CONFIDENCE_RANK: dict[Confidence, int] = {
    Confidence.CERTAIN: 2,
    Confidence.FIRM: 1,
    Confidence.TENTATIVE: 0,
}


class Tier(StrEnum):
    """What the rule needs in order to run."""

    STATIC = "static"
    """Parses source only. Never imports or executes the target."""

    LIVE = "live"
    """Needs the target's virtualenv: real settings resolution, sqlmigrate, EXPLAIN."""


class Family(StrEnum):
    """Rule families. The prefix of every rule id."""

    DJS = "DJS"
    """Settings and deployment hardening."""

    DJI = "DJI"
    """Injection and untrusted input."""

    DJA = "DJA"
    """API and DRF authorization and data exposure."""

    DJP = "DJP"
    """Performance and ORM efficiency."""

    DJM = "DJM"
    """Migration safety."""

    DJX = "DJX"
    """Cross-database portability and divergence."""

    DJD = "DJD"
    """Data model design.

    Separate from `DJS` because the fix is a migration rather than a settings
    line, and separate from `DJP` because these are correctness defects that
    happen to be cheap: a nullable `CharField` has two spellings of empty and
    every query has to know it, and an unordered queryset paginates by
    whatever the planner felt like. Neither costs anything until it produces a
    wrong answer.
    """

    @property
    def label(self) -> str:
        return _FAMILY_LABEL[self]


_FAMILY_LABEL: dict[Family, str] = {
    Family.DJS: "Settings & deployment",
    Family.DJI: "Injection & untrusted input",
    Family.DJA: "API authorization & exposure",
    Family.DJD: "Data model design",
    Family.DJP: "Performance & ORM efficiency",
    Family.DJM: "Migration safety",
    Family.DJX: "Database portability",
}


class EvidenceKind(StrEnum):
    """Where a piece of evidence came from.

    Every finding must carry at least one. A finding without evidence is an
    opinion, and this tool does not ship opinions.
    """

    SOURCE = "source"
    """A verbatim excerpt of the analysed file."""

    AST = "ast"
    """A derived fact about the syntax tree, e.g. a resolved assignment."""

    CONFIG = "config"
    """A resolved configuration value and where it came from."""

    SQL = "sql"
    """SQL emitted by the database or by ``sqlmigrate``."""

    COMMAND_OUTPUT = "command_output"
    """Captured stdout/stderr of a command run against the target."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """A concrete artefact backing a finding."""

    kind: EvidenceKind
    content: str
    source: str = ""
    """Human label for the origin, e.g. ``manage.py check --deploy``."""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "content": self.content, "source": self.source}


@dataclass(frozen=True, slots=True)
class Location:
    """Where in the codebase the finding sits.

    ``file`` is always a POSIX-style path relative to the analysed project root,
    so findings and baselines stay portable across machines and CI runners.
    """

    file: str
    line: int
    column: int = 1
    end_line: int | None = None
    end_column: int | None = None
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "snippet": self.snippet,
        }

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.column}"


@dataclass(frozen=True, slots=True)
class Finding:
    """A single audit result.

    ``fingerprint`` is assigned by the engine after collection rather than by
    the rule, because stable numbering of repeated findings requires seeing the
    whole set. Rules should leave it empty.
    """

    rule_id: str
    title: str
    family: Family
    severity: Severity
    confidence: Confidence
    tier: Tier
    location: Location
    message: str
    rationale: str = ""
    remediation: str = ""
    evidence: tuple[Evidence, ...] = ()
    references: tuple[str, ...] = ()
    fingerprint: str = ""
    properties: dict[str, str] = field(default_factory=dict)

    def with_fingerprint(self, fingerprint: str) -> Finding:
        return replace(self, fingerprint=fingerprint)

    @property
    def sort_key(self) -> tuple[int, int, str, int, str]:
        """Most severe, most confident, then stable by position."""
        return (
            -self.severity.rank,
            -self.confidence.rank,
            self.location.file,
            self.location.line,
            self.rule_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "family": self.family.value,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "tier": self.tier.value,
            "location": self.location.to_dict(),
            "message": self.message,
            "rationale": self.rationale,
            "remediation": self.remediation,
            "evidence": [e.to_dict() for e in self.evidence],
            "references": list(self.references),
            "fingerprint": self.fingerprint,
            "properties": dict(self.properties),
        }
