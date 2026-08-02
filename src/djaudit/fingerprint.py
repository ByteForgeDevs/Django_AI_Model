"""Stable finding fingerprints.

A fingerprint identifies "the same problem" across runs. It deliberately
excludes line numbers: adding an import at the top of a file must not
invalidate a committed baseline, or teams will stop trusting the baseline and
stop using the tool.

The identity of a finding is therefore:

    rule id + file path + normalised source snippet + occurrence index

The occurrence index disambiguates a rule firing twice on identical text in one
file. It is assigned in document order, so it only shifts when an earlier
occurrence is added or removed -- the smallest blast radius available without
semantic analysis.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from djaudit.models import Finding

FINGERPRINT_VERSION = "djaudit/v1"
_DIGEST_LENGTH = 16


def normalise_snippet(snippet: str) -> str:
    """Collapse whitespace so reformatting does not change identity."""
    return " ".join(snippet.split())


def compute(rule_id: str, file: str, snippet: str, occurrence: int = 0) -> str:
    payload = "\x00".join(
        (FINGERPRINT_VERSION, rule_id, file, normalise_snippet(snippet), str(occurrence))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def assign(findings: Iterable[Finding]) -> list[Finding]:
    """Return findings with fingerprints assigned, in document order.

    Deterministic for a given input set regardless of the order rules ran in.
    """
    ordered = sorted(
        findings,
        key=lambda f: (f.location.file, f.location.line, f.location.column, f.rule_id),
    )
    seen: dict[tuple[str, str, str], int] = {}
    result: list[Finding] = []
    for finding in ordered:
        key = (finding.rule_id, finding.location.file, normalise_snippet(finding.location.snippet))
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        result.append(finding.with_fingerprint(compute(*key, occurrence)))
    return result
