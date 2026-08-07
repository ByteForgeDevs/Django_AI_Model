"""`DJI-007` -- request data handed to a sink that can execute what it decodes.

The sinks divide into three kinds, and only the first is simple.

**Code evaluation.** ``eval`` and ``exec`` run whatever they are given. There is
nothing to qualify.

**Object deserialisation.** ``pickle`` reconstructs objects by calling whatever
the payload names, so a pickle is a program, not data. ``marshal``, ``dill``,
``shelve`` and ``jsonpickle`` are the same bargain.

**YAML, where the received wisdom is wrong.** The rule everyone writes is "flag
bare ``yaml.load``". Measured against PyYAML 6.0.3 rather than remembered, that
rule is obsolete: ``load(stream, Loader)`` makes the loader a *required*
argument, so ``yaml.load(x)`` raises ``TypeError`` and never parses anything. On
PyYAML 5.x the omitted loader defaulted to ``FullLoader``, which refuses the
attack. A missing loader is therefore a crash or a non-event, and never the
vulnerability it is usually reported as.

What does matter is which loader is named. Each was run in its own process
against ``!!python/object/apply:os.system`` and checked for a filesystem side
effect the return value could not fake:

===============  ==============  =========================================
loader           side effect     result
===============  ==============  =========================================
``Loader``       **yes**         returned the exit status of the command
``UnsafeLoader`` **yes**         returned the exit status of the command
``CLoader``      **yes**         returned the exit status of the command
``FullLoader``   no              ``ConstructorError``
``SafeLoader``   no              ``ConstructorError``
``CSafeLoader``  no              ``ConstructorError``
``CFullLoader``  no              ``ConstructorError``
``BaseLoader``   no              returned the tag's argument as plain strings
===============  ==============  =========================================

So the loader keyword decides, and reading it is not a refinement of this rule
but a condition of shipping it: all three ``yaml.load_all`` calls in the
benchmark corpus pass ``Loader=yaml.SafeLoader``, one of them on genuine bulk
import data. Ignoring the keyword would have produced three false positives and
no true ones -- the same trap `DJI-005` met in the class-based-view idiom and
`DJI-006` met in netbox's ordering allowlist.
"""

from __future__ import annotations

import ast
import string
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

EVALUATORS = {
    "eval": "evaluates it as a Python expression",
    "exec": "executes it as Python source",
}
"""Builtins that run their argument. Matched as bare names only."""

DESERIALISERS = {
    "pickle": "reconstructs objects by calling whatever the payload names",
    "cPickle": "reconstructs objects by calling whatever the payload names",
    "dill": "reconstructs objects by calling whatever the payload names",
    "marshal": "decodes a code object the interpreter will accept",
    "jsonpickle": "reconstructs objects by calling whatever the payload names",
}
"""Modules whose load functions treat the payload as instructions.

Keyed by module rather than by full dotted path so that ``pickle.loads`` and an
aliased ``import pickle as p`` are both reached by the same last-segment match.
"""

LOAD_FUNCTIONS = frozenset({"load", "loads", "decode"})
"""The function names on those modules that perform the reconstruction."""

UNSAFE_LOADERS = frozenset({"Loader", "UnsafeLoader", "CLoader"})
"""PyYAML loaders measured to execute a ``!!python/object/apply`` payload.

``BaseLoader``, ``SafeLoader``, ``FullLoader``, ``CSafeLoader`` and
``CFullLoader`` were measured not to, so naming any of them is a real defence
and must not be reported.
"""

YAML_LOADERS = frozenset({"load", "load_all"})
"""``yaml`` functions that take a ``Loader``, and are safe or not by which one."""

YAML_UNSAFE = frozenset({"unsafe_load", "unsafe_load_all"})
"""``yaml`` functions that take no loader because they have already chosen one."""

