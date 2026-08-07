"""`DJI-006` -- request data chosen as the ordering field, with no allowlist.

This is the one rule in the injection family that is not about injection, and
saying so precisely is most of the work. ``order_by`` does not concatenate its
argument into SQL. :meth:`django.db.models.sql.query.Query.add_ordering` passes
every string through ``names_to_path``, so a fragment like ``"'; DROP TABLE"``
raises ``FieldError`` and never reaches the database. Measured against Django,
not remembered:

    order_by("nickname")                 -> ORDER BY "profile"."nickname" ASC
    order_by("-nickname")                -> ORDER BY "profile"."nickname" DESC
    order_by("account__password_hash")   -> ORDER BY "account"."password_hash" ASC
    order_by("'; DROP TABLE account--")  -> FieldError

The third line is the defect. ``names_to_path`` follows ``__`` across relations,
so a client who chooses the ordering field can sort by *any* column on the model
or on anything it joins to -- including columns the view never selects. Sorting
is a comparison oracle: with a page of results and a column to sort on, an
attacker learns the relative order of values they cannot read, and by paging
through a table can recover a hidden column's ordering one boundary at a time.
The fourth line matters too, in a smaller way, because Django's ``FieldError``
names the model's valid fields, so an unhandled one hands over the schema.

The corpus decided the shape of the guard. Two places across the three
benchmarks let a request choose the ordering, and *both* allowlist it first --
netbox compares against an ``ORDERING_CHOICES`` dict and reassigns a default,
healthchecks tests ``request.GET.get("sort") in VALID_SORT_VALUES``. A rule that
reported every tainted ``order_by`` argument would call both of them defects and
ship at 0% precision, which is the same trap `DJI-005` met in the class-based
view idiom. So the membership test is not a refinement here; it is the rule.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from djaudit.dataflow.taint import (
    Taint,
    taint_of,
)
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
from djaudit.rules._injection import (
    Frame,
    identity,
    names_read,
    own_calls,
    own_nodes,
    parameters_read,
    source_of,
)

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.context import ProjectContext
    from djaudit.dataflow.chains import DefUse
    from djaudit.dataflow.scopes import Scope

METHOD = "order_by"
"""The only ordering method that takes a client-supplied field name.

``earliest`` and ``latest`` also name a field, and ``distinct`` names one on
PostgreSQL, but each returns a single row or collapses the result, so neither
gives the attacker the ordered page that makes the oracle work.
"""

WORD = "order_by"

SOURCE_WORDS = ("request", "self.kwargs")
"""The taint model's own necessary condition, reused as a prefilter.

``request_source`` recognises an attribute of the name ``request`` and the
literal ``self.kwargs``, and taint does not cross a scope boundary, so a file
whose text holds neither string can never produce a tainted argument.
"""


@dataclass(frozen=True, slots=True)
class Ordering:
    """One ordering argument, and the call it was written on."""

    call: ast.Call
    argument: ast.expr
    starred: bool


def arguments(node: ast.AST) -> Iterator[Ordering]:
    """Every ordering argument of an ``order_by`` call.

    Shared by the prefilter and the rule body so the two cannot drift. A
    constant is skipped here rather than later: ``order_by("name")`` is the
    overwhelming majority of real calls -- 200 of the 528 in the corpus -- and
    none of them can be tainted.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return
    if node.func.attr != METHOD:
        return
    for argument in node.args:
        starred = isinstance(argument, ast.Starred)
        inner = argument.value if isinstance(argument, ast.Starred) else argument
        if isinstance(inner, ast.Constant):
            continue
        yield Ordering(call=node, argument=inner, starred=starred)


CONTAINERS = (ast.Dict, ast.Set, ast.List, ast.Tuple)
"""Displays whose contents the project wrote, element by element.

A name bound to one of these cannot yield a value the module does not contain,
which is what makes a lookup through it a stronger guarantee than the
membership test below -- that one does not examine what the branch then does,
while this one cannot produce an unlisted column at all.
"""


