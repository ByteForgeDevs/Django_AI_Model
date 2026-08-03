"""Machine-readable JSON output.

The stable interchange format for anything programmatic, including the future
LLM layer, which consumes findings plus evidence and returns explanations and
patches. Versioned by ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import json
from typing import Any

from djaudit import __version__
from djaudit.engine import RunResult
from djaudit.models import SCHEMA_VERSION


def build(result: RunResult) -> dict[str, Any]:
    ctx = result.context
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
        "findings": [f.to_dict() for f in result.findings],
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


def render(result: RunResult) -> str:
    return json.dumps(build(result), indent=2) + "\n"