WORDS = ("eval", "exec", "pickle", "cPickle", "dill", "marshal", "jsonpickle", "yaml")
"""A necessary condition on a file's text, read off the matcher not guessed.

Every sink is either the builtin ``eval``/``exec``, matched as a bare
:class:`ast.Name`, or an attribute whose immediate holder is one of the module
names above -- :func:`_module` reads that holder, so an aliased ``import pickle
as p`` is not matched by the rule either and costs nothing to skip.
"""

IDENTIFIER = frozenset(string.ascii_letters + string.digits + "_")
"""The characters Python allows inside a name, and so the ones a token cannot touch."""

SOURCE_WORDS = ("request", "self.kwargs")
"""The taint model's own necessary condition, reused as a prefilter.

``request_source`` recognises an attribute of the name ``request`` and the
literal ``self.kwargs``, and taint does not cross a scope boundary, so a file
holding neither string can never produce a tainted argument.
"""


def names_a_sink(source: str) -> bool:
    """Whether *source* spells one of :data:`WORDS` as a whole identifier.

    Python identifiers are whole tokens, so a token search cannot miss a real
    sink, while a plain substring search cannot tell ``exec`` from ``execute``
    or ``marshal`` from ``marshalling``. Measured across the three benchmarks,
    the boundaries take the files reaching the AST walk from 46/83/139 down to
    0/8/3 and the walk from 0.24s/0.75s/2.05s to 0.00s/0.07s/0.01s.

    ``str.find`` is used rather than ``re`` with ``\\b``: both agreed on all
    3,091 benchmark files, but the regex alternation costs 0.07s/0.57s/0.46s
    against this loop's 0.02s/0.12s/0.09s.
    """
    for word in WORDS:
        start = source.find(word)
        while start != -1:
            end = start + len(word)
            before_ok = start == 0 or source[start - 1] not in IDENTIFIER
            after_ok = end == len(source) or source[end] not in IDENTIFIER
            if before_ok and after_ok:
                return True
            start = source.find(word, start + 1)
    return False


@dataclass(frozen=True, slots=True)
class Sink:
    """One call that would run or reconstruct whatever it is given."""

    call: ast.Call
    payload: ast.expr
    name: str
    behaviour: str


