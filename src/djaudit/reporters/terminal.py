"""Human-facing terminal output."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from djaudit.engine import RunResult
from djaudit.models import Confidence, Finding, Severity

_SEVERITY_STYLE: dict[Severity, str] = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}

_CONFIDENCE_STYLE: dict[Confidence, str] = {
    Confidence.CERTAIN: "green",
    Confidence.FIRM: "yellow",
    Confidence.TENTATIVE: "dim",
}


def _header(result: RunResult, console: Console) -> None:
    ctx = result.context
    console.print()
    console.print(Text("djaudit", style="bold"), Text(str(ctx.root), style="dim"))

    details = [
        f"Django {ctx.django_version}" if ctx.django_version else "Django version unknown",
        f"{len(ctx.python_files)} python files",
        f"{len(ctx.settings_modules)} settings modules",
        f"{result.rules_run} rules",
    ]
    console.print(Text("  " + "  ·  ".join(details), style="dim"))

    if ctx.settings_modules:
        roles = ", ".join(f"{m.dotted} [{m.role.value}]" for m in ctx.settings_modules)
        console.print(Text(f"  {roles}", style="dim"))
    console.print()

    for diagnostic in ctx.diagnostics:
        console.print(Text(f"incomplete analysis: {diagnostic.message}", style="bold yellow"))
        console.print(Text(f"  {diagnostic.detail}", style="yellow"))
        console.print()

    if result.degraded:
        heading, *lines = result.degraded.report()
        console.print(Text(f"not checked: {heading}", style="bold yellow"))
        for line in lines:
            console.print(Text(f"  {line}", style="yellow"))
        console.print()


def _finding(finding: Finding, console: Console) -> None:
    severity_style = _SEVERITY_STYLE[finding.severity]

    heading = Text()
    heading.append(f"{finding.rule_id}  ", style="bold")
    heading.append(f"{finding.severity.value:<10}", style=severity_style)
    heading.append(f"{finding.confidence.value:<12}", style=_CONFIDENCE_STYLE[finding.confidence])
    heading.append(str(finding.location), style="bold blue")
    console.print(heading)

    console.print(Text(f"  {finding.message}"))

    if finding.location.snippet:
        for line in finding.location.snippet.splitlines():
            console.print(Text(f"  │ {line}", style="dim"))

    if finding.remediation:
        first = finding.remediation.strip().splitlines()[0]
        console.print(Text(f"  → {first}", style="green"))

    console.print(Text(f"  fingerprint {finding.fingerprint}", style="dim"))
    console.print()


def _summary(result: RunResult, console: Console) -> None:
    counts = result.counts_by_severity()

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("severity")
    table.add_column("count", justify="right")
    for severity in Severity:
        count = counts[severity]
        if count:
            table.add_row(
                Text(severity.value, style=_SEVERITY_STYLE[severity]),
                str(count),
            )
    if any(counts.values()):
        console.print(table)
        console.print()

    notes = [f"{len(result.findings)} reported"]
    if result.filtered_threshold:
        notes.append(f"{result.filtered_threshold} below threshold")
    if result.suppressed_inline:
        notes.append(f"{result.suppressed_inline} suppressed inline")
    if result.suppressed_baseline:
        notes.append(f"{result.suppressed_baseline} in baseline")
    notes.append(f"{result.duration_seconds:.2f}s")
    console.print(Text("  ·  ".join(notes), style="dim"))

    for rule_id, error in sorted(result.rule_errors.items()):
        console.print(Text(f"rule {rule_id} failed: {error}", style="bold yellow"))


def report(result: RunResult, console: Console) -> None:
    _header(result, console)
    if not result.findings:
        # Saying "no findings" in green after admitting we could not read the
        # settings would undo the warning printed two lines earlier.
        style = "yellow" if result.context.diagnostics else "green"
        console.print(Text("No findings above the configured thresholds.", style=style))
        console.print()
    else:
        for finding in result.findings:
            _finding(finding, console)
    _summary(result, console)
