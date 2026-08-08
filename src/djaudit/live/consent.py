"""Turning `--live` into a live tier, and saying what that costs.

The live tier runs the target's own interpreter over the target's own
`manage.py`. That is not a stronger analysis of the same kind as the static
tier -- it is a different thing entirely, and it means **the audited project's
code executes on the auditing machine**. `settings.py` runs. Every module it
imports runs. A `.env` loader runs. Anything a repository's settings module
chooses to do at import time happens, with the file system and network access
of whoever typed the command.

That is a reasonable thing to do to your own project and an unreasonable thing
to have happen by surprise, so it is off unless asked for, and asking for it
prints what is about to happen and where. The notice names the interpreter and
the `manage.py`, because "target code will be executed" is a warning label and
`/home/you/proj/.venv/bin/python /home/you/proj/manage.py` is a fact the reader
can check.

**Consent is the flag, not a prompt.** This runs in CI far more often than at a
terminal, and a tool that blocks on a question nobody can answer is a tool that
gets run with `yes |` in front of it. `--live` is the answer; the notice is the
disclosure.

**It goes to stderr.** `--format json` and `--format sarif` write machine input
to stdout, and a security notice that corrupts the document it is warning you
about would be its own small joke.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from djaudit.live.context import LiveContext, Unavailable, inspect_target
from djaudit.live.interpreter import Search, find_interpreter


class Consent(NamedTuple):
    """What `--live` resolved to, and what to tell the reader about it.

    Carries the failure as data rather than raising, because a live tier that
    could not start is not a reason to abandon the audit -- the static tier
    still has 76 rules to run, and 4.1.4 reports the difference.
    """

    granted: bool
    """Whether the reader asked for the live tier at all."""

    context: LiveContext | None = None
    problem: str | None = None
    """Why the live tier is unavailable despite being asked for."""

    @property
    def available(self) -> bool:
        return self.context is not None

    def reason(self) -> str:
        """The `Degradation` reason, in the vocabulary 4.1.4 already reports."""
        if not self.granted:
            return "the live tier was not requested"
        if self.context is None:
            return f"the live tier was requested but is unavailable: {self.problem}"
        return "the live tier ran"


def notice(interpreter_path: Path, manage_py: Path) -> str:
    """The disclosure, printed before anything of the target's is executed.

    Names both paths. A reader who has just been told that code will run needs
    to know *whose*, and on a machine with several checkouts the answer is not
    obvious from the command line they typed.
    """
    return (
        "live tier enabled: djaudit will execute the target's own code.\n"
        f"  interpreter  {interpreter_path}\n"
        f"  entry point  {manage_py}\n"
        "  Its settings module and everything that module imports will be "
        "imported and run.\n"
        "  Run --no-live if this project is not one you trust."
    )


def resolve(
    root: Path,
    manage_py: Path | None,
    *,
    granted: bool,
    announce: Callable[[str], None] | None = None,
) -> Consent:
    """Find an interpreter, disclose, then ask the target what it is.

    `announce` is called with the notice *before* `inspect_target`, so the
    disclosure precedes the execution it is disclosing rather than describing
    it afterwards.
    """
    if not granted:
        return Consent(granted=False)
    if manage_py is None:
        return Consent(granted=True, problem="no manage.py was found in the target")

    search: Search = find_interpreter(root)
    if search.interpreter is None:
        return Consent(granted=True, problem=_no_interpreter(search))

    if announce is not None:
        announce(notice(search.interpreter.executable, manage_py))

    found = inspect_target(search.interpreter, manage_py)
    if isinstance(found, Unavailable):
        return Consent(granted=True, problem=found.explain())
    return Consent(granted=True, context=found)


def _no_interpreter(search: Search) -> str:
    """Say what was refused as well as that nothing was found.

    `find_interpreter` declines djaudit's own environment on purpose, and a
    reader who sees only "no virtualenv" while looking straight at one has been
    told the least useful true thing available.
    """
    if not search.rejected:
        return "no virtualenv was found in the target"
    tried = "; ".join(f"{item.path}: {item.reason}" for item in search.rejected)
    return f"no usable virtualenv was found in the target ({tried})"