def _tail(node: ast.expr) -> str | None:
    """The last segment of a dotted name, or ``None`` if it is not one."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _module(node: ast.Attribute) -> str | None:
    """The name the attribute was read from: ``pickle`` in ``pickle.loads``.

    Only the immediate holder is read, so ``a.b.pickle.loads`` resolves on
    ``pickle`` and an unrelated ``self.loads`` resolves on nothing.
    """
    return _tail(node.value)


def _loader(call: ast.Call) -> ast.expr | None:
    """The ``Loader`` argument, whether written positionally or by keyword.

    ``yaml.load``'s signature is ``load(stream, Loader)``, so the second
    positional argument is the loader.
    """
    for keyword in call.keywords:
        if keyword.arg == "Loader":
            return keyword.value
    if len(call.args) > 1:
        return call.args[1]
    return None


def sinks(node: ast.AST) -> Iterator[Sink]:
    """Every call on this node that executes or reconstructs its first argument.

    Shared by the prefilter and the rule body so the two cannot drift.
    """
    if not isinstance(node, ast.Call) or not node.args:
        return
    payload = node.args[0]
    if isinstance(payload, ast.Starred):
        return

    if isinstance(node.func, ast.Name) and node.func.id in EVALUATORS:
        yield Sink(node, payload, node.func.id, EVALUATORS[node.func.id])
        return

    if not isinstance(node.func, ast.Attribute):
        return
    function = node.func.attr
    module = _module(node.func)
    if module is None:
        return

    if module in DESERIALISERS and function in LOAD_FUNCTIONS:
        yield Sink(node, payload, f"{module}.{function}", DESERIALISERS[module])
        return

    if module != "yaml":
        return
    if function in YAML_UNSAFE:
        yield Sink(
            node,
            payload,
            f"yaml.{function}",
            "constructs arbitrary Python objects named by the document",
        )
        return
    if function in YAML_LOADERS:
        loader = _loader(node)
        if loader is None:
            # PyYAML 6 raises TypeError without one and PyYAML 5 defaulted to
            # FullLoader, which refuses the attack. Neither is a vulnerability.
            return
        named = _tail(loader)
        if named in UNSAFE_LOADERS:
            yield Sink(
                node,
                payload,
                f"yaml.{function}",
                f"was given {named}, which constructs objects the document names",
            )


@register
class UnsafeDeserialisation(Rule):
    """`DJI-007` -- request data reaching ``eval``, ``pickle`` or an unsafe loader."""

    meta = RuleMeta(
        id="DJI-007",
        title="Request data evaluated or deserialised by an executing loader",
        family=Family.DJI,
        severity=Severity.CRITICAL,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "These sinks do not parse data, they follow instructions. A pickle names "
            "the callables to invoke while it rebuilds, eval and exec run their "
            "argument outright, and PyYAML's Loader, UnsafeLoader and CLoader "
            "construct whatever object the document asks for -- all three were "
            "measured executing os.system through a !!python/object/apply tag. When "
            "the payload arrives from a request, the attacker is not supplying data "
            "to the process, they are supplying code to it, and the result is remote "
            "code execution with the application's own privileges."
        ),
        remediation=(
            "Parse the format instead of executing it: json.loads for structured "
            "data, yaml.safe_load or an explicit Loader=yaml.SafeLoader for YAML. "
            "Where a Python value must genuinely cross a boundary, sign it -- "
            "django.core.signing carries the same data with a tamper check, which is "
            "what Django itself switched sessions to. If a request must select "
            "behaviour, map its value through a dict of permitted callables rather "
            "than evaluating it."
        ),
        references=(
            "https://docs.python.org/3/library/pickle.html#module-pickle",
            "https://pyyaml.org/wiki/PyYAMLDocumentation",
            "https://docs.djangoproject.com/en/stable/topics/signing/",
            "https://cwe.mitre.org/data/definitions/502.html",
        ),
        limitations=(
            "A payload is reported only when taint analysis proves it came from the "
            "request. Deserialising an opaque value is not reported here, because "
            "ruff and bandit already flag these calls without regard to their input.",
            "Taint is tracked within one function, so a payload that arrives through "
            "a helper's parameter, a model field or a cache read is unknown rather "
            "than tainted and is not reported.",
            "A yaml.load call whose Loader is a variable rather than a named loader "
            "is not reported, because the rule cannot show the loader is one of the "
            "three measured to execute a payload.",
            "The compile builtin is not treated as a sink. It produces a code object "
            "without running it, so the defect only exists once that object reaches "
            "an eval or exec, which is what the rule reports instead.",
        ),
    )

    def admits(self, tree: ast.AST) -> bool:
        """Whether this file holds a sink call worth building a scope tree for."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for _ in sinks(node):
                return True
        return False

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or not names_a_sink(source):
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
        here = [found for node in own_calls(scope) for found in sinks(node)]
        if here:
            chains = def_use(scope)
            for found in here:
                if taint_of(found.payload, chains) is Taint.TAINTED:
                    yield self.report(ctx, path, found, chains)
        for child in scope.children:
            yield from self.inspect(ctx, path, child)

    def report(
        self, ctx: ProjectContext, path: Path, found: Sink, chains: DefUse | None
    ) -> Finding:
        written = ast.unparse(found.payload)
        source = _source_of(found.payload)
        arrival = f"{source} reaches it" if source else f"{written} carries request data"
        return self.finding(
            location=ctx.location(path, found.call),
            confidence=Confidence.CERTAIN if source else Confidence.FIRM,
            message=(
                f"{found.name} {found.behaviour}, and {arrival}. A client that "
                f"controls {written} controls what this process runs, so this is "
                f"remote code execution rather than a parsing bug."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=ast.unparse(found.call),
                    source="the sink and its payload",
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
    ``request.body`` and returns ``None`` for ``request.POST["blob"]``, which is
    the shape actually written at a call.
    """
    for inner in ast.walk(node):
        if isinstance(inner, ast.expr):
            found = request_source(inner)
            if found is not None:
                return found
    return None
