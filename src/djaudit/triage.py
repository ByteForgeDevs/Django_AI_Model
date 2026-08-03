"""Triaged benchmark verdicts for the real-world precision gate.

The Phase 0 precision gate asserted zero findings on Healthchecks and NetBox.
That works only while we detect almost nothing. Once the ``DJS`` corpus lands,
mature repositories will legitimately produce findings -- some true, some false
-- and a zero-findings assertion starts failing for the wrong reason. What
happens next is predictable: somebody disables it. That is how precision
benchmarks die.

A triage file replaces the tripwire with a tracked metric. Every finding on a
benchmark repository carries a recorded human verdict, so the gate can ask three
useful questions instead of one blunt one:

* Did something new appear that nobody has looked at?
* Is the false-positive rate climbing?
* Did a finding we know to be real quietly stop firing?

Verdicts are matched by fingerprint, which deliberately excludes line numbers,
so a verdict survives unrelated edits to the target repository.

The format is JSON rather than YAML. A reviewer edits these files by hand, which
argues for YAML, but the entries carry an explicit ``note`` field that covers
what comments would have, and JSON keeps the runtime dependency list at two
packages. For a tool whose pitch includes being cheap to install, that trade is
worth more than comment syntax.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from djaudit.fingerprint import FINGERPRINT_VERSION
from djaudit.models import Finding

TRIAGE_VERSION = 1

DEFAULT_MAX_FALSE_POSITIVE_RATE = 0.10
"""Above roughly one in ten, developers stop reading the output."""


class TriageError(Exception):
    """Raised when a triage file cannot be read or is incompatible."""


class Verdict(StrEnum):
    """A human's judgement about one finding on a benchmark repository."""

    TRUE_POSITIVE = "true_positive"
    """A real defect. We were right to report it."""

    FALSE_POSITIVE = "false_positive"
    """Not a defect. We were wrong, and this counts against the gate."""

    ACCEPTED_RISK = "accepted_risk"
    """Real, but the project has knowingly accepted it.

    Counts as a correct detection. Whether the target chooses to fix something
    says nothing about whether we were right to flag it, and conflating the two
    would punish us for other people's risk appetite.
    """

    @property
    def is_correct_detection(self) -> bool:
        return self is not Verdict.FALSE_POSITIVE


@dataclass(frozen=True, slots=True)
class TriageEntry:
    """One reviewed finding.

    ``file`` and ``line`` are recorded for reviewability only -- matching is by
    fingerprint alone, so that moving code does not silently discard a verdict.
    """

    fingerprint: str
    rule_id: str
    verdict: Verdict
    file: str = ""
    line: int = 0
    note: str = ""
    reviewed: str = ""
    reviewer: str = ""

    @classmethod
    def from_finding(cls, finding: Finding, verdict: Verdict, note: str = "") -> TriageEntry:
        return cls(
            fingerprint=finding.fingerprint,
            rule_id=finding.rule_id,
            verdict=verdict,
            file=finding.location.file,
            line=finding.location.line,
            note=note,
            reviewed=datetime.now(UTC).date().isoformat(),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TriageEntry:
        try:
            fingerprint = str(raw["fingerprint"])
            verdict = Verdict(str(raw["verdict"]))
        except KeyError as exc:
            raise TriageError(f"triage entry is missing {exc}: {raw!r}") from exc
        except ValueError as exc:
            raise TriageError(
                f"unknown verdict {raw.get('verdict')!r}; expected one of "
                f"{', '.join(v.value for v in Verdict)}"
            ) from exc

        return cls(
            fingerprint=fingerprint,
            rule_id=str(raw.get("rule_id", "")),
            verdict=verdict,
            file=str(raw.get("file", "")),
            line=int(raw.get("line", 0)),
            note=str(raw.get("note", "")),
            reviewed=str(raw.get("reviewed", "")),
            reviewer=str(raw.get("reviewer", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "rule_id": self.rule_id,
            "verdict": self.verdict.value,
            "file": self.file,
            "line": self.line,
            "note": self.note,
            "reviewed": self.reviewed,
            "reviewer": self.reviewer,
        }

    @property
    def family(self) -> str:
        return self.rule_id.split("-", 1)[0].upper()


@dataclass(frozen=True, slots=True)
class Triage:
    """Every recorded verdict for one benchmark repository."""

    target: str
    repo: str = ""
    sha: str = ""
    entries: tuple[TriageEntry, ...] = ()
    max_false_positive_rate: float = DEFAULT_MAX_FALSE_POSITIVE_RATE

    @property
    def by_fingerprint(self) -> dict[str, TriageEntry]:
        return {entry.fingerprint: entry for entry in self.entries}

    @classmethod
    def load(cls, path: Path) -> Triage:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise TriageError(f"triage file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise TriageError(f"triage file is not valid JSON: {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise TriageError(f"triage file must be a JSON object: {path}")

        version = raw.get("triage_version")
        if version != TRIAGE_VERSION:
            raise TriageError(
                f"triage version {version!r} is not supported (expected {TRIAGE_VERSION}): {path}"
            )
        if raw.get("fingerprint_version") != FINGERPRINT_VERSION:
            raise TriageError(
                f"triage file was written with fingerprint scheme "
                f"{raw.get('fingerprint_version')!r}, this build uses "
                f"{FINGERPRINT_VERSION!r}; every verdict must be re-reviewed: {path}"
            )

        findings = raw.get("findings", [])
        if not isinstance(findings, list):
            raise TriageError(f"triage 'findings' must be a list: {path}")

        return cls(
            target=str(raw.get("target", path.stem)),
            repo=str(raw.get("repo", "")),
            sha=str(raw.get("sha", "")),
            entries=tuple(
                TriageEntry.from_dict(item) for item in findings if isinstance(item, dict)
            ),
            max_false_positive_rate=float(
                raw.get("max_false_positive_rate", DEFAULT_MAX_FALSE_POSITIVE_RATE)
            ),
        )

    def save(self, path: Path) -> None:
        # Sorted by location so a reviewer reads the file in the order they
        # would read the repository, and so diffs stay small and meaningful.
        ordered = sorted(self.entries, key=lambda e: (e.file, e.line, e.rule_id, e.fingerprint))
        payload = {
            "triage_version": TRIAGE_VERSION,
            "fingerprint_version": FINGERPRINT_VERSION,
            "target": self.target,
            "repo": self.repo,
            "sha": self.sha,
            "max_false_positive_rate": self.max_false_positive_rate,
            "findings": [entry.to_dict() for entry in ordered],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def with_entries(self, new: Iterable[TriageEntry]) -> Triage:
        """Add entries, keeping any existing verdict for the same fingerprint.

        Existing verdicts win: re-running the benchmark must never overwrite a
        human's judgement with a freshly defaulted one.
        """
        merged = self.by_fingerprint
        for entry in new:
            merged.setdefault(entry.fingerprint, entry)
        return replace(self, entries=tuple(merged.values()))

    def __len__(self) -> int:
        return len(self.entries)
