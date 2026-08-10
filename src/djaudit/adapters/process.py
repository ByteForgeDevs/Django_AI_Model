"""Running an external tool, and finding out whether we can.

This module reuses `djaudit.live.runner.run_command`, and that deserves a
sentence because `djaudit/live/` is the one package documented as "everything
here executes the code being audited, and is written as though the target were
hostile". Nothing in this module does that. `ruff` and `bandit` read the
target's source as text; they never import it, and a malicious `settings.py`
gets parsed rather than run.

The reuse is still right, for the two properties that have nothing to do with
who is hostile. A tool that hangs must be killed by process group rather than
left holding our pipes, and a subprocess must not inherit a CI job's
deployment tokens just because it happens to be convenient. Re-implementing
either one here would mean two copies of code whose failure mode is a hang.

What is *not* reused is the `Interpreter` machinery. These tools run from our
environment, not the target's; asking a project's virtualenv for its `ruff`
would mean running a binary the target chose, which is the one thing this
layer has no reason to do.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from djaudit.adapters.base import Availability
from djaudit.live.runner import Outcome, run_command

PROBE_TIMEOUT = 10.0
"""Seconds to wait for `--version`. A tool that cannot say its own name in ten
seconds is not one we should be waiting on."""

RUN_TIMEOUT = 120.0
"""Seconds to wait for an analysis run.

Measured, best of three: `ruff --select DJ,S` takes 0.26s over pretix's 1,225
files, 0.25s over netbox and 0.06s over healthchecks. The ceiling is here for
the pathological case, not the normal one, and it is generous on purpose --
bandit walks the same trees in Python rather than Rust.
"""


def probe_tool(
    executable: str, *, name: str | None = None, argument: str = "--version"
) -> Availability:
    """Whether `executable` is on `PATH` and will answer, and at what version.

    `name` is what the tool is *called*, which is not always what we run: a
    caller may hold a resolved path, and an `Availability.tool` is not just a
    label. It names the tool in every diagnostic, and `ClaimTable.as_finding`
    builds rule ids out of it -- so an absolute path would produce a finding
    called `/USR/BIN/RUFF-S324`, and rule ids are what fingerprints and
    baselines are keyed on. It also decides whether `read_version` recognises
    the first token of `ruff 0.16.1` as the tool's own name.

    Never raises. Every way this can fail -- not installed, installed but not
    executable, installed but hanging, installed but exiting non-zero -- comes
    back as an `Availability` carrying the reason, because a missing optional
    tool is an ordinary fact about a machine and not an error in an audit.
    """
    tool = name or executable
    found = shutil.which(executable)
    if found is None:
        return Availability(tool=tool, reason=f"`{executable}` is not on PATH")
    outcome = run_command([found, argument], cwd=Path.cwd(), timeout=PROBE_TIMEOUT)
    if not outcome.started:
        return Availability(tool=tool, reason=f"`{tool}` could not be started: {outcome.error}")
    if outcome.timed_out:
        return Availability(
            tool=tool, reason=f"`{tool} {argument}` did not answer in {PROBE_TIMEOUT:.0f}s"
        )
    if outcome.returncode != 0:
        return Availability(tool=tool, reason=f"`{tool} {argument}` exited {outcome.returncode}")
    return Availability(tool=tool, version=read_version(outcome.stdout, tool))


def read_version(stdout: str, tool: str) -> str:
    """The version string, from output whose format nobody promised us.

    Measured, the three tools do not agree even about how many lines to use:

        ruff --version        `ruff 0.16.1`
        bandit --version      `bandit 1.9.4` and then a second line naming the
                              Python it runs on
        pip-audit --version   `pip-audit 2.10.1`

    Reading the first line and taking its last whitespace-separated token
    handles all three, and degrades to the whole line rather than to an
    exception when a fourth tool does something else again. The version is
    evidence about where a finding came from, so being approximately right and
    never failing is the correct trade.
    """
    first = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
    if not first:
        return ""
    tokens = first.split()
    if len(tokens) >= 2 and tokens[0].lower().replace("_", "-") == tool.lower():
        return tokens[-1]
    return tokens[-1] if len(tokens) == 1 else first


def run_tool(command: list[str], *, root: Path, timeout: float = RUN_TIMEOUT) -> Outcome:
    """Run an analysis command against `root`, capturing everything it said.

    `cwd` is the project root so the tool resolves its own relative paths the
    way the caller expects. The `Outcome` is returned whatever happened; the
    adapter decides whether a non-zero exit means findings or a failure, and
    that differs per tool: `ruff` exits 1 precisely when it has something to
    say.
    """
    return run_command(command, cwd=root, timeout=timeout)
