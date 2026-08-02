"""SARIF 2.1.0 output.

SARIF is what makes this usable in CI without building any integration: GitHub
ingests it natively, so findings appear as inline pull request annotations and
in the Security tab.

Two details matter for that integration to behave well:

* ``partialFingerprints`` lets GitHub track a finding across commits, so a
  resolved alert stays resolved and an unchanged one is not re-raised.
* ``security-severity`` and ``precision`` drive GitHub's own severity ranking
  and its noise filtering, which is where our severity/confidence split earns
  its keep.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from djaudit import __version__
from djaudit.engine import RunResult
from djaudit.fingerprint import FINGERPRINT_VERSION
from djaudit.models import Confidence, Family, Finding, Severity
from djaudit.registry import RuleError, get

SARIF_SCHEMA = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
)
SARIF_VERSION = "2.1.0"
INFORMATION_URI = "https://github.com/ByteForgeDevs/Django_AI_Model"
SRCROOT = "%SRCROOT%"

_LEVEL: dict[Severity, str] = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
    Severity.INFO: "note",
}

_PRECISION: dict[Confidence, str] = {
    Confidence.CERTAIN: "very-high",
    Confidence.FIRM: "high",
    Confidence.TENTATIVE: "medium",
}


@dataclass(frozen=True, slots=True)
class _RuleInfo:
    """What a SARIF ``reportingDescriptor`` needs, from wherever we can get it.

    Preferably from ``RuleMeta``, because a descriptor describes the *rule*.
    Falling back to a sample finding covers rule ids we did not register --
    which is exactly what the external tool adapters will produce in Phase 5.
    """

    id: str
    title: str
    family: Family
    severity: Severity
    confidence: Confidence
    rationale: str
    remediation: str
    references: tuple[str, ...]


def _rule_name(info: _RuleInfo) -> str:
    """A stable PascalCase identifier, which is what SARIF's ``name`` expects."""
    words = re.findall(r"[A-Za-z0-9]+", info.title)
    return "".join(word[:1].upper() + word[1:] for word in words) or info.id


def _help_markdown(info: _RuleInfo) -> str:
    parts = [f"## {info.title}"]
    if info.rationale:
        parts.append(info.rationale)
    if info.remediation:
        parts.append(f"### Remediation\n\n{info.remediation}")
    if info.references:
        parts.append("### References\n\n" + "\n".join(f"- {ref}" for ref in info.references))
    return "\n\n".join(parts)


def _family_from_id(rule_id: str) -> Family:
    """Best-effort family for a rule we know nothing else about.

    The prefix is the family by construction, so read it rather than guessing.
    """
    try:
        return Family(rule_id.split("-", 1)[0].upper())
    except ValueError:
        return Family.DJS


def _rule_info(rule_id: str, sample: Finding | None) -> _RuleInfo:
    """Describe a rule, preferring its registered metadata over any one finding.

    A descriptor's ``defaultConfiguration`` and ``security-severity`` are
    properties of the rule, not of one occurrence. Deriving them from a sample
    finding mislabels the whole rule whenever a rule grades an instance down --
    DJS-001 downgrades an overridden base module to ``low``, which would have
    published DJS-001 to GitHub as a low-severity rule.
    """
    try:
        meta = get(rule_id).meta
    except RuleError:
        meta = None

    if meta is not None:
        return _RuleInfo(
            id=meta.id,
            title=meta.title,
            family=meta.family,
            severity=meta.severity,
            confidence=meta.confidence,
            rationale=meta.rationale,
            remediation=meta.remediation,
            references=meta.references,
        )
    if sample is not None:
        return _RuleInfo(
            id=sample.rule_id,
            title=sample.title,
            family=sample.family,
            severity=sample.severity,
            confidence=sample.confidence,
            rationale=sample.rationale,
            remediation=sample.remediation,
            references=tuple(sample.references),
        )
    return _RuleInfo(
        id=rule_id,
        title=rule_id,
        family=_family_from_id(rule_id),
        severity=Severity.MEDIUM,
        confidence=Confidence.TENTATIVE,
        rationale="",
        remediation="",
        references=(),
    )


