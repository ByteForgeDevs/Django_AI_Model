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
from typing import Any

from djaudit import __version__
from djaudit.engine import RunResult
from djaudit.fingerprint import FINGERPRINT_VERSION
from djaudit.models import Confidence, Finding, Severity

SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema.json"
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


def _rule_name(finding: Finding) -> str:
    """A stable PascalCase identifier, which is what SARIF's ``name`` expects."""
    words = re.findall(r"[A-Za-z0-9]+", finding.title)
    return "".join(word[:1].upper() + word[1:] for word in words) or finding.rule_id


def _help_markdown(finding: Finding) -> str:
    parts = [f"## {finding.title}"]
    if finding.rationale:
        parts.append(finding.rationale)
    if finding.remediation:
        parts.append(f"### Remediation\n\n{finding.remediation}")
    if finding.references:
        parts.append(
            "### References\n\n" + "\n".join(f"- {ref}" for ref in finding.references)
        )
    return "\n\n".join(parts)


def _descriptor(finding: Finding) -> dict[str, Any]:
    return {
        "id": finding.rule_id,
        "name": _rule_name(finding),
        "shortDescription": {"text": finding.title},
        "fullDescription": {"text": finding.rationale or finding.title},
        "help": {
            "text": finding.remediation or finding.rationale or finding.title,
            "markdown": _help_markdown(finding),
        },
        "defaultConfiguration": {"level": _LEVEL[finding.severity]},
        "properties": {
            "tags": ["django", finding.family.value, finding.family.label],
            "security-severity": f"{finding.severity.security_severity:.1f}",
            "precision": _PRECISION[finding.confidence],
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


def _notifications(result: RunResult) -> list[dict[str, Any]]:
    """Surface rule crashes in the SARIF itself rather than only on stderr."""
    return [
        {
            "level": "error",
            "message": {"text": f"rule {rule_id} failed: {error}"},
            "associatedRule": {"id": rule_id},
        }
        for rule_id, error in sorted(result.rule_errors.items())
    ]


def build(result: RunResult) -> dict[str, Any]:
    ordered_rule_ids = sorted({f.rule_id for f in result.findings})
    index_of = {rule_id: i for i, rule_id in enumerate(ordered_rule_ids)}

    first_by_rule: dict[str, Finding] = {}
    for finding in result.findings:
        first_by_rule.setdefault(finding.rule_id, finding)

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
                        "rules": [_descriptor(first_by_rule[r]) for r in ordered_rule_ids],
                    }
                },
                "originalUriBaseIds": {SRCROOT: {"uri": result.context.root.as_uri() + "/"}},
                "invocations": [
                    {
                        "executionSuccessful": not result.rule_errors,
                        "toolExecutionNotifications": _notifications(result),
                    }
                ],
                "results": [_result(f, index_of[f.rule_id]) for f in result.findings],
            }
        ],
    }


def render(result: RunResult) -> str:
    return json.dumps(build(result), indent=2) + "\n"
