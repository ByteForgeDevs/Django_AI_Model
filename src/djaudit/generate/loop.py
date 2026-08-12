"""Generate, audit, repair, audit again -- and know when to stop.

The loop is four lines of pseudocode and most of this module is the stopping
conditions, because the loop without them is worse than no loop.

**Nothing is written to the real destination until it is accepted.** Every
iteration materialises into a disposable copy of the project, made by the same
``scratch_copy`` the fix verifier has used since Phase 6. A run that fails
leaves the caller's project byte-identical, which is the difference between a
tool you can point at real work and one you run in a scratch directory.

**Auditing inside a copy of the real project is the point.** A generated app
audited in isolation cannot be audited at all: half the rules need the settings
module, the installed apps and the model graph to say anything. So the app is
written into a copy of the project it is destined for and the whole thing is
analysed, which means the findings are the findings that project will actually
have.

**Three stopping conditions, and only one of them is a counter.**

A counter alone stops a runaway loop and tells you nothing. The other two are
the ones that carry information:

*Convergence.* If an iteration clears nothing -- the same fingerprints come
back -- the model is not making progress and another call will not change that.
Stopping on the second identical finding set is cheaper than the ceiling and
gives a better message.

*Regression.* If an iteration declares less than the one before it, the model
removed a feature to silence a finding. That is the degenerate optimum of any
audit-until-clean loop and it must be a hard failure, not a warning: an empty
file passes every rule djaudit has.

**Suppressions are rejected outright.** A model that writes
``# djaudit: ignore`` has found the second degenerate optimum. The prompt says
not to, and this checks, because a prompt is a request.

**What "clean" means.** No findings at or above the caller's threshold, from 87
deterministic rules, in a copy of the real project. It does not mean the app
works, and nothing here runs it: the loop never imports the generated code or
the target's settings, because the static tier's whole claim is that it does
not. Deciding the app is correct is still the caller's job.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from djaudit import engine
from djaudit.generate.scaffold import FIELD_TO_FILENAME, Spec, write_app
from djaudit.generate.surface import Regression, Surface, surface_of
from djaudit.llm.provider import Answer, Declined, Prompt, Provider, Reply
from djaudit.llm.verify import scratch_copy
from djaudit.models import Confidence, Finding, Severity

# Enough for a model to react to a finding list twice. Beyond that, convergence
# has almost always already fired, and a run that has not improved in three
# attempts is not one call away from success.
DEFAULT_MAX_ITERATIONS = 4

# The markers a model might reach for to silence a rule instead of fixing it.
SUPPRESSIONS = ("djaudit: ignore", "djaudit:ignore", "noqa: DJ", "# type: ignore[djaudit")


class Outcome(StrEnum):
    """How a run ended. Every one of these is a different thing to tell a user."""

    CLEAN = "clean"
    """The generated app produced no findings at or above the threshold."""

    EXHAUSTED = "exhausted"
    """The iteration ceiling was reached with findings outstanding."""

    STALLED = "stalled"
    """An iteration cleared nothing, so further calls would not either."""

    REGRESSED = "regressed"
    """An iteration removed a feature to silence a finding. Hard failure."""

    SUPPRESSED = "suppressed"
    """The model tried to silence a rule instead of fixing it. Hard failure."""

    UNPARSEABLE = "unparseable"
    """The model returned code that is not valid Python."""

    DECLINED = "declined"
    """No model was available, or it refused. Nothing was generated."""

    @property
    def writable(self) -> bool:
        """Whether the result may be written to the caller's project.

        Only ``CLEAN`` and ``EXHAUSTED``. A stalled run is offered too --
        findings that a model cannot fix are still a working app a human can --
        but a regression or a suppression is a hard no, because accepting one
        would mean writing code the caller asked for and did not get.
        """
        return self in {Outcome.CLEAN, Outcome.EXHAUSTED, Outcome.STALLED}


@dataclass(frozen=True)
class Iteration:
    """One pass: what the model returned and what the auditor said about it."""

    number: int
    files: dict[str, str]
    findings: tuple[Finding, ...]
    surface: Surface

    @property
    def fingerprints(self) -> frozenset[str]:
        return frozenset(f.fingerprint for f in self.findings)


@dataclass
class Run:
    """The whole attempt, kept so a caller can show its work."""

    spec: Spec
    outcome: Outcome = Outcome.DECLINED
    iterations: list[Iteration] = field(default_factory=list)
    reason: str = ""
    regression: Regression | None = None
    written: list[Path] = field(default_factory=list)

    @property
    def final(self) -> Iteration | None:
        return self.iterations[-1] if self.iterations else None

    @property
    def findings(self) -> tuple[Finding, ...]:
        return self.final.findings if self.final else ()

    @property
    def cleared(self) -> int:
        """How many findings the loop removed. The headline number."""
        if len(self.iterations) < 2:
            return 0
        return max(0, len(self.iterations[0].findings) - len(self.iterations[-1].findings))

    @property
    def accepted(self) -> bool:
        return self.outcome.writable and self.final is not None


def _syntax_error(files: dict[str, str]) -> str:
    for name, source in files.items():
        try:
            ast.parse(source)
        except SyntaxError as exc:
            where = FIELD_TO_FILENAME.get(name, name)
            return f"{where} does not parse: {exc.msg} (line {exc.lineno})"
    return ""


def _suppression(files: dict[str, str]) -> str:
    for name, source in files.items():
        for marker in SUPPRESSIONS:
            if marker in source:
                return f"{FIELD_TO_FILENAME.get(name, name)} contains {marker!r}"
    return ""


def summarise(findings: Sequence[Finding], app: str) -> str:
    """The finding list as the model will see it.

    Findings outside the generated app are dropped. The model was asked for one
    app and cannot fix the project's settings module, and a list it can do
    nothing about is a list that dilutes the ones it can.
    """
    mine = in_app(findings, app)
    if not mine:
        return "no findings in the generated app"
    lines = []
    for finding in mine:
        name = finding.location.file.rsplit("/", 1)[-1]
        lines.append(
            f"{finding.rule_id}  {finding.severity.value}  "
            f"{name}:{finding.location.line}\n"
            f"  {finding.message}\n"
            f"  FIX: {finding.remediation}"
        )
    return "\n".join(lines)


def in_app(findings: Sequence[Finding], app: str) -> tuple[Finding, ...]:
    """Only the findings the model is responsible for.

    ``Location.file`` is POSIX and relative to the analysed root, so an app at
    the project root is matched by its leading segment; the ``/app/`` form
    covers a project whose apps live under a subdirectory.
    """
    prefix = f"{app}/"
    return tuple(
        f for f in findings if f.location.file.startswith(prefix) or f"/{app}/" in f.location.file
    )


@dataclass
class Loop:
    """Runs the generate-audit-repair cycle against a disposable copy."""

    provider: Provider
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    min_severity: Severity = Severity.LOW
    min_confidence: Confidence = Confidence.FIRM
    observer: Callable[[str], None] | None = None

    def _say(self, message: str) -> None:
        if self.observer is not None:
            self.observer(message)

    def run(
        self, spec: Spec, first: Prompt, repair: Callable[[dict[str, str], str], Prompt]
    ) -> Run:
        """Generate and repair until clean, stalled, exhausted or refused."""
        result = Run(spec=spec)

        reply = self.provider.ask(first)
        if isinstance(reply, Declined):
            result.outcome = Outcome.DECLINED
            result.reason = reply.reason
            return result

        files = dict(_content(reply))
        with TemporaryDirectory(prefix="djaudit-generate-") as tmp:
            for number in range(1, self.max_iterations + 1):
                stop = self._check(files)
                if stop is not None:
                    result.outcome, result.reason = stop
                    return result

                iteration = self._audit(spec, files, Path(tmp), number)
                previous = result.final
                result.iterations.append(iteration)
                self._say(f"iteration {number}: {len(iteration.findings)} finding(s) in {spec.app}")

                if previous is not None:
                    lost = previous.surface.missing_from(iteration.surface)
                    if lost:
                        result.outcome = Outcome.REGRESSED
                        result.regression = lost
                        result.reason = lost.describe()
                        return result

                if not iteration.findings:
                    result.outcome = Outcome.CLEAN
                    return result

                if previous is not None and iteration.fingerprints >= previous.fingerprints:
                    result.outcome = Outcome.STALLED
                    result.reason = (
                        f"iteration {number} cleared none of the "
                        f"{len(previous.findings)} finding(s) from the one before it"
                    )
                    return result

                if number == self.max_iterations:
                    result.outcome = Outcome.EXHAUSTED
                    result.reason = (
                        f"{len(iteration.findings)} finding(s) outstanding after "
                        f"{number} iteration(s)"
                    )
                    return result

                nxt = self.provider.ask(repair(files, summarise(iteration.findings, spec.app)))
                if isinstance(nxt, Declined):
                    result.outcome = Outcome.EXHAUSTED
                    result.reason = f"the model stopped answering: {nxt.reason}"
                    return result
                files = dict(_content(nxt))

        return result

    def _check(self, files: dict[str, str]) -> tuple[Outcome, str] | None:
        """The two hard refusals, before anything touches a filesystem."""
        broken = _syntax_error(files)
        if broken:
            return Outcome.UNPARSEABLE, broken
        silenced = _suppression(files)
        if silenced:
            return Outcome.SUPPRESSED, f"{silenced}. A suppression is not a fix."
        return None

    def _audit(self, spec: Spec, files: dict[str, str], tmp: Path, number: int) -> Iteration:
        """Write into a fresh copy of the project and analyse the whole thing."""
        workspace = tmp / f"iteration-{number}"
        workspace.mkdir()
        copy = scratch_copy(spec.project, workspace)
        write_app(spec, files, copy)
        outcome = engine.run(
            copy,
            min_severity=self.min_severity,
            min_confidence=self.min_confidence,
        )
        return Iteration(
            number=number,
            files=dict(files),
            findings=in_app(outcome.findings, spec.app),
            surface=surface_of(files),
        )


def _content(reply: Reply) -> dict[str, str]:
    """The validated fields, which are exactly the files that may be written."""
    if isinstance(reply, Answer):
        return {k: v for k, v in reply.content.items() if isinstance(v, str)}
    return {}


def commit(run: Run) -> list[Path]:
    """Write an accepted run into the caller's real project.

    Separate from the loop, and refuses anything the loop did not accept, so
    that "we decided this was good enough" and "we wrote it to disk" are two
    decisions rather than one.
    """
    if not run.accepted or run.final is None:
        raise ValueError(f"a {run.outcome.value} run is not written to the project")
    run.written = write_app(run.spec, run.final.files, run.spec.project)
    return run.written
