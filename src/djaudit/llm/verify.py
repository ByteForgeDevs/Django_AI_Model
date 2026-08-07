"""Check a proposed patch by applying it somewhere disposable.

A fix that looks right is not a fix. This copies the project, applies the patch
to the copy, and asks four questions of the result: does every changed file
still parse, did the finding the patch targeted actually go away, did any new
finding appear, and -- only if the operator supplies a command -- does the
project's own test suite still pass. A patch that fails any of these is
discarded, and the original is never touched at any point.

**What "verified" means without a test suite.** Four of those five checks are
static, so by default this proves the patch applies, the result parses, the
finding is gone and nothing new appeared. It does **not** prove the application
still works. `SECURE_HSTS_INCLUDE_SUBDOMAINS = True` passes every static check
and can still take a subdomain offline for a year. `Verification.confidence`
says which of the two was established, so no caller can print "verified" over
the weaker one by accident.

**Why the test command is not auto-detected.** Detecting `manage.py` and running
`manage.py test` would be the convenient thing and would quietly destroy the
property that makes djaudit safe to point at code you have not read: the static
tier parses and never executes. Importing a target's settings module runs
whatever is in it. So the command is run only when the operator names it, and
naming it is how they accept that consequence.

**Bisecting rather than verifying one at a time.** Verifying each patch in its
own copy is N copies and N engine runs, which on pretix is minutes. Patches are
verified together first, which is the common case and costs one of each; only
when the batch fails is it split, so the price of precision is paid only when
something is actually wrong.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from djaudit import engine
from djaudit.llm.fix import Fix, combined
from djaudit.models import Confidence, Finding

# Long enough for a real Django suite, short enough that a hung process is not
# a hung CI job.
SUITE_TIMEOUT = 900

# Copying a target is the expensive part; these are never worth carrying.
IGNORED = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "htmlcov",
    ".coverage",
)


class Level(StrEnum):
    """How much a verification actually established."""

    STATIC = "static"
    """The patch applies, parses, clears its finding and adds none."""

    TESTED = "tested"
    """All of the above, and the project's own suite still passes."""

    FAILED = "failed"
    """At least one check did not hold, so the patch is discarded."""


@dataclass(frozen=True)
class SuiteResult:
    """What the operator's own test command did."""

    command: str
    passed: bool
    code: int
    output: str


@dataclass(frozen=True)
class Verification:
    """The verdict on one batch of patches."""

    fixes: tuple[Fix, ...]
    parses: bool
    cleared: tuple[str, ...] = ()
    still_present: tuple[str, ...] = ()
    introduced: tuple[Finding, ...] = ()
    suite: SuiteResult | None = None
    problem: str = ""

    @property
    def accepted(self) -> bool:
        return (
            self.parses
            and not self.still_present
            and not self.introduced
            and not self.problem
            and (self.suite is None or self.suite.passed)
        )

    @property
    def confidence(self) -> Level:
        """Never let a static pass be reported as a tested one."""
        if not self.accepted:
            return Level.FAILED
        return Level.TESTED if self.suite is not None else Level.STATIC


@dataclass
class Report:
    """Which patches survived, and what happened to the ones that did not."""

    accepted: list[Fix] = field(default_factory=list)
    rejected: list[Verification] = field(default_factory=list)
    batch: Verification | None = None

    @property
    def confidence(self) -> Level:
        if not self.accepted:
            return Level.FAILED
        levels = [v.confidence for v in [self.batch] if v is not None]
        return levels[0] if levels else Level.STATIC


def scratch_copy(root: Path, into: Path) -> Path:
    """A disposable copy of the project. The original is never written to."""
    destination = into / root.name
    shutil.copytree(root, destination, ignore=IGNORED, symlinks=True)
    return destination


def run_suite(root: Path, command: str) -> SuiteResult:
    """Run the operator's own test command inside the copy.

    Shell interpretation is deliberate: the command came from whoever typed it
    on the command line, who could equally have typed it into their shell. It
    is not derived from the target, which is the part that would matter.
    """
    try:
        completed = subprocess.run(
            command,
            check=False,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=SUITE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return SuiteResult(command, passed=False, code=-1, output="the suite timed out")
    return SuiteResult(
        command=command,
        passed=completed.returncode == 0,
        code=completed.returncode,
        output=(completed.stdout + completed.stderr)[-4000:],
    )


def verify(
    proposed: Sequence[Fix],
    root: Path,
    before: Sequence[Finding],
    min_confidence: Confidence = Confidence.FIRM,
    command: str | None = None,
) -> Verification:
    """Apply a batch to a copy and report what held.

    `before` is the findings of the run the patches came from, so "introduced"
    means new relative to that run rather than to some fresh one whose settings
    might differ.
    """
    if not proposed:
        return Verification(fixes=(), parses=True)

    known = {f.fingerprint for f in before}
    targeted = {f.finding.fingerprint for f in proposed}

    with TemporaryDirectory(prefix="djaudit-verify-") as tmp:
        try:
            copy = scratch_copy(root, Path(tmp))
            for path, after in combined(proposed).items():
                (copy / path.relative_to(root)).write_text(after, encoding="utf-8")
        except (OSError, ValueError) as exc:
            return Verification(fixes=tuple(proposed), parses=False, problem=str(exc))

        for path in {f.path for f in proposed}:
            text = (copy / path.relative_to(root)).read_text(encoding="utf-8")
            try:
                ast.parse(text)
            except SyntaxError as exc:
                return Verification(
                    fixes=tuple(proposed),
                    parses=False,
                    problem=f"{path.name} no longer parses: {exc}",
                )

        after_run = engine.run(copy, min_confidence=min_confidence)
        remaining = {f.fingerprint for f in after_run.findings}
        suite = run_suite(copy, command) if command else None

    return Verification(
        fixes=tuple(proposed),
        parses=True,
        cleared=tuple(sorted(targeted - remaining)),
        still_present=tuple(sorted(targeted & remaining)),
        introduced=tuple(f for f in after_run.findings if f.fingerprint not in known),
        suite=suite,
    )


def verified(
    proposed: Sequence[Fix],
    root: Path,
    before: Sequence[Finding],
    min_confidence: Confidence = Confidence.FIRM,
    command: str | None = None,
) -> Report:
    """Keep the patches that hold up, dropping only the ones that do not.

    The whole batch is tried first. When it fails, each patch is verified alone
    rather than the batch being thrown away -- one bad fix should not cost the
    others, and "something in here is wrong" is not a useful thing to tell
    somebody.
    """
    report = Report()
    if not proposed:
        return report

    batch = verify(proposed, root, before, min_confidence, command)
    report.batch = batch
    if batch.accepted:
        report.accepted = list(proposed)
        return report

    for fix in proposed:
        alone = verify([fix], root, before, min_confidence, command)
        if alone.accepted:
            report.accepted.append(fix)
        else:
            report.rejected.append(alone)

    if report.accepted and len(report.accepted) != len(proposed):
        report.batch = verify(report.accepted, root, before, min_confidence, command)
        if not report.batch.accepted:
            report.rejected.append(report.batch)
            report.accepted = []
    return report


def explain_rejection(verification: Verification) -> str:
    """Why a patch was discarded, in one line."""
    if verification.problem:
        return verification.problem
    if verification.still_present:
        return "the finding it targeted is still reported after the change"
    if verification.introduced:
        rules = sorted({f.rule_id for f in verification.introduced})
        return f"it introduced {len(verification.introduced)} new finding(s): {', '.join(rules)}"
    if verification.suite is not None and not verification.suite.passed:
        return f"the project's own tests failed (exit {verification.suite.code})"
    return "it did not hold up"
