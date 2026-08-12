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


def _degraded(result: RunResult) -> dict[str, Any] | None:
    """What the run could not check, for a consumer that never sees the terminal.

    Emitted even when nothing was skipped, because an empty `skipped` is the
    positive statement -- "the live tier ran, so this report is complete" -- and
    a consumer that only saw the block when it was bad could not distinguish a
    complete report from an old djaudit that never wrote one.

    Terminal output has carried this since the live tier landed; JSON and SARIF
    did not, so a pipeline uploading a report had no way to tell a clean project
    from an unreachable one. That is the exact confusion `djaudit.degradation`
    exists to prevent, and the container makes it the normal case: an image
    holds djaudit alone, so the live tier can never run inside one.
    """
    if result.degraded is None:
        return None
    return {
        "reason": result.degraded.reason,
        "skipped": [
            {
                "rule_id": item.rule_id,
                "title": item.title,
                "fallback": item.fallback,
                "covered_by": list(item.covered_by),
            }
            for item in result.degraded.skipped
        ],
    }


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
            "suppressed_path": result.suppressed_path,
            "suppressed_baseline": result.suppressed_baseline,
            "below_threshold": result.filtered_threshold,
            "rules_run": result.rules_run,
            "by_severity": {sev.value: n for sev, n in counts.items()},
            "duration_seconds": round(result.duration_seconds, 4),
        },
        "findings": [_finding(f, judged.get(f.fingerprint)) for f in result.findings],
        "degraded": _degraded(result),
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
