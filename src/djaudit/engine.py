"""The analysis engine: discover, run rules, filter, fingerprint."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from djaudit import fingerprint as fp
from djaudit import scope
from djaudit.adapters.base import Adapter, Report
from djaudit.adapters.merge import merge
from djaudit.baseline import Baseline
from djaudit.context import ProjectContext
from djaudit.degradation import Degradation, assess
from djaudit.discovery import build_context
from djaudit.gcpolicy import deferred_full_collection
from djaudit.models import Confidence, Family, Finding, Severity, Tier
from djaudit.pathfilter import excluded
from djaudit.registry import Rule, select
from djaudit.suppression import is_suppressed

AFTER_CORROBORATION = frozenset({"DJS-028"})
"""Rules whose input is the other rules' output, so they run in a second pass.

Kept as an explicit set rather than a flag on `RuleMeta` because there is one
of them and a general mechanism would be a general mechanism for nothing. If a
second one appears, that is the moment to build one.
"""


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
    suppressed_path: int = 0
    """How many findings an `exclude_paths` pattern hid.

    Reported for the reason in this class's docstring: an exclusion that
    quietly removes 300 findings and a clean project both print nothing.
    """
    suppressed_baseline: int = 0
    filtered_threshold: int = 0
    rules_run: int = 0
    corroborated: int = 0
    """How many Django deployment checks landed on a finding of ours.

    Counted rather than inferred: a live run where Django confirmed nothing and
    a static run where it was never asked both show zero findings raised to
    `certain`, and they are not the same situation.
    """

    external_notices: tuple[str, ...] = ()
    """One line per external tool asked for, whether it ran or not.

    A report that is short because a linter was missing looks exactly like a
    report that is short because a project is clean, so the run says which.
    """

    external_diagnostics: tuple[str, ...] = ()
    external_duplicates: int = 0

    rule_errors: dict[str, str] = field(default_factory=dict)
    degraded: Degradation | None = None
    """Live-tier rules this run did not reach. Reported, never silently dropped."""
    duration_seconds: float = 0.0

    @property
    def worst_severity(self) -> Severity | None:
        return max((f.severity for f in self.findings), key=lambda s: s.rank, default=None)

    def counts_by_severity(self) -> dict[Severity, int]:
        counts = dict.fromkeys(Severity, 0)
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts


def _why_not_live(ctx: ProjectContext, tiers: set[Tier]) -> str:
    """The reader's answer to "why is this shorter than I expected".

    Distinguishes the three ways a live rule fails to run, because they need
    three different actions: give consent, install a virtualenv, or nothing.
    """
    if ctx.live:
        return "the live tier ran"
    # Checked before the tier set, because a request that failed downgrades the
    # tiers to static and would otherwise report itself as never having been
    # made -- telling the reader to pass a flag they already passed.
    if ctx.live_problem is not None:
        return f"the live tier was requested but {ctx.live_problem}"
    if Tier.LIVE not in tiers:
        return "the live tier was not requested"
    return "the live tier was requested but the target's environment is unavailable"


def _passes_threshold(finding: Finding, min_severity: Severity, min_confidence: Confidence) -> bool:
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
    external: Sequence[Adapter] = (),
    exclude_paths: tuple[str, ...] = (),
) -> RunResult:
    """Audit the project at ``root``.

    Rules are isolated: one raising an exception is recorded in
    ``rule_errors`` and the run continues. A single unusual construct in a large
    codebase must never cost the user the other 40 rules' worth of results.

    The run holds every parsed AST live throughout, so full garbage collections
    during it traverse a large heap and free nothing. See
    :mod:`djaudit.gcpolicy` for why they are suppressed here and what it costs.
    """
    with deferred_full_collection():
        return _audit(
            root,
            families=families,
            tiers=tiers,
            include=include,
            exclude=exclude,
            min_severity=min_severity,
            min_confidence=min_confidence,
            baseline=baseline,
            context=context,
            external=external,
            exclude_paths=exclude_paths,
        )


def _audit(
    root: Path,
    *,
    families: set[Family] | None,
    tiers: set[Tier] | None,
    include: set[str] | None,
    exclude: set[str] | None,
    min_severity: Severity,
    min_confidence: Confidence,
    baseline: Baseline | None,
    context: ProjectContext | None,
    external: Sequence[Adapter],
    exclude_paths: tuple[str, ...],
) -> RunResult:
    """The audit itself. Separated so :func:`run` reads as policy, then work."""
    started = time.perf_counter()
    ctx = context if context is not None else build_context(root)
    if tiers is None:
        tiers = {Tier.STATIC} if not ctx.live else {Tier.STATIC, Tier.LIVE}
    requested = tiers
    if not ctx.live:
        # A tier is a capability, not a preference -- the same reason `select`
        # applies it ahead of `include`. A live rule without a live context
        # cannot check anything, and letting it be selected would make it count
        # as having run: `assess` reads the selected set as the rules that
        # reached the target. The reader would be told nothing was skipped on a
        # run where nothing live was checked, which is the single failure
        # `djaudit.degradation` exists to prevent. `requested` is kept so the
        # reason still reports what was asked for.
        tiers = tiers - {Tier.LIVE}

    selected = select(families=families, tiers=tiers, include=include, exclude=exclude)
    result = RunResult(
        context=ctx,
        degraded=assess(_why_not_live(ctx, requested), ran={r.meta.id for r in selected}),
    )
    collected: list[Finding] = []

    # `DJS-028` reports the deployment checks that landed on nothing of ours,
    # so its subject is the outcome of every other rule. It runs in a second
    # pass for the same reason the engine assigns fingerprints rather than the
    # rules doing it: the answer requires seeing the whole set.
    deferred = [r for r in selected if r.meta.id in AFTER_CORROBORATION]
    for rule_cls in [r for r in selected if r not in deferred]:
        result.rules_run += 1
        try:
            collected.extend(_run_rule(rule_cls, ctx))
        except Exception as exc:  # deliberate: rule isolation is the point
            result.rule_errors[rule_cls.meta.id] = f"{type(exc).__name__}: {exc}"

    collected, result.corroborated = _corroborate(ctx, collected)

    for rule_cls in deferred:
        result.rules_run += 1
        try:
            collected.extend(_run_rule(rule_cls, ctx))
        except Exception as exc:  # deliberate: rule isolation is the point
            result.rule_errors[rule_cls.meta.id] = f"{type(exc).__name__}: {exc}"

    result.total_raw = len(collected)

    def suppressed(finding: Finding) -> bool:
        path = ctx.root / finding.location.file
        return is_suppressed(
            ctx.lines(path), finding.location.line, finding.location.end_line, finding.rule_id
        )

    kept: list[Finding] = []
    for finding in collected:
        if suppressed(finding):
            result.suppressed_inline += 1
            continue
        # After every rule has run, never before parsing: see djaudit.pathfilter
        # for the measurement that settled this.
        if exclude_paths and excluded(finding.location.file, exclude_paths):
            result.suppressed_path += 1
            continue
        kept.append(scope.apply(finding))

    kept = fp.assign(kept)

    if external:
        kept = _fold_external(ctx, external, kept, result, suppressed)

    if baseline is not None:
        before = len(kept)
        kept = baseline.filter(kept)
        result.suppressed_baseline = before - len(kept)

    above = [f for f in kept if _passes_threshold(f, min_severity, min_confidence)]
    result.filtered_threshold = len(kept) - len(above)

    result.findings = sorted(above, key=lambda f: f.sort_key)
    result.duration_seconds = time.perf_counter() - started
    return result


def _fold_external(
    ctx: ProjectContext,
    adapters: Sequence[Adapter],
    ours: list[Finding],
    result: RunResult,
    suppressed: Callable[[Finding], bool],
) -> list[Finding]:
    """Add every external tool's findings to ours, under our own rules.

    Called *after* our findings have their identities and *before* the baseline
    and the thresholds, so an external finding is suppressed, baselined and
    ranked by exactly what governs one of ours. An adapter that bypassed them
    would turn `--external` into a way to defeat the user's own filters.
    """
    reports = _collect_external(ctx, adapters, result)
    merged = merge(ours, reports)
    result.external_notices = merged.notices
    result.external_diagnostics = merged.diagnostics
    result.external_duplicates = merged.duplicates
    result.total_raw += sum(len(report.findings) for report in reports)

    kept: list[Finding] = []
    for finding in merged.findings:
        if suppressed(finding):
            result.suppressed_inline += 1
            continue
        kept.append(finding)
    return kept


def _collect_external(
    ctx: ProjectContext, adapters: Sequence[Adapter], result: RunResult
) -> list[Report]:
    """Ask every adapter, and let none of them end the run.

    `Adapter.collect` already promises not to raise for anything its tool does,
    but the promise is a protocol's and an adapter is somebody else's object.
    The same isolation rules apply as to a rule that crashes: the failure is
    recorded under the tool's name and the other results survive, because a
    security tool that abandons an audit over an optional linter is worse than
    one that reports the linter is broken.
    """
    reports: list[Report] = []
    for adapter in adapters:
        try:
            reports.append(adapter.collect(ctx.root))
        except Exception as exc:  # deliberate: adapter isolation, as for rules
            result.rule_errors[adapter.name] = f"{type(exc).__name__}: {exc}"
    return reports


def _corroborate(ctx: ProjectContext, findings: list[Finding]) -> tuple[list[Finding], int]:
    """Merge Django's deployment check into our own findings, if it can run.

    Lives here rather than in a rule because no rule may edit another rule's
    output, and the whole point is that our `DJS` findings and Django's checks
    are two readings of one defect. Everything is imported inside the function:
    a static audit must not pay for `subprocess`, and `tests/test_import_cost`
    fails if it does.
    """
    if not ctx.live or ctx.live_context is None or ctx.manage_py is None:
        return findings, 0

    from djaudit.live.checks import run_deployment_check  # noqa: PLC0415
    from djaudit.live.corroborate import corroborate  # noqa: PLC0415
    from djaudit.live.sqlmigrate import Target  # noqa: PLC0415

    target = Target.of(ctx.live_context, ctx.manage_py)
    if target is None:
        # No `default` alias to describe. The deployment check itself does not
        # touch a database, but `Target` is the only interpreter this tool is
        # allowed to run -- it is built from what the user disclosed -- and
        # inventing a backend to satisfy the constructor would be fabricating
        # the one field we would then be reporting on.
        return findings, 0

    try:
        report = run_deployment_check(target)
    except OSError:
        # Same isolation as a rule: the deployment check is a subprocess into
        # someone else's project, and a run that cannot make it must degrade to
        # the static answer rather than lose every finding collected so far.
        return findings, 0

    result = corroborate(findings, report)
    ctx.deployment_report = report
    ctx.deployment_gaps = result.unclaimed
    return list(result.findings), result.merged


def _run_rule(rule_cls: type[Rule], ctx: ProjectContext) -> list[Finding]:
    return list(rule_cls().check(ctx))