def _bound_values(name: str, scope: Scope | None) -> Iterator[ast.expr]:
    """Every value bound to ``name``, looking outward through enclosing scopes.

    The allowlist is nearly always a module constant read inside a view, so the
    walk has to leave the function to find it. It stops at the first scope that
    binds the name, because a local rebinding is what the reader sees.
    """
    while scope is not None:
        bindings = scope.own_all(name)
        if bindings:
            for binding in bindings:
                if binding.value is not None:
                    yield binding.value
            return
        scope = scope.parent


def _lookups(value: ast.expr, chains: DefUse | None) -> Iterator[ast.expr]:
    """``value`` itself, and anything a name it stands for was defined as."""
    yield value
    if isinstance(value, ast.Name) and chains is not None:
        for binding in chains.reaching(value):
            if binding.value is not None:
                yield binding.value


def from_owned_container(value: ast.expr, scope: Scope, chains: DefUse | None) -> bool:
    """Whether the ordering value came *out of* a container the project wrote.

    This is the idiom Django documentation and DRF both push towards::

        SORTABLE = {"name": "name", "newest": "-id"}
        column = SORTABLE.get(request.GET.get("sort"), "name")
        products.order_by(column)

    The caller still chooses the sort, so a rule keyed on "the request
    influences ``order_by``" reports it -- but nothing the caller sends can
    produce a column that is not in the dict, which is a stronger guarantee
    than the membership test this rule shipped with.

    It is checked by resolving the *holder* to a display, not by trusting the
    shape. ``params = request.GET`` followed by ``params.get("sort")`` is a
    ``Name.get(...)`` too, and it is not an allowlist -- ``params`` resolves to
    an attribute of ``request`` rather than to a dict the module wrote, so it
    is correctly refused.

    Only ``get`` qualifies. ``SORTABLE.setdefault(key, key)`` and
    ``SORTABLE.pop(key, key)`` read the same owned dict but can hand back the
    caller's own string, and both stay reported.

    ``SORTABLE[key]`` needs no branch here, and one written for it was measured
    to be unreachable: a subscript takes its taint from its base, the base is a
    container this module wrote, so the value never arrives tainted and this
    function is never asked. A branch that cannot run is worse than a missing
    one, because it advertises a guarantee it never provides.
    """
    for candidate in _lookups(value, chains):
        if not (
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Attribute)
            and candidate.func.attr == "get"
            and isinstance(candidate.func.value, ast.Name)
        ):
            continue
        holder = candidate.func.value.id
        if any(isinstance(v, CONTAINERS) for v in _bound_values(holder, scope)):
            return True
    return False


def allowlisted(value: ast.expr, scope: Scope, chains: DefUse | None) -> bool:
    """Whether this scope tests the ordering value for membership of a set.

    Both real corpus shapes are covered by the same question. netbox writes::

        sort = request.GET.get('sort', 'name')
        if sort not in ORDERING_CHOICES:
            sort = 'name'
        racks = racks.order_by(sort)

    and healthchecks writes ``if request.GET.get("sort") in VALID_SORT_VALUES``.
    One tests the name that reaches the call, the other tests the request read
    that name came from, so matching identity rather than syntax catches both
    without needing to know which container was consulted.

    What the branch then *does* is not examined. Modelling that would mean
    deciding whether every path out of the comparison either rejects the
    request or substitutes a default, and a rule that guessed wrong would call
    correct defensive code a vulnerability -- the failure this rule exists to
    avoid.

    A value that came out of a container the module wrote is accepted before
    any of this, by :func:`from_owned_container`, which is a stronger guarantee
    than the membership test and does not need a branch to examine.
    """
    if from_owned_container(value, scope, chains):
        return True
    names, parameters = identity(value, chains)
    if not names and not parameters:
        return False
    for node in own_nodes(scope):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            continue
        if names_read(node.left) & names or parameters_read(node.left) & parameters:
            return True
    return False


