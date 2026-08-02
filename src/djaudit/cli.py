"""Command line interface.

Exit codes are chosen for CI:

* ``0`` -- nothing at or above ``--fail-on``
* ``1`` -- findings at or above ``--fail-on``
* ``2`` -- the tool could not run (bad path, unreadable baseline, bad options)

Keeping "found problems" and "tool broke" on different codes means a pipeline
can tell a real failure from a broken installation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from djaudit import __version__, engine
from djaudit.baseline import Baseline, BaselineError
from djaudit.models import Confidence, Family, Severity
from djaudit.registry import all_rules
from djaudit.reporters import OutputFormat, json_reporter, sarif, terminal

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

app = typer.Typer(
    name="djaudit",
    help="Django-aware static analysis: security, DRF authorization, ORM performance, "
    "migration safety and database portability.",
    no_args_is_help=True,
    add_completion=False,
)


def _fail(message: str) -> None:
    Console(stderr=True).print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(EXIT_ERROR)


@app.command()
def run(
    path: Annotated[
        Path,
        typer.Argument(help="Path to the Django project to audit."),
    ] = Path(),
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format."),
    ] = OutputFormat.TERMINAL,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write output to a file instead of stdout."),
    ] = None,
    min_severity: Annotated[
        Severity,
        typer.Option("--min-severity", help="Hide findings below this severity."),
    ] = Severity.LOW,
    min_confidence: Annotated[
        Confidence,
        typer.Option(
            "--min-confidence",
            help="Hide findings below this confidence. Tentative findings are excluded "
            "by default because they are the ones that generate noise.",
        ),
    ] = Confidence.FIRM,
    fail_on: Annotated[
        Severity,
        typer.Option("--fail-on", help="Exit non-zero when a finding reaches this severity."),
    ] = Severity.HIGH,
    family: Annotated[
        list[Family] | None,
        typer.Option("--family", help="Restrict to these rule families. Repeatable."),
    ] = None,
    select: Annotated[
        list[str] | None,
        typer.Option("--select", help="Run only these rule ids. Repeatable. Overrides --family."),
    ] = None,
    ignore: Annotated[
        list[str] | None,
        typer.Option("--ignore", help="Skip these rule ids. Repeatable."),
    ] = None,
    baseline_path: Annotated[
        Path | None,
        typer.Option("--baseline", help="Suppress findings recorded in this baseline file."),
    ] = None,
    write_baseline: Annotated[
        Path | None,
        typer.Option(
            "--write-baseline",
            help="Record all current findings to this file and exit 0. Use once, when "
            "adopting the tool on an existing codebase.",
        ),
    ] = None,
) -> None:
    """Audit a Django project."""
    if not path.exists():
        _fail(f"path does not exist: {path}")
    if not path.is_dir():
        _fail(f"path is not a directory: {path}")

    baseline: Baseline | None = None
    if baseline_path is not None:
        try:
            baseline = Baseline.load(baseline_path)
        except BaselineError as exc:
            _fail(str(exc))

    # Writing a baseline must capture everything, otherwise findings hidden by
    # the thresholds today would surface as "new" the moment someone lowers them.
    writing = write_baseline is not None
    result = engine.run(
        path,
        families=set(family) if family else None,
        include={s.upper() for s in select} if select else None,
        exclude={i.upper() for i in ignore} if ignore else None,
        min_severity=Severity.INFO if writing else min_severity,
        min_confidence=Confidence.TENTATIVE if writing else min_confidence,
        baseline=None if writing else baseline,
    )

    if write_baseline is not None:
        created = Baseline.from_findings(result.findings)
        created.save(write_baseline)
        Console().print(
            f"Wrote {len(created)} findings to [bold]{write_baseline}[/bold]. "
            "Future runs will report only new findings."
        )
        raise typer.Exit(EXIT_OK)

    _emit(result, output_format, output)

    worst = result.worst_severity
    if worst is not None and worst.rank >= fail_on.rank:
        raise typer.Exit(EXIT_FINDINGS)
    raise typer.Exit(EXIT_OK)


def _emit(result: engine.RunResult, output_format: OutputFormat, output: Path | None) -> None:
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)

    if output_format is OutputFormat.TERMINAL:
        if output is None:
            terminal.report(result, Console(file=sys.stdout))
            return
        # Context-managed so a reporter crash cannot leave a half-written,
        # unflushed report on disk that a CI step would then try to read.
        with output.open("w", encoding="utf-8") as handle:
            terminal.report(result, Console(file=handle))
        return

    renderer = json_reporter.render if output_format is OutputFormat.JSON else sarif.render
    text = renderer(result)
    if output:
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


@app.command(name="rules")
def list_rules() -> None:
    """List the rule catalogue."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("family")
    table.add_column("severity")
    table.add_column("confidence")
    table.add_column("tier")
    table.add_column("title")

    for rule_cls in all_rules():
        meta = rule_cls.meta
        table.add_row(
            meta.id,
            meta.family.value,
            meta.severity.value,
            meta.confidence.value,
            meta.tier.value,
            meta.title,
        )

    console = Console()
    console.print(table)
    console.print(f"[dim]{len(all_rules())} rules[/dim]")


@app.command(name="eval")
def evaluate_command(
    project: Annotated[
        Path,
        typer.Argument(help="Project containing an expected.json manifest."),
    ],
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="Manifest path, if not expected.json in the project."),
    ] = None,
) -> None:
    """Score the analyser against a manifest of expected findings.

    Intended as a CI regression gate: rules interact, and without a score nobody
    notices when a new rule quietly breaks an existing one's grading.
    """
    from djaudit.evaluation import ManifestError, evaluate  # noqa: PLC0415 - keeps `run` fast

    if not project.is_dir():
        _fail(f"not a directory: {project}")

    try:
        report = evaluate(project, manifest)
    except ManifestError as exc:
        _fail(str(exc))
        return

    console = Console()
    for line in report.failures():
        console.print(f"[red]{line}[/red]")

    console.print(
        f"precision [bold]{report.precision:.2%}[/bold]  "
        f"recall [bold]{report.recall:.2%}[/bold]  "
        f"f1 [bold]{report.f1:.2%}[/bold]  "
        f"[dim]tp={report.true_positives} fp={report.false_positives} "
        f"fn={report.false_negatives}[/dim]"
    )

    if not report.passed:
        raise typer.Exit(EXIT_FINDINGS)
    console.print("[green]evaluation passed[/green]")


@app.command()
def version() -> None:
    """Print the djaudit version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