def _descriptor(info: _RuleInfo) -> dict[str, Any]:
    return {
        "id": info.id,
        "name": _rule_name(info),
        "shortDescription": {"text": info.title},
        "fullDescription": {"text": info.rationale or info.title},
        "help": {
            "text": info.remediation or info.rationale or info.title,
            "markdown": _help_markdown(info),
        },
        "defaultConfiguration": {"level": _LEVEL[info.severity]},
        "properties": {
            "tags": ["django", info.family.value, info.family.label],
            "security-severity": f"{info.severity.security_severity:.1f}",
            "precision": _PRECISION[info.confidence],
        },
    }


def _region(finding: Finding) -> dict[str, Any]:
    location = finding.location
    region: dict[str, Any] = {
        "startLine": max(location.line, 1),
        "startColumn": max(location.column, 1),
    }
    if location.end_line is not None and location.end_line >= region["startLine"]:
        region["endLine"] = location.end_line
        if location.end_column is not None:
            region["endColumn"] = max(location.end_column, 1)
    if location.snippet:
        region["snippet"] = {"text": location.snippet}
    return region


def _result(finding: Finding, rule_index: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ruleId": finding.rule_id,
        "ruleIndex": rule_index,
        "level": _LEVEL[finding.severity],
        "message": {"text": finding.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {
                        "uri": finding.location.file,
                        "uriBaseId": SRCROOT,
                    },
                    "region": _region(finding),
                }
            }
        ],
        "properties": {
            "confidence": finding.confidence.value,
            "family": finding.family.value,
            "tier": finding.tier.value,
            "remediation": finding.remediation,
            **finding.properties,
        },
    }
    if finding.fingerprint:
        payload["partialFingerprints"] = {FINGERPRINT_VERSION: finding.fingerprint}
    return payload


def _notifications(result: RunResult, index_of: dict[str, int]) -> list[dict[str, Any]]:
    """Surface rule crashes in the SARIF itself rather than only on stderr.

    ``associatedRule`` is a reportingDescriptorReference, so it has to resolve
    to a descriptor in ``tool.driver.rules``. ``build`` guarantees one exists
    for every crashed rule, and we carry the index as well as the id so a
    consumer can resolve it without a lookup.
    """
    notifications = []
    for rule_id, error in sorted(result.rule_errors.items()):
        notification: dict[str, Any] = {
            "level": "error",
            "message": {"text": f"rule {rule_id} failed: {error}"},
            "associatedRule": {"id": rule_id, "index": index_of[rule_id]},
        }
        notifications.append(notification)
    return notifications


def build(result: RunResult) -> dict[str, Any]:
    first_by_rule: dict[str, Finding] = {}
    for finding in result.findings:
        first_by_rule.setdefault(finding.rule_id, finding)

    # Crashed rules need a descriptor too, even though they produced no
    # findings, or the notification below points at nothing.
    ordered_rule_ids = sorted(set(first_by_rule) | set(result.rule_errors))
    index_of = {rule_id: i for i, rule_id in enumerate(ordered_rule_ids)}
    descriptors = [
        _descriptor(_rule_info(rule_id, first_by_rule.get(rule_id))) for rule_id in ordered_rule_ids
    ]

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "djaudit",
                        "version": __version__,
                        "semanticVersion": __version__,
                        "informationUri": INFORMATION_URI,
                        "rules": descriptors,
                    }
                },
                "originalUriBaseIds": {SRCROOT: {"uri": result.context.root.as_uri() + "/"}},
                "invocations": [
                    {
                        "executionSuccessful": not result.rule_errors,
                        "toolExecutionNotifications": _notifications(result, index_of),
                    }
                ],
                "results": [_result(f, index_of[f.rule_id]) for f in result.findings],
            }
        ],
    }


def render(result: RunResult) -> str:
    return json.dumps(build(result), indent=2) + "\n"
