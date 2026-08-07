"""Command line interface.

Exit codes are chosen for CI:

* ``0`` -- nothing at or above ``--fail-on``
* ``1`` -- findings at or above ``--fail-on``
* ``2`` -- the tool could not run (bad path, unreadable baseline, bad options),
  or ran but could not analyse enough of the project to be trusted

Keeping "found problems" and "tool broke" on different codes means a pipeline
can tell a real failure from a broken installation. Incomplete analysis belongs
with the latter: a green build from a run that never located the settings is a
worse outcome than a red one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from djaudit import __version__, engine
from djaudit.baseline import Baseline, BaselineError
from djaudit.llm import config as llm_config
from djaudit.llm.budget import Budget, Metered
from djaudit.llm.cache import Cache, Cached
from djaudit.llm.evaluate import Verdict
from djaudit.llm.provider import NullProvider, Provider
from djaudit.llm.suggest import render, suggest

# Imported by name rather than as a module: `djaudit.llm` re-exports a `triage`
# function, which shadows the submodule of the same name.
from djaudit.llm.triage import TriageRun, triage
from djaudit.models import Confidence, Family, Severity
from djaudit.registry import all_rules
from djaudit.reporters import OutputFormat, json_reporter, sarif, terminal

if TYPE_CHECKING:
    from djaudit.benchmark import BenchmarkReport

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

# A run with hundreds of corpus verdicts refuses hundreds of times, and the
# refusals are all the same sentence; showing every one buries the diffs above.
_REFUSALS_SHOWN = 5

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

    # A rule that crashed reported nothing, and nothing is what a clean project
    # also reports. Saying so on stderr keeps the two apart without corrupting
    # JSON or SARIF on stdout.
    if result.rule_errors:
        stderr = Console(stderr=True)
        for rule_id, message in sorted(result.rule_errors.items()):
            stderr.print(f"[bold red]rule crashed:[/bold red] {rule_id}: {message}")
        stderr.print(
            f"[yellow]warning:[/yellow] {len(result.rule_errors)} "
            f"rule(s) did not run; their silence is not a clean result"
        )

    # A blocking diagnostic means whole rule families never ran, so exiting 0
    # would tell CI the project is clean when nothing actually examined it.
    if any(d.blocking for d in result.context.diagnostics):
        Console(stderr=True).print(
            "[bold red]error:[/bold red] analysis was incomplete; "
            "this result does not mean the project is clean"
        )
        raise typer.Exit(EXIT_ERROR)

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
    summary: Annotated[
        Path | None,
        typer.Option(
            "--summary",
            help="Append a markdown report here, e.g. $GITHUB_STEP_SUMMARY.",
        ),
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

    if summary is not None:
        from djaudit.summary import write_evaluation  # noqa: PLC0415 - keeps `run` fast

        write_evaluation(report, summary)

    console.print(
        f"precision [bold]{report.precision:.2%}[/bold]  "
        f"recall [bold]{report.recall:.2%}[/bold]  "
        f"f1 [bold]{report.f1:.2%}[/bold]  "
        f"[dim]tp={report.true_positives} fp={report.false_positives} "
        f"fn={report.false_negatives}[/dim]"
    )

    # Say plainly that the numbers above describe a run that did not finish.
    # `djaudit run` has refused to exit 0 on a blocking diagnostic since Phase
    # 1; scoring one and calling it a result would make the exit code depend on
    # which command happened to be used.
    if report.incomplete or report.rule_errors:
        Console(stderr=True).print(
            "[bold red]error:[/bold red] the analysis was incomplete; "
            "these scores describe a run that did not examine the project"
        )

    if not report.passed:
        raise typer.Exit(EXIT_FINDINGS)
    console.print("[green]evaluation passed[/green]")


@app.command(name="triage")
def triage_command(
    path: Annotated[
        Path,
        typer.Argument(help="Path to the Django project to audit."),
    ] = Path(),
    min_severity: Annotated[
        Severity,
        typer.Option("--min-severity", help="Hide findings below this severity."),
    ] = Severity.LOW,
    min_confidence: Annotated[
        Confidence,
        typer.Option("--min-confidence", help="Hide findings below this confidence."),
    ] = Confidence.FIRM,
    enable_llm: Annotated[
        bool | None,
        typer.Option(
            "--llm/--no-llm",
            help="Consult a configured model on findings the corpus does not settle. "
            "Off unless both this and [tool.djaudit.llm] enable it.",
        ),
    ] = None,
    show_suggestions: Annotated[
        bool,
        typer.Option(
            "--suggest",
            help="Also print suppression comments for findings judged an accepted risk. "
            "Nothing is written; the diffs are for you to apply.",
        ),
    ] = False,
) -> None:
    """Rank findings by whether they are worth a reviewer's time.

    Findings under a rule that three real Django projects judged unanimously are
    settled from that record. Everything else is put to a model if one is
    configured, and reported as undecided if not -- which is the default, and a
    useful answer: it is the shortlist of findings that actually need a human.
    """
    if not path.is_dir():
        _fail(f"path is not a directory: {path}")

    result = engine.run(path, min_severity=min_severity, min_confidence=min_confidence)

    try:
        config = llm_config.resolve(path / "pyproject.toml", enable=enable_llm)
    except llm_config.ConfigError as exc:
        _fail(str(exc))
        return

    provider = _build_provider(config)
    run = triage(result.findings, provider)
    console = Console()
    _print_triage(console, run, provider_name=provider.name)
    if show_suggestions:
        _print_suggestions(console, run, path)

    if any(d.blocking for d in result.context.diagnostics):
        Console(stderr=True).print(
            "[bold red]error:[/bold red] analysis was incomplete; "
            "this ranking does not cover the whole project"
        )
        raise typer.Exit(EXIT_ERROR)
    raise typer.Exit(EXIT_OK)


def _build_provider(config: llm_config.LLMConfig) -> Provider:
    """Assemble the provider stack, which is a null one unless told otherwise.

    The cache goes outermost so a repeat question never reaches the meter at
    all -- `Metered` also refuses to charge for a cached answer, but not
    consulting the budget is cheaper than consulting it and forgiving it, and
    it leaves the cache able to tag each entry with its finding's fingerprint.

    There is no branch here that reaches a third party. No such provider is
    implemented, and this returns a declining one saying so, because a stack
    that silently does nothing is indistinguishable from one that is broken.
    """
    allowed, reason = config.usable
    if not allowed:
        return NullProvider(reason)
    unimplemented = NullProvider(f"provider {config.provider!r} is not implemented yet")
    metered = Metered(
        inner=unimplemented,
        budget=Budget(max_tokens=config.max_tokens, max_calls=config.max_calls),
    )
    return Cached(inner=metered, cache=Cache(directory=config.cache_dir))


def _print_triage(console: Console, run: TriageRun, *, provider_name: str) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("verdict")
    table.add_column("source")
    table.add_column("rule")
    table.add_column("severity")
    table.add_column("location")

    styles = {
        Verdict.TRUE_POSITIVE: "bold red",
        Verdict.ABSTAINED: "yellow",
        Verdict.ACCEPTED_RISK: "dim",
    }
    for judgement in run.ranked:
        finding = judgement.finding
        table.add_row(
            f"[{styles[judgement.verdict]}]{judgement.verdict.value}[/]",
            judgement.source.value,
            finding.rule_id,
            finding.severity.value,
            f"{finding.location.file}:{finding.location.line}",
        )
    console.print(table)

    console.print(
        f"{len(run.judgements)} findings · "
        f"{run.counting(Verdict.TRUE_POSITIVE)} worth fixing · "
        f"{run.counting(Verdict.ACCEPTED_RISK)} judged acceptable · "
        f"{run.counting(Verdict.ABSTAINED)} undecided"
    )
    console.print(
        f"{run.skipped} settled from the recorded corpus, "
        f"{run.asked} put to [bold]{provider_name}[/bold], "
        f"{run.declined} unanswered"
    )
    if not run.consulted_a_model:
        # Said plainly, because a table of verdicts looks equally authoritative
        # either way and the undecided rows are the ones a human still owns.
        console.print(
            "[yellow]no model was consulted[/yellow]: every undecided finding above is "
            "one this corpus cannot settle, and needs a person."
        )


def _print_suggestions(console: Console, run: TriageRun, root: Path) -> None:
    """Print the suppressions this run would justify, and the ones it would not.

    An offline run reaches here and prints nothing but refusals, which is the
    designed outcome: the corpus prior ranks findings, and ranking is reversible
    in a way that a comment committed to somebody's source is not.
    """
    proposals, refused = suggest(run, root)

    console.print()
    if proposals:
        console.print("[bold]suggested suppressions[/bold] (not applied):")
        console.print(Syntax(render(proposals), "diff", theme="ansi_dark"))
    else:
        console.print("[bold]no suppression is justified by this run[/bold]")

    if refused:
        # The refusals are the point when nothing is proposed, and worth seeing
        # even when something is: they say which findings stay a human's problem.
        console.print(f"[dim]{len(refused)} not offered:[/dim]")
        for reason in refused[:_REFUSALS_SHOWN]:
            console.print(f"  [dim]- {reason}[/dim]")
        if len(refused) > _REFUSALS_SHOWN:
            console.print(f"  [dim]... and {len(refused) - _REFUSALS_SHOWN} more[/dim]")


@app.command()
def benchmark(
    project: Annotated[
        Path,
        typer.Argument(help="Checkout of a real-world Django project to audit."),
    ],
    triage_file: Annotated[
        Path,
        typer.Option("--triage", "-t", help="Triage file holding recorded verdicts."),
    ],
    update: Annotated[
        bool,
        typer.Option(
            "--update",
            help="Add untriaged findings to the triage file for review, then exit non-zero.",
        ),
    ] = False,
    summary: Annotated[
        Path | None,
        typer.Option(
            "--summary",
            help="Append a markdown report here, e.g. $GITHUB_STEP_SUMMARY.",
        ),
    ] = None,
) -> None:
    """Measure precision against a real project with recorded verdicts.

    Mature open-source projects measure precision and crash-resistance, not
    recall: we cannot know what they contain that we missed. Recall is the
    planted-defect fixtures' job.

    Fails when a finding is untriaged, when a family exceeds its false-positive
    budget, when a known-real finding stops firing, or when a rule crashes.
    """
    from djaudit.benchmark import run_benchmark  # noqa: PLC0415 - keeps `run` fast
    from djaudit.triage import Triage, TriageEntry, TriageError, Verdict  # noqa: PLC0415

    if not project.is_dir():
        _fail(f"not a directory: {project}")

    try:
        report = run_benchmark(project, triage_file)
    except TriageError as exc:
        _fail(str(exc))
        return

    console = Console()
    _print_benchmark(console, report)

    if summary is not None:
        from djaudit.summary import write_benchmark  # noqa: PLC0415 - keeps `run` fast

        write_benchmark(report, summary)

    if update and report.untriaged:
        # Seeded as false_positive so an unreviewed entry can never silently
        # count in our favour. A human flips the ones we got right.
        triage = Triage.load(triage_file)
        triage.with_entries(
            TriageEntry.from_finding(f, Verdict.FALSE_POSITIVE, note="TODO: review")
            for f in report.untriaged
        ).save(triage_file)
        console.print(
            f"[yellow]wrote {len(report.untriaged)} entries to {triage_file} "
            f"as false_positive — review each before committing[/yellow]"
        )

    if not report.ok:
        raise typer.Exit(EXIT_FINDINGS)
    console.print(f"[green]{report.summary()}[/green]")


def _print_benchmark(console: Console, report: BenchmarkReport) -> None:
    # Printed first: precision measured over a project that was never
    # discovered is 100% of nothing, and the reader needs to know that before
    # reaching the table.
    for diagnostic in report.incomplete:
        console.print(
            f"[bold red]analysis incomplete[/bold red] {diagnostic.code}: {diagnostic.message}"
        )

    for rule_id, message in sorted(report.rule_errors.items()):
        console.print(f"[red]rule crashed[/red] {rule_id}: {message}")

    for finding in report.untriaged:
        console.print(
            f"[yellow]untriaged[/yellow] {finding.rule_id} "
            f"{finding.location.file}:{finding.location.line} "
            f"[dim]{finding.fingerprint}[/dim]"
        )

    for entry in report.regressed:
        console.print(
            f"[red]regressed[/red] {entry.rule_id} {entry.file}:{entry.line} "
            f"[dim]judged {entry.verdict.value}, no longer reported[/dim]"
        )

    for entry in report.resolved:
        console.print(
            f"[green]resolved[/green] {entry.rule_id} {entry.file}:{entry.line} "
            f"[dim]known false positive, no longer reported[/dim]"
        )

    for misfiled in report.misfiled:
        console.print(
            f"[red]misfiled[/red] {misfiled.recorded} "
            f"[dim]is now {misfiled.actual} -- re-read the note before trusting it[/dim]"
        )

    if report.scores:
        table = Table(title=f"{report.target} precision", header_style="bold")
        table.add_column("family")
        table.add_column("reported", justify="right")
        table.add_column("tp", justify="right")
        table.add_column("fp", justify="right")
        table.add_column("accepted", justify="right")
        table.add_column("fp rate", justify="right")
        for score in report.scores:
            over = score.false_positive_rate > report.max_false_positive_rate
            table.add_row(
                score.family,
                str(score.reported),
                str(score.true_positives),
                str(score.false_positives),
                str(score.accepted_risks),
                f"[red]{score.false_positive_rate:.1%}[/red]"
                if over
                else f"{score.false_positive_rate:.1%}",
            )
        console.print(table)

    for score in report.over_budget:
        console.print(
            f"[red]over budget[/red] {score.family} false-positive rate "
            f"{score.false_positive_rate:.1%} exceeds {report.max_false_positive_rate:.1%}"
        )


@app.command()
def version() -> None:
    """Print the djaudit version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
