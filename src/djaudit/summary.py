"""Markdown summaries for CI job pages.

Exit codes tell CI whether to fail; they tell a human nothing. Someone looking
at a red build wants the numbers -- which family got noisier, by how much, and
against what budget -- without downloading an artifact or re-running the audit
locally. Anything that makes the answer slower to reach makes the gate easier
to ignore.

Written to ``$GITHUB_STEP_SUMMARY`` in append mode: several steps share that
file, and truncating it would delete another job's output.
"""

from __future__ import annotations

from pathlib import Path

from djaudit.benchmark import BenchmarkReport
from djaudit.evaluation import EvalReport


def _append(path: Path, markdown: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(markdown.rstrip() + "\n\n")


def benchmark_summary(report: BenchmarkReport) -> str:
    verdict = "✅ pass" if report.ok else "❌ fail"
    lines = [
        f"## Precision — {report.target}",
        "",
        f"{verdict} · **{report.python_files}** Python files · "
        f"**{report.reported}** reported · precision **{report.precision_display}**",
        "",
    ]

    if report.scores:
        lines += [
            "| family | reported | true | false | accepted | fp rate | budget |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for score in report.scores:
            over = score.false_positive_rate > report.max_false_positive_rate
            mark = " ⚠️" if over else ""
            lines.append(
                f"| {score.family} | {score.reported} | {score.true_positives} | "
                f"{score.false_positives} | {score.accepted_risks} | "
                f"{score.false_positive_rate:.1%}{mark} | "
                f"{report.max_false_positive_rate:.0%} |"
            )
        lines.append("")

    sections: list[tuple[str, list[str]]] = [
        (
            f"Untriaged ({len(report.untriaged)})",
            [
                f"- `{f.rule_id}` {f.location.file}:{f.location.line} — `{f.fingerprint}`"
                for f in report.untriaged
            ],
        ),
        (
            f"Regressed ({len(report.regressed)})",
            [
                f"- `{e.rule_id}` {e.file}:{e.line} — judged {e.verdict.value}, no longer reported"
                for e in report.regressed
            ],
        ),
        (
            f"Resolved ({len(report.resolved)})",
            [f"- `{e.rule_id}` {e.file}:{e.line}" for e in report.resolved],
        ),
        (
            f"Rule errors ({len(report.rule_errors)})",
            [f"- `{rule_id}` — {msg}" for rule_id, msg in sorted(report.rule_errors.items())],
        ),
    ]
    for heading, body in sections:
        if body:
            lines += [f"### {heading}", "", *body, ""]

    if report.untriaged:
        lines += [
            "> Run `djaudit benchmark <target> --triage benchmarks/"
            f"{report.target}.json --update` and record a verdict for each.",
            "",
        ]

    return "\n".join(lines)


def evaluation_summary(report: EvalReport) -> str:
    verdict = "✅ pass" if report.passed else "❌ fail"
    lines = [
        "## Recall — planted defects",
        "",
        f"{verdict}",
        "",
        "| metric | value |",
        "| --- | ---: |",
        f"| precision | {report.precision:.1%} |",
        f"| recall | {report.recall:.1%} |",
        f"| f1 | {report.f1:.1%} |",
        f"| true positives | {report.true_positives} |",
        f"| false positives | {report.false_positives} |",
        f"| false negatives | {report.false_negatives} |",
        "",
    ]

    failures = report.failures()
    if failures:
        lines += ["### Failures", "", *[f"- {line}" for line in failures], ""]

    return "\n".join(lines)


def write_benchmark(report: BenchmarkReport, path: Path) -> None:
    _append(path, benchmark_summary(report))


def write_evaluation(report: EvalReport, path: Path) -> None:
    _append(path, evaluation_summary(report))
