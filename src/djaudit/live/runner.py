"""Running a command in the target's environment, on our terms.

This is the only module in `djaudit` that executes the code being audited.
Everything about it is written on the assumption that the target is hostile, or
-- far more likely and just as damaging -- merely careless: a `settings.py` that
opens a socket at import time, a `manage.py` that prompts for input, a
`conftest` that never returns.

Four properties, each of which exists because its absence is a real failure.

**A hard timeout, enforced by killing the process group.** `subprocess.run`'s
`timeout` kills only the process it started. Django's `manage.py` spawns
children -- the autoreloader is the obvious one -- and a child that outlives its
parent inherits our pipes, so the read that follows the kill blocks on a
pipe nobody will ever close. The child is started in its own process group with
`start_new_session=True` and the whole group is signalled, so the timeout is a
timeout rather than a suggestion.

**No inherited secrets.** The environment is built from nothing rather than
copied from ours. A CI job's environment holds deployment tokens, registry
credentials and cloud keys, and handing them to a subprocess that runs
arbitrary code from the repository being audited would make djaudit a
credential exfiltration path — a supply-chain vulnerability introduced by a
security tool. Only the variables named in `PASSTHROUGH` cross, and the caller
adds specific ones explicitly.

**Output is captured, bounded, and never interleaved with ours.** A target that
writes a gigabyte to stdout should not exhaust our memory, and one that writes
ANSI escapes should not be able to rewrite our terminal. Streams are captured
separately and truncated at a fixed ceiling, and the fact of truncation is
recorded rather than hidden.

**stdin is closed.** A `manage.py` command that asks a question gets EOF rather
than blocking forever against a terminal that is not there. This is the failure
that most often looks like a hang, because it *is* a hang.

Nothing here decides *what* to run. Choosing commands, and trusting their
output, belongs to the callers in 4.1.3 and later.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from djaudit.live.interpreter import Creator, Interpreter

DEFAULT_TIMEOUT = 30.0
"""Seconds before a command is killed.

Generous for `sqlmigrate` on one migration, which is the busiest thing the live
tier asks for, and short enough that a hung target does not stall a CI job.
"""

OUTPUT_LIMIT = 1 << 20
"""Bytes kept from each stream. Past this the target is not saying anything we
are going to read; it is filling a buffer."""

PASSTHROUGH = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
    }
)
"""The variables a process needs to start at all, and nothing else.

`PATH` because Python resolves helper binaries through it, `HOME` because
without it Python and `psycopg` look up the passwd database and some
configurations fail outright, the locale pair because their absence changes how
Python decodes filenames, and the three Windows entries because on that
platform a process without them cannot spawn anything.

Deliberately excluded and worth naming: `DJANGO_SETTINGS_MODULE`, which would
let our own environment choose the target's settings; `PYTHONPATH`, which would
let our packages shadow theirs; `DATABASE_URL` and everything like it, which the
caller must pass explicitly if it wants them.
"""

REFUSED = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "DJANGO_SETTINGS_MODULE"})
"""Variables a caller may not add, because they redirect the interpreter itself.

`PYTHONPATH` and `PYTHONHOME` would let djaudit's packages shadow the target's,
which defeats the point of finding its interpreter in the first place.
`PYTHONSTARTUP` is executed before anything else runs.
`DJANGO_SETTINGS_MODULE` is excluded for a subtler reason: a live rule's whole
job is to observe which settings the target resolves, and setting it here would
mean measuring our own answer.
"""


class Outcome(NamedTuple):
    """What happened, with enough detail to put in a finding's evidence."""

    command: tuple[str, ...]
    returncode: int | None
    """`None` when the process was killed for running past the timeout."""

    stdout: str
    stderr: str
    duration: float
    truncated: bool
    """Whether either stream hit `OUTPUT_LIMIT` and lost its tail."""

    started: bool = True
    """False when the command could not be launched at all."""

    error: str | None = None
    """Why it could not be launched, or why it was killed."""

    @property
    def ok(self) -> bool:
        return self.started and self.returncode == 0

    @property
    def timed_out(self) -> bool:
        return self.started and self.returncode is None

    def describe(self) -> str:
        """One line naming the command and its fate, for a diagnostic.

        Whitespace in the arguments is collapsed, because `-c` carries a whole
        script and a "one line" summary that spans thirty is not one.
        """
        shown = " ".join(" ".join(part.split()) for part in self.command)
        if not self.started:
            return f"could not run `{shown}`: {self.error}"
        if self.timed_out:
            return f"`{shown}` was killed after {self.duration:.1f}s"
        return f"`{shown}` exited {self.returncode} in {self.duration:.1f}s"


