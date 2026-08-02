"""Baseline support: adopt the tool on a legacy codebase without drowning in alerts.

The first run against a mature Django project will report a lot. If the only
options are "fix everything now" or "turn it off", every team picks the second.
A baseline records the fingerprints of findings that already existed, so CI can
fail on *new* findings only while the backlog is burned down separately.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from djaudit.fingerprint import FINGERPRINT_VERSION
from djaudit.models import Finding

BASELINE_VERSION = 1


class BaselineError(Exception):
    """Raised when a baseline file cannot be read or is incompatible."""


@dataclass(frozen=True, slots=True)
class Baseline:
    """A recorded set of accepted findings, matched by fingerprint only.

    Entries also carry the rule id and location, purely so the committed file is
    reviewable in a pull request. Those fields are never used for matching --
    if they were, moving code would resurrect suppressed findings.
    """

    fingerprints: frozenset[str]
    created_at: str = ""
    entries: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_findings(cls, findings: Iterable[Finding]) -> Baseline:
        collected = sorted(findings, key=lambda f: (f.location.file, f.location.line, f.rule_id))
        return cls(
            fingerprints=frozenset(f.fingerprint for f in collected if f.fingerprint),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            entries=tuple(
                {
                    "fingerprint": f.fingerprint,
                    "rule_id": f.rule_id,
                    "file": f.location.file,
                    "line": f.location.line,
                    "message": f.message,
                }
                for f in collected
                if f.fingerprint
            ),
        )

    @classmethod
    def load(cls, path: Path) -> Baseline:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BaselineError(f"baseline not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise BaselineError(f"baseline is not valid JSON: {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise BaselineError(f"baseline must be a JSON object: {path}")

        version = raw.get("baseline_version")
        if version != BASELINE_VERSION:
            raise BaselineError(
                f"baseline version {version!r} is not supported "
                f"(expected {BASELINE_VERSION}); regenerate with --write-baseline"
            )
        if raw.get("fingerprint_version") != FINGERPRINT_VERSION:
            raise BaselineError(
                f"baseline was written with fingerprint scheme "
                f"{raw.get('fingerprint_version')!r}, this build uses "
                f"{FINGERPRINT_VERSION!r}; regenerate with --write-baseline"
            )

        entries = raw.get("findings", [])
        if not isinstance(entries, list):
            raise BaselineError(f"baseline 'findings' must be a list: {path}")

        return cls(
            fingerprints=frozenset(
                str(e["fingerprint"]) for e in entries if isinstance(e, dict) and "fingerprint" in e
            ),
            created_at=str(raw.get("created_at", "")),
            entries=tuple(e for e in entries if isinstance(e, dict)),
        )

    def save(self, path: Path) -> None:
        payload = {
            "baseline_version": BASELINE_VERSION,
            "fingerprint_version": FINGERPRINT_VERSION,
            "created_at": self.created_at,
            "findings": list(self.entries),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    def filter(self, findings: Sequence[Finding]) -> list[Finding]:
        """Drop findings already accepted by this baseline."""
        return [f for f in findings if f.fingerprint not in self.fingerprints]

    def __len__(self) -> int:
        return len(self.fingerprints)