@register
class RequestDrivenOrdering(Rule):
    """`DJI-006` -- the client chooses which column the results are sorted by."""

    meta = RuleMeta(
        id="DJI-006",
        title="Ordering field chosen by request data with no allowlist",
        family=Family.DJI,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale=(
            "order_by() does not build SQL from its argument -- Django resolves the "
            "string to a field and raises FieldError if it cannot, so this is not an "
            "injection. What it does allow is any field on the model or on anything "
            "the model joins to, because the resolver follows __ across relations. "
            "Letting the client pick that field turns an ordinary listing into a "
            "comparison oracle: sorting by a column nobody is allowed to read still "
            "reveals the order of its values, and a paginated list gives that up a "
            "boundary at a time. The FieldError path leaks separately, because its "
            "message names the model's valid fields."
        ),
        remediation=(
            "Map the parameter through an allowlist before it reaches the ORM: keep "
            "a dict of accepted values to field names and fall back to a default "
            "when the lookup misses, which is what netbox does with ORDERING_CHOICES. "
            "In DRF, add OrderingFilter and set ordering_fields to the columns the "
            "endpoint should expose -- but not to '__all__', which restores the "
            "problem for every field on the model."
        ),
        references=(
            "https://docs.djangoproject.com/en/stable/ref/models/querysets/#order-by",
            "https://www.django-rest-framework.org/api-guide/filtering/#orderingfilter",
            "https://cwe.mitre.org/data/definitions/213.html",
        ),
        limitations=(
            "A membership test on the same value or the same request parameter "
            "counts as an allowlist wherever it sits in the function, and what the "
            "branch then does with the result is not examined.",
            "Taint is tracked within one function, so an ordering field that arrives "
            "through a helper's parameter or a form's cleaned_data is unknown rather "
            "than tainted and is not reported.",
            "Only order_by is examined. The earliest, latest and distinct methods "
            "also name fields, but each returns one row or collapses the result, so "
            "none of them hands back the ordered page the oracle depends on.",
            "A DRF view that sets ordering_fields to '__all__' re-opens every field "
            "on the model without naming a request source, and is not reported here "
            "because no request data appears in the order_by call itself.",
        ),
    )

    def admits(self, tree: ast.AST) -> bool:
        """Whether this file holds a call worth building a scope tree for."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for _ in arguments(node):
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

        Nothing is analysed until a syntactic match is found, because def-use
        chains and the queryset tracker cost far more than the test that decides
        whether either is wanted.
        """
        here = [found for node in own_calls(scope) for found in arguments(node)]
        if here:
            chains = ctx.def_use(scope)
            querysets = Frame(ctx=ctx, path=path, scope=scope, chains=chains).queryset_calls
            for found in here:
                if id(found.call) not in querysets:
                    continue
                if taint_of(found.argument, chains) is not Taint.TAINTED:
                    continue
                if allowlisted(found.argument, scope, chains):
                    continue
                yield self.report(ctx, path, found, chains)
        for child in scope.children:
            yield from self.inspect(ctx, path, child)

    def report(
        self, ctx: ProjectContext, path: Path, found: Ordering, chains: DefUse | None
    ) -> Finding:
        written = ast.unparse(found.argument)
        source = source_of(found.argument)
        named = f"{source} reaches it" if source else f"{written} carries request data"
        spread = "expanded into the ordering fields" if found.starred else "the ordering field"
        return self.finding(
            location=ctx.location(path, found.call),
            confidence=Confidence.CERTAIN if source else Confidence.FIRM,
            message=(
                f"The client chooses {spread} here: {named}. Django resolves the "
                f"name to a field, so this is not SQL injection -- but it follows "
                f"__ across relations, so the ordering can be taken over any column "
                f"the model joins to, and sorting a page by a column nobody may read "
                f"still reveals its order. Map {written} through an allowlist first."
            ),
            evidence=(
                Evidence(
                    kind=EvidenceKind.AST,
                    content=ast.unparse(found.call),
                    source="the ordering argument",
                ),
            ),
            properties={
                "argument": written,
                "source": source or "unknown",
                "starred": str(found.starred).lower(),
            },
        )
