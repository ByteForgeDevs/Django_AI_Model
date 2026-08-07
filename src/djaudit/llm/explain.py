"""Explain one finding in terms of the code it was found in.

The rule's own text says what the rule is about. It does not say what happened
*here*: which line, which relation, which setting, what else in the same file is
wrong for the same reason. That is what this assembles.

**No model is involved, and none is needed.** Measured across the 245 corpus
findings: 100% carry evidence, references, a rationale and a remediation, and
97% carry a snippet. The material for a substantive explanation is already in
the finding schema, put there by the rule that fired. A model can add prose on
top (6.3.2) but cannot add facts, and `explain` works with the provider offline
-- which is the default.

**This module never reads the target's source, and that is a security property
rather than an optimisation.** Settings rules mask the value they report, so the
snippet for a live key is `SECRET_KEY = "*x<redacted:50 chars>"`. An explanation
that "helpfully" re-read the line from disk to show more context would print the
real key to a terminal, into CI logs, and -- once 6.3.2 exists -- into a prompt
bound for a third party. So the finding is explained entirely from the finding.

This is the exact inverse of `suggest`, which *must* read from disk because it
builds a patch, and the contrast is deliberate: a patch has to match reality, a
description has to respect the redaction the rule applied.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from djaudit.models import Evidence, Finding

# Fingerprints are 16 hex characters. Fewer than this collides too readily to
# accept as an identifier; the caller is told to paste more of it.
MINIMUM_PREFIX = 6

# A file with forty findings does not need all forty listed under one of them.
RELATED_SHOWN = 6

_EVIDENCE_LABELS = {
    "ast": "the code this was read from",
    "config": "the setting as resolved",
    "source": "the line as parsed",
    "sql": "the SQL this emits",
    "command_output": "what the command reported",
}


class FingerprintError(Exception):
    """A fingerprint that named no finding, or more than one."""


@dataclass(frozen=True, slots=True)
class Explanation:
    """One finding, everything the engine knows about it, and its neighbours."""

    finding: Finding
    same_cause: tuple[Finding, ...]
    same_file: tuple[Finding, ...]

    @property
    def where(self) -> str:
        location = self.finding.location
        return f"{location.file}:{location.line}"

    @property
    def headline(self) -> str:
        return f"{self.finding.rule_id} {self.finding.title}"


def find(findings: Sequence[Finding], fingerprint: str) -> Finding:
    """The one finding a fingerprint prefix names.

    Ambiguity is an error rather than a first match. Explaining the wrong
    finding under the identifier of another is worse than explaining nothing,
    because the output looks entirely correct.
    """
    wanted = fingerprint.strip().lower()
    if len(wanted) < MINIMUM_PREFIX:
        raise FingerprintError(
            f"{fingerprint!r} is too short to identify a finding; "
            f"give at least {MINIMUM_PREFIX} characters"
        )

    matches = [f for f in findings if f.fingerprint.startswith(wanted)]
    if not matches:
        raise FingerprintError(
            f"no finding here has fingerprint {wanted!r}; it may have been fixed, "
            "or the run may have used different filters"
        )
    if len(matches) > 1:
        listed = ", ".join(sorted(f.fingerprint for f in matches)[:4])
        raise FingerprintError(f"{wanted!r} matches {len(matches)} findings: {listed}")
    return matches[0]


def explain(finding: Finding, among: Sequence[Finding] = ()) -> Explanation:
    """Assemble the explanation, including what else it sits next to.

    `same_cause` is the rest of this rule's findings in this file -- the theme
    from 6.2.4, which was measured to be the grouping a reviewer decides once.
    `same_file` is everything else wrong in the file, which is how a reader
    works out whether they have found one mistake or a habit.
    """
    location = finding.location
    others = [f for f in among if f.fingerprint != finding.fingerprint]

    same_cause = tuple(
        f for f in others if f.rule_id == finding.rule_id and f.location.file == location.file
    )
    same_file = tuple(
        f for f in others if f.location.file == location.file and f.rule_id != finding.rule_id
    )
    return Explanation(finding=finding, same_cause=same_cause, same_file=same_file)


def label_for(evidence: Evidence) -> str:
    kind = evidence.kind.value if hasattr(evidence.kind, "value") else str(evidence.kind)
    return _EVIDENCE_LABELS.get(kind, kind)


def render(explanation: Explanation) -> str:
    """Plain text, so what is asserted in tests is what a reader sees."""
    finding = explanation.finding
    lines = [
        explanation.headline,
        f"  at {explanation.where}  ({finding.severity.value}, "
        f"{finding.confidence.value}, {finding.tier.value} analysis)",
        "",
        "What is wrong",
        f"  {finding.message}",
    ]

    if finding.location.snippet:
        lines += ["", "The code", *[f"  {line}" for line in finding.location.snippet.splitlines()]]

    for evidence in finding.evidence:
        lines += ["", f"Evidence -- {label_for(evidence)}"]
        lines += [f"  {line}" for line in evidence.content.splitlines()]
        if evidence.source:
            lines.append(f"  (from {evidence.source})")

    lines += ["", "Why it matters", f"  {finding.rationale}"]
    lines += ["", "What to do", f"  {finding.remediation}"]

    if explanation.same_cause:
        shown = explanation.same_cause[:RELATED_SHOWN]
        lines += [
            "",
            f"The same thing, in the same file ({len(explanation.same_cause)} more)",
            *[f"  line {f.location.line}  [{f.fingerprint}]" for f in shown],
        ]
    if explanation.same_file:
        shown = explanation.same_file[:RELATED_SHOWN]
        lines += [
            "",
            f"Also in this file ({len(explanation.same_file)} findings)",
            *[f"  {f.rule_id} at line {f.location.line}  [{f.fingerprint}]" for f in shown],
        ]

    if finding.references:
        lines += ["", "References", *[f"  {url}" for url in finding.references]]
    return "\n".join(lines)
