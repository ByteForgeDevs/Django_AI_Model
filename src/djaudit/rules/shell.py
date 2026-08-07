"""`DJI-008` -- request data reaching a shell.

The substep title says "``subprocess`` with ``shell=True`` or ``os.system``",
and that is two thirds of a rule. Which shapes actually let a payload start a
*second* command was settled by running each one against ``hi; touch SENTINEL``
and checking the filesystem afterwards, because the return value of a shell call
cannot be trusted to say whether something ran:

| shape | second command ran |
|---|---|
| ``os.system(s)`` | **yes** |
| ``os.popen(s)`` | **yes** |
| ``subprocess.getoutput(s)`` | **yes** |
| ``subprocess.run(s, shell=True)`` | **yes** |
| ``subprocess.Popen(s, shell=True)`` | **yes** |
| ``subprocess.run(["sh", "-c", s])`` | **yes** |
| ``subprocess.run(s)`` | no -- ``FileNotFoundError`` |
| ``subprocess.run(["echo", s])`` | no |
| ``subprocess.run(["echo", s], shell=True)`` | **no** |

Two of those rows are the reason this rule is not a grep for ``shell=True``.

**A string command without a shell is a crash, not a vulnerability.**
``subprocess.run("hi; touch X")`` raises ``FileNotFoundError``: the whole string
is taken as one program name, so nothing is parsed and nothing is split. This is
the same shape as PyYAML's missing ``Loader`` in `DJI-007` -- conventional advice
reports it, and what it would be reporting is a traceback.

**``shell=True`` with a list does not make the list dangerous.** On POSIX,
``subprocess`` passes ``["echo", s]`` to ``/bin/sh -c "echo" "s"``, so only
element 0 becomes the command and everything after it is set as the shell's own
positional parameters. The sentinel confirms it: ``["echo", payload]`` with
``shell=True`` did not run the payload, while ``[payload, "ignored"]`` did.
Flagging every element of a list because the call also says ``shell=True`` would
be wrong on the common shape and right only on the rare one.

**The shell can also arrive as the program.** ``["sh", "-c", s]`` has no
``shell`` keyword at all and runs ``s`` as a shell command. A rule keyed on
``shell=True`` misses it entirely, so the program name is checked too.

``shlex.quote`` is honoured as a sanitiser, having been measured to neutralise
the payload through f-strings, concatenation, ``%`` formatting and ``join``. It
is deliberately *not* added to the taint model's global sanitiser set: quoting
makes a string safe as one shell word, and says nothing about the same string
reaching SQL or ``eval``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from djaudit.dataflow.chains import def_use
from djaudit.dataflow.taint import Taint, request_source, taint_of
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Severity,
    Tier,
)
from djaudit.registry import Rule, RuleMeta, register
from djaudit.rules._injection import own_calls

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.scopes import Scope

ALWAYS_SHELL = {
    "os": {
        "system": "runs its argument through the shell",
        "popen": "runs its argument through the shell",
    },
    "subprocess": {
        "getoutput": "runs its argument through the shell",
        "getstatusoutput": "runs its argument through the shell",
    },
}
"""Calls that reach a shell whatever their keywords say."""

RUNNERS = frozenset({"run", "call", "check_call", "check_output", "Popen"})
"""``subprocess`` entry points that take a ``shell`` keyword and obey it."""

SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "ash", "busybox"})
"""Programs that treat an argument as a command line rather than as a filename."""

COMMAND_FLAGS = frozenset({"-c", "-lc", "-ic"})
"""Flags after which one of these programs reads a command string."""

QUOTERS = frozenset({"quote", "join"})
"""``shlex`` helpers measured to render client text inert as a shell word."""

WORDS = ("system", "popen", "subprocess", "getoutput", "getstatusoutput")
"""A necessary condition on a file's text: no shell sink can be spelled without one.

``os.system`` and ``os.popen`` name their function, and every ``subprocess``
entry point is reached through an attribute whose immediate holder is the module
name -- :func:`_module` reads only that holder, so an aliased import is outside
what the matcher can act on anyway.
"""

SOURCE_WORDS = ("request", "self.kwargs")
"""The taint model's own necessary condition, reused as a prefilter.