def build_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a target command runs in: almost empty, on purpose.

    Built from nothing and filled from `PASSTHROUGH` rather than copied from
    ours and filtered, because a denylist is only as good as its last update
    and the cost of one missed entry is a leaked credential.
    """
    environment = {name: os.environ[name] for name in PASSTHROUGH if name in os.environ}
    # Byte-compiling the target's modules writes into its tree, which turns a
    # read-only audit into one that leaves traces.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Output is captured, so a pipe would otherwise be block-buffered and a
    # killed process would lose everything it had written.
    environment["PYTHONUNBUFFERED"] = "1"
    for name, value in (extra or {}).items():
        if name in REFUSED:
            raise ValueError(f"{name} would redirect the interpreter and cannot be set here")
        environment[name] = value
    return environment


def _truncate(raw: bytes) -> tuple[str, bool]:
    """Decode a captured stream, keeping the head and saying if it was cut.

    Decoded with `replace` because the target chooses this encoding and a
    `UnicodeDecodeError` here would turn its bad output into our crash.
    """
    truncated = len(raw) > OUTPUT_LIMIT
    return raw[:OUTPUT_LIMIT].decode("utf-8", errors="replace"), truncated


def _in_its_own_group(process: subprocess.Popen[bytes]) -> bool:
    """Whether signalling this process's group would reach only its own tree.

    Asked as a question about the running process rather than assumed from the
    `start_new_session` we requested, because the answer decides whether we are
    about to `SIGKILL` a subprocess or ourselves. The request is made in another
    function and is honoured only on posix; if the two ever disagree, the group
    we would signal is the one containing djaudit, the CI runner's shell, and on
    a developer's machine their terminal.

    This is not hypothetical. Removing `start_new_session` and running the
    timeout tests kills the test runner, its parent, and the shell that started
    it, and does so before anything reaches a log.
    """
    if os.name != "posix":
        return False
    try:
        return os.getpgid(process.pid) != os.getpgid(0)
    except OSError:
        # It exited before we could ask. `Popen.kill` handles that case.
        return False


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Kill the process *group*, then the process, and never raise.

    Signalling the group is the point: `manage.py` spawns children, and a child
    that outlives its parent keeps our pipes open, so the read after the kill
    blocks on a pipe nobody will close.
    """
    if _in_its_own_group(process):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except OSError:
            # It exited between the check and the signal.
            pass
    process.kill()


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT,
    extra_environment: Mapping[str, str] | None = None,
) -> Outcome:
    """Run `command` in `cwd` under a hard timeout, and capture what it said.

    Never raises for anything the target does. A command that cannot start, one
    that fails, and one that hangs all come back as an `Outcome`, because a
    live rule's job is to report what it observed rather than to propagate the
    target's problems as our exceptions.
    """
    arguments = tuple(command)
    environment = build_environment(extra_environment)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Its own process group, so the timeout can signal the whole tree.
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as exception:
        return Outcome(
            command=arguments,
            returncode=None,
            stdout="",
            stderr="",
            duration=time.monotonic() - started,
            truncated=False,
            started=False,
            error=f"{type(exception).__name__}: {exception}",
        )

    timed_out = False
    try:
        raw_out, raw_err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        # The group is gone, so this returns whatever was buffered rather than
        # blocking. Bounded anyway, because a killed leader does not guarantee
        # every descendant closed its end of the pipe.
        try:
            raw_out, raw_err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            raw_out, raw_err = b"", b""

    stdout, cut_out = _truncate(raw_out or b"")
    stderr, cut_err = _truncate(raw_err or b"")
    return Outcome(
        command=arguments,
        returncode=None if timed_out else process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration=time.monotonic() - started,
        truncated=cut_out or cut_err,
        error=f"exceeded the {timeout:g}s timeout and its process group was killed"
        if timed_out
        else None,
    )


def run_python(
    interpreter: Interpreter,
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT,
    extra_environment: Mapping[str, str] | None = None,
) -> Outcome:
    """Run the target's interpreter, isolated from ours.

    `-I` is the whole reason this wrapper exists rather than callers building
    the list themselves. It implies `-s` and `-E`, so the user site directory is
    ignored and `PYTHON*` variables are disregarded even if one reached the
    environment by a route this module did not anticipate. That makes the
    isolation belt-and-braces: `build_environment` refuses to *set* them, and
    `-I` means the interpreter would ignore them anyway.
    """
    return run_command(
        [str(interpreter.executable), "-I", *arguments],
        cwd=cwd,
        timeout=timeout,
        extra_environment=extra_environment,
    )


def probe(
    interpreter: Interpreter, expression: str, *, cwd: Path, timeout: float = 10.0
) -> Outcome:
    """Print one expression from inside the target's environment.

    The narrow waist of the live tier: rather than importing the target's code
    into our process, we ask its interpreter a question and read the answer off
    stdout as text. Whatever the target's Django does on import, it does over
    there, behind a timeout, in a process whose death costs us nothing.
    """
    return run_python(
        interpreter,
        ["-c", f"import sys; sys.stdout.write(str({expression}))"],
        cwd=cwd,
        timeout=timeout,
    )


def self_check() -> Interpreter:
    """djaudit's own interpreter, shaped as one, for tests and diagnostics.

    Not usable as a target -- `find_interpreter` refuses it by design -- but the
    runner has to be exercised against *something* real, and this is the one
    interpreter we know exists.
    """
    return Interpreter(
        executable=Path(sys.executable),
        prefix=Path(sys.prefix),
        creator=Creator.UNKNOWN,
        version=".".join(str(part) for part in sys.version_info[:3]),
        found_by="djaudit's own interpreter",
    )
