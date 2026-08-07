"""`DJI-005` -- request data expanded into a queryset call's keyword arguments.

``Book.objects.filter(**request.GET)`` hands the client the *shape* of the
query rather than a value in it. Django reads each keyword as a field lookup,
so a query string of ``?password__startswith=a`` is a legal, silent oracle:
repeat it and the whole column falls out one character at a time. Nothing is
misquoted and no SQL is malformed, which is why this survives review that
would catch an f-string.

The same expansion into ``create()`` or ``update()`` is the other half of the
bug: those keywords are field *values*, so the client chooses which columns to
write, and ``?is_staff=1`` is the canonical result.

**What the surface actually looks like was worth measuring first.** Across the
three benchmark corpora 2,092 calls expand a mapping with ``**``, 437 of them
onto a method with one of these names. Twelve of those reach a request source
directly -- and *all twelve* are
``self.get(self.request, *self.args, **self.kwargs)``, the standard
class-based-view idiom for re-rendering a form after a failed POST. ``self.get``
is a view method; the name it shares with ``QuerySet.get`` is a coincidence. A
rule keyed on the method name would therefore have shipped at **0% precision**,
every finding a false positive.

So the receiver is tested with the queryset tracker, exactly as `DJI-002` and
`DJI-003` test theirs, which cuts 437 calls to 55 and excludes all twelve
structurally rather than by a name blocklist. All 55 expand a value this
analysis reads as ``unknown``, so the rule is silent on the corpus.

**Which methods take lookups was read from the signatures, not remembered.**
``order_by(*field_names)`` and ``values_list(*fields, flat=False, named=False)``
accept no ``**kwargs`` at all, and an earlier draft of this rule listed both.
``annotate``, ``aggregate``, ``alias`` and ``values`` do take them, but their
values must be query expressions: a string from ``request.GET`` raises
``TypeError`` there rather than injecting anything, so they are a crash and not
a vulnerability, and they are left out deliberately.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
from djaudit.rules._injection import Frame, own_calls

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.dataflow.scopes import Scope

LOOKUPS = frozenset(
    {
        "filter",
        "exclude",
        "get",
        "get_or_create",
        "update_or_create",
    }
)
"""Methods whose keywords Django parses as field lookups.

Each is ``(*args, **kwargs)`` or ``(defaults=None, **kwargs)`` in Django
6.0.7's ``QuerySet``. A keyword here names a field *and* an operator --
``password__startswith`` is one keyword -- which is what makes an expanded
mapping so much more than a value.
"""

WRITES = frozenset({"create", "update"})
"""Methods whose keywords are field values rather than lookups.

``QuerySet.create(**kwargs)`` and ``QuerySet.update(**kwargs)``. The defect is
mass assignment rather than lookup injection, so the message says so.
"""

METHODS = LOOKUPS | WRITES

WORD = "**"
"""File-level prefilter.

Admits 608 of 3,091 files, against 117 that hold a real call, so the AST stage
below does most of the work. It is still worth having: the 2,483 files it
rejects are never parsed for candidates at all.
"""

SOURCE_WORDS = ("request", "self.kwargs")
"""The second half of the prefilter, and a consequence of the taint model.

:func:`~djaudit.dataflow.taint.request_source` recognises exactly two things:
an attribute of a name in ``REQUEST_NAMES``, which is the single name
``request``, and the literal ``self.kwargs``. Taint propagates only through
def-use chains, which do not leave the scope, so a mapping cannot be judged
tainted unless one of these two strings appears in the file's own text. This
is a necessary condition read off the taint model rather than a heuristic --
and it halves the work, from 608 files walked to 264 and from 117 scope trees
to 68.
"""


@dataclass(frozen=True, slots=True)
class Expansion:
    """One ``**`` argument handed to a queryset method."""

    call: ast.Call
    method: str
    mapping: ast.expr


def expansions(node: ast.AST) -> Iterator[Expansion]:
    """Every ``**`` argument of a call to one of these methods.

    Shared by the prefilter and the rule body so the two cannot drift.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return
    if node.func.attr not in METHODS:
        return
    for keyword in node.keywords:
        if keyword.arg is None:
            yield Expansion(call=node, method=node.func.attr, mapping=keyword.value)


