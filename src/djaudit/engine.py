"""The analysis engine: discover, run rules, filter, fingerprint."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from djaudit import fingerprint as fp
from djaudit.baseline import Baseline
from djaudit.context import ProjectContext
from djaudit.discovery import build_context
from djaudit.models import Confidence, Family, Finding, Severity, Tier
from djaudit.registry import Rule, select
from djaudit.suppression import is_suppressed


@dataclass
class RunResult:
    """Outcome of a single audit run, including everything that was filtered.

    The counters matter as much as the findings: a run reporting nothing because
    a baseline swallowed 300 findings is a very different situation from a clean
    project, and the CLI must be able to say which one happened.
    """

    context: ProjectContext
    findings: list[Finding] = field(default_factory=list)
    total_raw: int = 0
    suppressed_inline: int = 0
    suppressed_baseline: int = 0
    filtered_threshold: int = 0
    rules_run: int = 0
    rule_errors: dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0

    @property
    def worst_severity(self) -> Severity | None:
        return max((f.severity for f in self.findings), key=lambda s: s.rank, default=None)

    def counts_by_severity(self) -> dict[Severity, int]:
        counts = dict.fromkeys(Severity, 0)
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts


def _passes_threshold(
    finding: Finding, min_severity: Severity, min_confidence: Confidence
) -> bool:
    return (
        finding.severity.rank >= min_severity.rank
        and finding.confidence.rank >= min_confidence.rank
    )


def run(
    root: Path,
    *,
    families: set[Family] | None = None,
    tiers: set[Tier] | None = None,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    min_severity: Severity = Severity.INFO,
    min_confidence: Confidence = Confidence.TENTATIVE,
    baseline: Baseline | None = None,
    context: ProjectContext | None = None,
) -> RunResult:
    """Audit the project at ``root``.

    Rules are isolated: one raising an exception is recorded in
    ``rule_errors`` and the run continues. A single unusual construct in a large
    codebase must never cost the user the other 40 rules' worth of results.
    """
    started = time.perf_counter()
    ctx = context if context is not None else build_context(root)
    if tiers is None:
        tiers = {Tier.STATIC} if not ctx.live else {Tier.STATIC, Tier.LIVE}

    result = RunResult(context=ctx)
    collected: list[Finding] = []

    for rule_cls in select(families=families, tiers=tiers, include=include, exclude=exclude):
        result.rules_run += 1
        try:
            collected.extend(_run_rule(rule_cls, ctx))
        except Exception as exc:  # deliberate: rule isolation is the point
            result.rule_errors[rule_cls.meta.id] = f"{type(exc).__name__}: {exc}"

    result.total_raw = len(collected)

    kept: list[Finding] = []
    for finding in collected:
        path = ctx.root / finding.location.file
        if is_suppressed(
            ctx.lines(path), finding.location.line, finding.location.end_line, finding.rule_id
        ):
            result.suppressed_inline += 1
            continue
        kept.append(finding)

    kept = fp.assign(kept)

    if baseline is not None:
        before = len(kept)
        kept = baseline.filter(kept)
        result.suppressed_baseline = before - len(kept)

    above = [f for f in kept if _passes_threshold(f, min_severity, min_confidence)]
    result.filtered_threshold = len(kept) - len(above)

    result.findings = sorted(above, key=lambda f: f.sort_key)
    result.duration_seconds = time.perf_counter() - started
    return result


def _run_rule(rule_cls: type[Rule], ctx: ProjectContext) -> list[Finding]:
    return list(rule_cls().check(ctx))
