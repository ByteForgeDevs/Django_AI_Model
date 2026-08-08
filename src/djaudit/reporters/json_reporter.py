"""Machine-readable JSON output.

The stable interchange format for anything programmatic, including the future
LLM layer, which consumes findings plus evidence and returns explanations and
patches. Versioned by ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from djaudit import __version__
from djaudit.engine import RunResult
from djaudit.models import SCHEMA_VERSION, Finding
from djaudit.provenance import DETERMINISTIC, Verdict


def _finding(finding: Finding, verdict: Verdict | None) -> dict[str, Any]:
    """A finding is always deterministic; only its verdict may not be."""
    payload = {**finding.to_dict(), "provenance": DETERMINISTIC.as_properties()}
    if verdict is not None:
        payload["triage"] = verdict.as_properties()
    return payload


def build(
    result: RunResult,
    verdicts: Mapping[str, Verdict] | None = None,
) -> dict[str, Any]:
    ctx = result.context
    judged = verdicts or {}
    counts = result.counts_by_severity()
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "djaudit", "version": __version__},
        "project": {
            "root": str(ctx.root),
            "django_version": ctx.django_version,
            "settings_entrypoint": ctx.settings_entrypoint,
            "settings_modules": [
                {"dotted": m.dotted, "path": ctx.rel(m.path), "role": m.role.value}
                for m in ctx.settings_modules
            ],
            "python_files": len(ctx.python_files),
        },
        "summary": {
            "reported": len(result.findings),
            "total_raw": result.total_raw,
            "suppressed_inline": result.suppressed_inline,
            "suppressed_baseline": result.suppressed_baseline,
            "below_threshold": result.filtered_threshold,
            "rules_run": result.rules_run,
            "by_severity": {sev.value: n for sev, n in counts.items()},
            "duration_seconds": round(result.duration_seconds, 4),
        },
        "findings": [_finding(f, judged.get(f.fingerprint)) for f in result.findings],
        "diagnostics": [
            {
                "code": d.code,
                "message": d.message,
                "detail": d.detail,
                "blocking": d.blocking,
            }
            for d in ctx.diagnostics
        ],
        "rule_errors": dict(result.rule_errors),
        "parse_errors": {ctx.rel(p): msg for p, msg in ctx.parse_errors.items()},
    }


def render(result: RunResult, verdicts: Mapping[str, Verdict] | None = None) -> str:
    return json.dumps(build(result, verdicts), indent=2) + "\n"