``request_source`` recognises an attribute of the name ``request`` and the
literal ``self.kwargs``, and taint does not cross a scope boundary, so a file
holding neither string can never produce a tainted argument.
"""


@dataclass(frozen=True, slots=True)
class Command:
    """One call that hands a payload to a shell."""

    call: ast.Call
    payload: ast.expr
    name: str
    behaviour: str


def _module(node: ast.Attribute) -> str | None:
    """The immediate holder of an attribute, when it is a plain name."""
    holder = node.value
    return holder.id if isinstance(holder, ast.Name) else None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _is_true(node: ast.expr | None) -> bool:
    """Whether an expression is the literal ``True``.

    A variable is not accepted. ``shell=flag`` may well be true at runtime, but
    the rule cannot show it, and a critical finding is not the place to guess.
    """
    return isinstance(node, ast.Constant) and node.value is True


def _text(node: ast.expr) -> str | None:
    """The literal string an expression is, or ``None`` if it is not one."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _program(name: str) -> bool:
    """Whether a literal argv[0] names a shell, with or without a directory."""
    return name.rsplit("/", 1)[-1] in SHELLS


def _elements(node: ast.expr) -> tuple[ast.expr, ...] | None:
    """The elements of a list or tuple display, or ``None`` for anything else."""
    return tuple(node.elts) if isinstance(node, (ast.List, ast.Tuple)) else None


def _shell_command(argv: tuple[ast.expr, ...]) -> ast.expr | None:
    """The element of *argv* a shell program would run as a command line.

    ``["sh", "-c", payload]`` runs ``payload``. Anything before the flag is the
    program and its options; anything after the command is set as the shell's
    positional parameters and is not executed.
    """
    if not argv:
        return None
    leader = _text(argv[0])
    if leader is None or not _program(leader):
        return None
    for index, element in enumerate(argv[1:], start=1):
        flag = _text(element)
        if flag in COMMAND_FLAGS and index + 1 < len(argv):
            return argv[index + 1]
    return None


def commands(node: ast.Call) -> Iterator[Command]:
    """Every payload this call would hand to a shell.

    Shared by the file prefilter and the body, so the two can never disagree
    about what counts as a sink.
    """
    if not isinstance(node.func, ast.Attribute):
        return
    module = _module(node.func)
    if module is None:
        return
    function = node.func.attr

    behaviour = ALWAYS_SHELL.get(module, {}).get(function)
    if behaviour is not None:
        if node.args:
            yield Command(node, node.args[0], f"{module}.{function}", behaviour)
        return

    if module != "subprocess" or function not in RUNNERS or not node.args:
        return

    first = node.args[0]
    argv = _elements(first)

    if _is_true(_keyword(node, "shell")):
        if argv is None:
            yield Command(
                node,
                first,
                f"subprocess.{function}",
                "was given shell=True, so its first argument is a command line",
            )
        elif argv:
            # Measured: with a list, only element 0 becomes the command. The
            # rest are handed to the shell as its own positional parameters.
            yield Command(
                node,
                argv[0],
                f"subprocess.{function}",
                "was given shell=True, so the first element of its argument list "
                "is the command line",
            )
        return

    if argv is not None:
        command = _shell_command(argv)
        if command is not None:
            yield Command(
                node,
                command,
                f"subprocess.{function}",
                "runs a shell, which reads the argument after -c as a command line",
            )


def quoted(node: ast.expr, chains: DefUse | None) -> bool:
    """Whether every tainted part of *node* passed through ``shlex``.

    The payload is usually a name, and the quoting is written where that name
    was built, so this follows reaching definitions the way `DJI-006`'s
    allowlist guard does rather than looking only at the expression at the call.
    """
    return _quoted(node, chains, set())


def _quoted(node: ast.expr, chains: DefUse | None, seen: set[str]) -> bool:
    tainted = [
        inner
        for inner in ast.walk(node)
        if isinstance(inner, (ast.Attribute, ast.Subscript, ast.Name, ast.Call))
        and taint_of(inner, chains) is Taint.TAINTED
    ]
    if not tainted:
        return False
    for inner in tainted:
        if _is_quoter(inner):
            return True
    for inner in tainted:
        if not isinstance(inner, ast.Name) or inner.id in seen or chains is None:
            continue
        seen.add(inner.id)
        for binding in chains.reaching(inner):
            if binding.value is not None and _quoted(binding.value, chains, seen):
                return True
    return False