@register
class KwargExpansion(Rule):
    """`DJI-005` -- request data expanded into a queryset call's keywords."""

    meta = RuleMeta(
        id="DJI-005",
        title="Request data expanded into queryset keyword arguments",
        family=Family.DJI,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "Expanding a request mapping with ** lets the client choose the keywords, "
            "and in the ORM a keyword is not a value but a field name joined to a "
            "lookup operator. filter(**request.GET) answers ?password__startswith=a, "
            "which turns any query into an oracle that reveals a column one character "
            "at a time, and ?owner__isnull=1 quietly removes the scoping the view "
            "relied on. On create() and update() the same expansion is mass "
            "assignment: the client picks which columns are written, and ?is_staff=1 "
            "is the usual result. Severity is high rather than critical because the "
            "attacker is confined to the ORM's own grammar -- this is arbitrary field "
            "and lookup selection, not arbitrary SQL."
        ),
        remediation=(
            "Never expand a request mapping into an ORM call. Read the parameters "
            "you support by name, or build the lookup dictionary from an explicit "
            "allowlist: {k: v for k, v in request.GET.items() if k in ALLOWED}. For "
            "anything with more than a few filters, a DRF FilterSet or a Django Form "
            "declares the accepted fields in one place and validates their types as "
            "well, which is the fix that keeps working as the model grows."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/topics/db/queries/#field-lookups",
            "https://owasp.org/www-community/attacks/Mass_Assignment",
            "https://cwe.mitre.org/data/definitions/915.html",
        ),
        limitations=(
            "Taint is tracked within one function. A mapping that reaches the call "
            "through a helper's parameter is reported as unknown rather than tainted, "
            "so an expansion assembled across two functions is not seen.",
            "The receiver must be an expression the model graph recognises as a "
            "queryset. This is what excludes the class-based-view idiom "
            "self.get(*args, **self.kwargs), but it also skips a manager reached "
            "through an unresolved import.",
            "Q(**request.GET) builds the same lookup injection and is not reported, "
            "because Q is a bare callable rather than a queryset method. The corpus "
            "holds 69 such calls and every one expands a value read as unknown.",
            "The annotate, aggregate, alias and values methods take keyword arguments too, "
            "but theirs must be query expressions rather than strings, so an expanded "
            "request mapping raises TypeError there instead of injecting anything.",
        ),
    )

    def admits(self, tree: ast.AST) -> bool:
        """Whether this file holds a call worth building a scope tree for.

        The ``isinstance`` guard is a fast path, not a second opinion:
        :func:`expansions` declines a non-call itself, and asking it directly
        would allocate a generator for all eight hundred thousand nodes in the
        files this reaches rather than the seven percent of them that are calls.
        The test that decides remains the one the rule body uses.
        """
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for _ in expansions(node):
                return True
        return False

    def check(self, ctx: ProjectContext) -> Iterator[Finding]:
        for path in ctx.python_files:
            source = ctx.source(path)
            if source is None or WORD not in source:
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

        The three analyses this needs -- def-use chains, the queryset tracker,
        and the spine walk over them -- are each far dearer than the syntactic
        test that decides whether any of them are wanted. Most scopes in an
        admitted file hold no expansion at all, so nothing is computed until one
        is found: on pretix that is the difference between a rule costing about
        a quarter of the whole run and one costing almost nothing.
        """
        here = [found for node in own_calls(scope) for found in expansions(node)]
        if here:
            chains = ctx.def_use(scope)
            querysets = Frame(ctx=ctx, path=path, scope=scope, chains=chains).queryset_calls
            for found in here:
                if id(found.call) not in querysets:
                    continue
                if taint_of(found.mapping, chains) is not Taint.TAINTED:
                    continue
                yield self.report(ctx, path, found)
        for child in scope.children:
            yield from self.inspect(ctx, path, child)

    def report(self, ctx: ProjectContext, path: Path, found: Expansion) -> Finding:
        mapping = ast.unparse(found.mapping)
        source = request_source(found.mapping)
        return self.finding(
            location=ctx.location(path, found.call),
            confidence=Confidence.CERTAIN if source else Confidence.FIRM,
            message=(
                f"{found.method}() is called with keyword arguments expanded from "
                f"{mapping}, so the client chooses "
                + (
                    "which fields are written."
                    if found.method in WRITES
                    else "which field is queried and with which lookup."
                )
                + " Read the parameters you support by name instead."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=ast.unparse(found.call),
                    source=f"{ctx.rel(path)}:{found.call.lineno}",
                ),
            ),
            properties={
                "method": found.method,
                "kind": "mass assignment" if found.method in WRITES else "lookup injection",
                "mapping": mapping,
                "request_source": source or "",
            },
        )