def _is_quoter(node: ast.expr) -> bool:
    """Whether this expression is a ``shlex.quote`` or ``shlex.join`` call."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return _module(node.func) == "shlex" and node.func.attr in QUOTERS


@register
class ShellInjection(Rule):
    """`DJI-008` -- request data reaching a shell command line."""

    meta = RuleMeta(
        id="DJI-008",
        title="Request data used to build a shell command",
        family=Family.DJI,
        severity=Severity.CRITICAL,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "A shell reads its input as a language, not as a filename. Semicolons, "
            "backticks, $(...) and pipes are all instructions, so a request value "
            "spliced into a command line does not choose an argument, it appends "
            "commands. Each shape reported here was measured running a second "
            "command from its payload, which means the process executes whatever "
            "the client writes, with the application's own privileges and its own "
            "database credentials in the environment."
        ),
        remediation=(
            "Pass an argument list and no shell: subprocess.run([tool, value]) hands "
            "value to the program as one argument however it is spelled, which was "
            "measured not to run an embedded command. Where a shell is genuinely "
            "required, wrap every interpolated value in shlex.quote, which was "
            "measured to neutralise the same payload. Better still, validate the "
            "value against a fixed set first: most command lines built from a "
            "request only ever need one of a handful of permitted values."
        ),
        references=(
            "https://docs.python.org/3/library/subprocess.html#security-considerations",
            "https://docs.python.org/3/library/shlex.html#shlex.quote",
            "https://cwe.mitre.org/data/definitions/78.html",
            "https://owasp.org/www-community/attacks/Command_Injection",
        ),
        limitations=(
            "A payload is reported only when taint analysis proves it came from the "
            "request. A command built from a model field, a setting or a helper's "
            "parameter is unknown rather than tainted and is not reported.",
            "A string first argument with no shell keyword is not reported. It was "
            "measured to raise FileNotFoundError, because the whole string is taken "
            "as one program name, so the defect is a crash rather than an injection.",
            "With shell=True and a list argument only the first element is reported. "
            "POSIX passes the rest to the shell as positional parameters, which was "
            "measured not to execute them.",
            "A shell keyword that is a variable rather than the literal True is not "
            "reported, because the rule cannot show which way it resolves and this "
            "finding is too severe to raise on a guess.",
            "Quoting is recognised as shlex.quote or shlex.join by name. A project "
            "that wraps either in its own helper is not recognised, so such a call "
            "is reported even though the payload is escaped.",
        ),
    )

    def admits(self, tree: ast.AST) -> bool:
        """Whether this file holds a shell sink worth building a scope tree for."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for _ in commands(node):
                return True
        return False

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or not any(word in source for word in WORDS):
                continue
            if not any(word in source for word in SOURCE_WORDS):
                continue
            tree = ctx.parse(path)
            if tree is None or not self.admits(tree):
                continue
            root = ctx.scopes(path)
            if root is None:
                continue
            yield from self.inspect(ctx, path, root)

    def inspect(self, ctx: ProjectContext, path: Path, scope: Scope) -> Iterator[Finding]:
        """Walk one scope's own calls, then its children.

        Def-use chains are built only for scopes that hold a syntactic match,
        because they cost far more than the test that decides whether they are
        wanted.
        """
        here = [found for node in own_calls(scope) for found in commands(node)]
        if here:
            chains = def_use(scope)
            for found in here:
                if taint_of(found.payload, chains) is not Taint.TAINTED:
                    continue
                if quoted(found.payload, chains):
                    continue
                yield self.report(ctx, path, found, chains)
        for child in scope.children:
            yield from self.inspect(ctx, path, child)

    def report(
        self, ctx: ProjectContext, path: Path, found: Command, chains: DefUse | None
    ) -> Finding:
        written = ast.unparse(found.payload)
        source = _source_of(found.payload)
        arrival = f"{source} reaches it" if source else f"{written} carries request data"
        return self.finding(
            location=ctx.location(path, found.call),
            confidence=Confidence.CERTAIN if source else Confidence.FIRM,
            message=(
                f"{found.name} {found.behaviour}, and {arrival}. A client that "
                f"controls {written} can end the command and start another one, so "
                f"this is command injection rather than a bad argument."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=ast.unparse(found.call),
                    source="the call and the payload it hands to a shell",
                ),
            ),
            properties={
                "sink": found.name,
                "payload": written,
                "source": source or "unknown",
            },
        )


def _source_of(node: ast.expr) -> str | None:
    """The request attribute somewhere inside this expression.

    :func:`request_source` resolves a plain attribute chain, so it answers for
    ``request.body`` and returns ``None`` for ``request.POST["cmd"]``, which is
    the shape actually written at a call.
    """
    for inner in ast.walk(node):
        if isinstance(inner, ast.expr):
            found = request_source(inner)
            if found is not None:
                return found
    return None
