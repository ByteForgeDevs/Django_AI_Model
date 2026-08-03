"""Which rows an endpoint can reach, and whether the request narrows them.

This is the groundwork for the IDOR question, and the reason it needs its own
pass is that "scoped to the requesting user" is not a phrase either benchmark
writes. NetBox scopes with ``self.queryset.restrict(request.user, action)`` in
a base viewset's ``initial()`` -- not in ``get_queryset``, and not with
``filter`` -- so a rule reading ``get_queryset`` for ``request.user`` would
find nothing on any of its 151 views and report every one of them. pretix
scopes with ``filter(order__event__organizer=self.request.organizer)``, where
``organizer`` is an attribute middleware attached to the request and the word
``user`` never appears.

So the test is not "does it mention ``request.user``". It is "does anything
narrowing this queryset derive from the request at all", asked of the whole
ancestry rather than one method. That is weaker than knowing the scoping is
*correct*, and deliberately so: this pass reports whether the request was
consulted, and declines to guess whether it was consulted properly.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from djaudit.api.views import ViewNode
    from djaudit.graph.inheritance import ClassIndex, ClassRecord

REQUEST_ROOTS = frozenset({"request", "kwargs", "args"})
"""Names that, read off ``self`` or taken as a parameter, carry caller input.

``self.kwargs`` is in here because a URL captures into it, so
``filter(pk=self.kwargs['pk'])`` is narrowing by something the caller chose --
which is exactly the shape that makes an object reachable by guessing its id.
"""

Providers = dict[str, "ast.FunctionDef | ast.AsyncFunctionDef"]
"""Methods and properties reachable as ``self.<name>``, nearest declaration
first. A ``@cached_property`` is indistinguishable from a method here and does
not need to be told apart -- both are read through ``self`` and both answer
with their return expressions."""

NARROWING_CALLS = frozenset(
    {
        "filter",
        "exclude",
        "restrict",
        "for_user",
        "visible_to",
        "get",
        "none",
        "distinct",
        "intersection",
        "difference",
    }
)
"""Queryset methods that can remove rows.

``restrict`` is NetBox's, ``for_user`` and ``visible_to`` are the two names
this pattern is given often enough to be worth knowing by sight. They earn
nothing on their own -- what matters is whether their arguments come from the
request -- but they separate a narrowing call from ``select_related``, which
changes how rows are fetched and not which ones.
"""

WIDENING_CALLS = frozenset({"all", "objects"})


class Source(StrEnum):
    """Where the queryset came from."""

    ATTRIBUTE = "attribute"
    """A ``queryset = ...`` class attribute."""

    METHOD = "method"
    """A ``get_queryset`` override."""

    ABSENT = "absent"
    """Neither -- for a plain ``APIView``, which has no queryset by design."""


@dataclass(frozen=True, slots=True)
class Return:
    """One queryset a ``get_queryset`` can return."""

    expression: str
    model_ref: str | None
    calls: tuple[str, ...]
    scoping: tuple[str, ...]
    guarded: bool
    """Reached only through a condition that reads the request.

    pretix's ``OrganizerViewSet`` returns ``Organizer.objects.all()`` -- every
    row -- but only inside ``if self.request.user.has_active_staff_session(...)``.
    The expression is unscoped and the path is not, and calling that an
    unscoped queryset would be wrong.
    """

    lineno: int

    delegates: bool = False
    """Built from ``super().get_queryset()``, so the parent's scoping applies
    and this method only refines it."""

    @property
    def scoped(self) -> bool:
        """Whether this return can be trusted not to hand over other people's rows.

        ``none()`` counts. The pair

            if self.request.user.is_staff:
                return Model.objects.all()
            return Model.objects.none()

        is one of the most common ways a Django project spells a staff-only
        list, and the second return leaks nothing by construction -- reading it
        as an unscoped queryset makes the safest branch in the file the reason
        the endpoint is reported.
        """
        return bool(self.scoping) or self.guarded or "none" in self.calls


@dataclass(frozen=True, slots=True)
class QuerysetNode:
    """What one view can read, and what narrows it."""

    source: Source
    declared_by: str | None = None
    model_ref: str | None = None
    model: str | None = None
    """Resolved to a model graph label, when the reference named one of ours."""

    expression: str | None = None
    calls: tuple[str, ...] = ()
    returns: tuple[Return, ...] = ()

    scoping: tuple[str, ...] = ()
    """Request-derived expressions that narrow this queryset, wherever in the
    ancestry they were written."""

    rebound_by: tuple[str, ...] = ()
    """Methods that assign ``self.queryset`` from a request-derived value.

    NetBox's entire object-permission scheme is one of these. Without reading
    them its every viewset looks like an unfiltered ``Model.objects.all()``.
    """

    object_scoping: tuple[str, ...] = ()
    """Request-derived expressions in a ``get_object`` override.

    Kept apart from :attr:`scoping` because it answers a different question.
    NetBox's ``DashboardView`` declares ``queryset = Dashboard.objects.all()``
    -- every dashboard in the install -- and overrides ``get_object`` to return
    ``Dashboard.objects.filter(user=self.request.user).first()``. That fully
    protects a detail-only view and would do nothing at all for a list route,
    so folding it into the queryset's own scoping would be wrong on any
    viewset that can list.
    """

    unread: bool = False
    """A ``get_queryset`` we could not reduce to returns -- it builds the
    queryset through local variables or a helper. The view has a queryset;
    we cannot say what narrows it."""

    lineno: int = 0

    @property
    def scoped(self) -> bool:
        """Whether every path through this queryset consults the request."""
        if self.unread:
            return False
        if self.rebound_by or self.scoping:
            return True
        return bool(self.returns) and all(r.scoped for r in self.returns)

    @property
    def object_scoped(self) -> bool:
        return bool(self.object_scoping)

    @property
    def empty(self) -> bool:
        """Provably no rows.

        ``queryset = Event.objects.none()`` is the strongest narrowing there
        is, and pretix writes it on views that exist only to accept a POST.
        Reading it as an unscoped queryset would report the safest shape in the
        project.
        """
        if self.source is Source.ATTRIBUTE:
            return "none" in self.calls
        return bool(self.returns) and all("none" in r.calls for r in self.returns)

    @property
    def certain(self) -> bool:
        return not self.unread

    @property
    def unfiltered(self) -> bool:
        """Every row of the model, with nothing derived from the request.

        The precondition for an IDOR finding, not the finding itself -- an
        endpoint may be deliberately public, and a permission class may be
        doing the work instead.
        """
        return (
            self.source is not Source.ABSENT and self.certain and not self.scoped and not self.empty
        )


def read_queryset(
    view: ViewNode,
    index: ClassIndex,
    by_class: dict[tuple[Path, str], str] | None = None,
) -> QuerysetNode:
    """Resolve a view's queryset across its whole ancestry.

    Nearest declaration wins for the queryset itself, but scoping is collected
    from everywhere -- a base class that narrows in ``initial()`` protects a
    subclass that never mentions it, and attributing that to the subclass is
    the only way the subclass reads correctly.
    """
    record = index.lookup(view.label)
    chain = (record, *index.ancestry(record)) if record is not None else ()

    rebound: list[str] = []
    scoping: list[str] = []
    for link in chain:
        for method, expression in _queryset_rebinds(link.node):
            found = request_refs(expression)
            if found:
                rebound.append(f"{link.dotted}.{method}")
                scoping.extend(found)

    node = _declared(view, chain)
    node = _replace_scoping(node, tuple(rebound), tuple(dict.fromkeys(scoping)))
    node = replace(node, object_scoping=_object_scoping(chain))
    return _with_model(node, index, view.module, by_class or {})


def _object_scoping(chain: tuple[ClassRecord, ...]) -> tuple[str, ...]:
    """Request references in the nearest ``get_object`` override.

    DRF's own ``get_object`` filters the queryset by ``lookup_field`` and calls
    ``check_object_permissions``; an override replaces both, so whether it
    consults the request is the only thing that says a detail route is scoped.
    """
    providers = _providers(chain)
    for link in chain:
        method = _method(link.node, "get_object")
        if method is None:
            continue
        found: set[str] = set()
        for candidate in _returns(method, providers):
            found |= set(candidate.scoping)
        return tuple(sorted(found))
    return ()


def _declared(view: ViewNode, chain: tuple[ClassRecord, ...], start: int = 0) -> QuerysetNode:
    """The nearest ``get_queryset`` or ``queryset`` in the chain.

    A ``get_queryset`` override beats an attribute even when the attribute is
    nearer, because DRF only ever calls the method -- ``GenericAPIView`` reads
    ``self.queryset`` from inside its own ``get_queryset``, so an override
    replaces the attribute entirely rather than refining it.

    ``start`` resumes the search past a link already read, which is how a
    method built from ``super().get_queryset()`` reaches the declaration it
    defers to.
    """
    providers = _providers(chain)
    for index, link in enumerate(chain[start:], start=start):
        method = _method(link.node, "get_queryset")
        if method is not None:
            returns = tuple(_returns(method, providers))
            node = QuerysetNode(
                source=Source.METHOD,
                declared_by=f"{link.dotted}.get_queryset",
                model_ref=_single(r.model_ref for r in returns),
                calls=_single_calls(returns),
                returns=returns,
                unread=not returns,
                lineno=method.lineno,
            )
            if returns and all(r.delegates for r in returns):
                return _inherit(node, _declared(view, chain, index + 1))
            return node
        assigned = _assigned(link.node, "queryset")
        if assigned is not None:
            return QuerysetNode(
                source=Source.ATTRIBUTE,
                declared_by=link.dotted,
                model_ref=_root_name(assigned),
                expression=ast.unparse(assigned),
                calls=call_chain(assigned),
                scoping=request_refs(assigned),
                lineno=assigned.lineno,
            )
    if start == 0 and view.queryset_ref is not None:
        return QuerysetNode(
            source=Source.ATTRIBUTE,
            declared_by=view.label,
            model_ref=view.queryset_model_ref,
            expression=view.queryset_ref,
            lineno=view.lineno,
        )
    return QuerysetNode(source=Source.ABSENT)


def _inherit(node: QuerysetNode, parent: QuerysetNode) -> QuerysetNode:
    """Carry a parent declaration's answers into a method that only refines it.

    ``return super().get_queryset().order_by('-created')`` is the ordinary way
    to add one clause to what a base class already decided, and read on its own
    it is a queryset with no model and no request in it -- which is to say, an
    unscoped list of nothing. Both halves of that are wrong, and the second is
    wrong in the direction that reports a subclass of a correctly scoped
    viewset.

    Nothing is invented: when the parent is not scoped either, the delegating
    return stays unscoped and is reported, which is the case where the
    subclass really did inherit the defect.
    """
    if parent.source is Source.ABSENT:
        return node
    inherited = (
        tuple(dict.fromkeys((*parent.scoping, *(s for r in parent.returns for s in r.scoping))))
        if parent.scoped
        else ()
    )
    return replace(
        node,
        model_ref=node.model_ref or parent.model_ref,
        unread=node.unread or parent.unread,
        returns=tuple(
            replace(
                candidate,
                scoping=candidate.scoping or inherited,
                # A parent scoped only by the condition its return sits under
                # has no expression to hand down, and the child is protected
                # by it just the same.
                guarded=candidate.guarded or (parent.scoped and not inherited),
            )
            for candidate in node.returns
        ),
    )


def _replace_scoping(
    node: QuerysetNode, rebound: tuple[str, ...], scoping: tuple[str, ...]
) -> QuerysetNode:
    if not rebound and not scoping:
        return node
    return replace(
        node,
        rebound_by=rebound,
        scoping=tuple(dict.fromkeys((*node.scoping, *scoping))),
    )


def _with_model(
    node: QuerysetNode,
    index: ClassIndex,
    module: str,
    by_class: dict[tuple[Path, str], str],
) -> QuerysetNode:
    """Resolve the queryset's root name to a model graph label.

    By definition site rather than by name, for the reason the serializer pass
    resolves ``Meta.model`` the same way: two apps routinely define classes
    with the same name, and matching on the name alone picks whichever was
    indexed last.
    """
    if node.model_ref is None or not by_class:
        return node
    record = index.lookup(index.resolve_name(module, node.model_ref))
    if record is None:
        return node
    return replace(node, model=by_class.get((record.path, record.name)))


def request_refs(node: ast.expr | None) -> tuple[str, ...]:
    """Sub-expressions that read the request, rendered for evidence.

    Both ``self.request.organizer`` and a bare ``request.user`` count: the
    first is how a viewset reaches it and the second how a method handed the
    request as a parameter does. ``self.kwargs['pk']`` counts too, since a URL
    capture is caller input by another route.
    """
    if node is None:
        return ()
    found = [
        ast.unparse(child)
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute | ast.Subscript) and _reads_request(child)
    ]
    return tuple(dict.fromkeys(_longest(found)))


def _reads_request(node: ast.expr) -> bool:
    path = _root_path(node)
    if path is None or not path:
        return False
    if path[0] in REQUEST_ROOTS:
        return True
    return path[0] == "self" and len(path) > 1 and path[1] in REQUEST_ROOTS


def _root_path(node: ast.expr) -> list[str] | None:
    """``self.request.user`` as ``['self', 'request', 'user']``.

    ``None`` when the chain does not bottom out in a plain name -- a call or a
    literal in the middle means the expression is not a simple attribute
    lookup and there is nothing to match against.
    """
    parts: list[str] = []
    current: ast.expr = node
    while True:
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        elif isinstance(current, ast.Subscript):
            current = current.value
        elif isinstance(current, ast.Call):
            # Only the receiver, never the arguments:
            # ``self.item.variations.all().prefetch_related(...)`` is rooted at
            # ``self.item``, while ``lookup(request.user).all()`` is not rooted
            # at the request and must not be read as though it were.
            current = current.func
        elif isinstance(current, ast.Name):
            parts.append(current.id)
            return list(reversed(parts))
        else:
            return None


def _longest(found: list[str]) -> list[str]:
    """Drop references that are a prefix of another.

    ``self.request.user.pk`` walks to ``self.request`` and ``self.request.user``
    as well, and three overlapping strings make one piece of evidence look
    like three.
    """
    return [
        ref for ref in found if not any(other != ref and other.startswith(ref) for other in found)
    ]


def call_chain(node: ast.expr) -> tuple[str, ...]:
    """Queryset methods applied, in the order written.

    ``Circuit.objects.filter(x=1).select_related('y')`` answers
    ``('objects', 'filter', 'select_related')``.
    """
    names: list[str] = []
    current: ast.expr = node
    while True:
        if isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Attribute):
            names.append(current.attr)
            current = current.value
        else:
            break
    return tuple(reversed(names))


def _returns(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    providers: Providers | None = None,
    seen: frozenset[str] = frozenset(),
) -> list[Return]:
    """Every queryset a method can return, with the conditions guarding it.

    Local variables are followed, and that is the whole difficulty. Almost no
    real ``get_queryset`` returns the expression that scoped it -- pretix
    writes ``qs = self.request.event.checkin_lists...`` and then returns ``qs``
    four statements later -- so reading only the return expression finds a bare
    name and concludes the endpoint reads every row. That mistake called 31 of
    pretix's 57 querysets unscoped.
    """
    assignments = _local_assignments(func)
    context = _Context(assignments, providers or {})
    out: list[Return] = []

    def walk(body: list[ast.stmt], *, guarded: bool) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                refs, root, delegates = _expand(stmt.value, context, seen)
                out.append(
                    Return(
                        expression=ast.unparse(stmt.value),
                        model_ref=root,
                        calls=call_chain(stmt.value),
                        scoping=tuple(sorted(refs)),
                        guarded=guarded,
                        delegates=delegates,
                        lineno=stmt.lineno,
                    )
                )
            elif isinstance(stmt, ast.If):
                inner = guarded or bool(request_refs(stmt.test))
                walk(stmt.body, guarded=inner)
                walk(stmt.orelse, guarded=guarded)
            elif isinstance(stmt, ast.For | ast.While | ast.With | ast.Try):
                walk(_nested(stmt), guarded=guarded)

    walk(func.body, guarded=False)
    return out


def _local_assignments(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, list[ast.expr]]:
    """Every value bound to a plain local name in this function.

    All of them, not the last: ``qs`` is typically assigned once from a scoped
    source and then reassigned several times to refine itself, and the scoping
    lives in the first of those.
    """
    found: dict[str, list[ast.expr]] = {}
    for stmt in ast.walk(func):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(stmt.value)
        elif isinstance(stmt, ast.AugAssign | ast.NamedExpr) and isinstance(stmt.target, ast.Name):
            found.setdefault(stmt.target.id, []).append(stmt.value)
    return found


def _expand(
    expr: ast.expr,
    context: _Context,
    seen: frozenset[str],
) -> tuple[set[str], str | None, bool]:
    """Follow local names and ``self`` attributes to what they were built from.

    Answers the request references reachable from an expression, the model it
    starts at, and whether it defers to ``super().get_queryset()``.

    Two indirections, both taken from pretix because both are ordinary Django.
    A local name, since ``qs = ...`` then ``return qs`` is how everyone writes
    this. And a ``self`` attribute, since ``ItemVariationViewSet`` resolves
    ``self.item`` in a ``@cached_property`` off ``self.kwargs['item']`` and
    returns ``self.item.variations.all()`` -- scoped to a URL capture, through
    a method, with nothing in the return expression to show it.

    Names already visited are not revisited: ``qs = qs.filter(...)`` is the
    normal way to refine a queryset and would otherwise not terminate.
    """
    refs = set(request_refs(expr))
    delegates = any(
        _is_super_queryset(child) for child in ast.walk(expr) if isinstance(child, ast.expr)
    )
    root = _root_name(expr)

    if root is not None and root in context.locals and root not in seen:
        inner = seen | {root}
        origin: str | None = None
        for assigned in context.locals[root]:
            more, deeper, delegated = _expand(assigned, context, inner)
            refs |= more
            delegates = delegates or delegated
            if origin is None and deeper is not None:
                origin = deeper
        root = origin

    path = _root_path(expr)
    if path and path[0] == "self" and len(path) > 1:
        attribute = path[1]
        provider = context.providers.get(attribute)
        if provider is not None and attribute not in seen:
            for nested in _returns(provider, context.providers, seen | {attribute}):
                refs |= set(nested.scoping)

    # ``self`` is where ``self.request.event.checkin_lists`` bottoms out. It
    # names no model, and reporting it as one would put "self" in a finding.
    return refs, (None if root in {"self", "super"} else root), delegates


@dataclass(frozen=True, slots=True)
class _Context:
    """What a return expression can be resolved against."""

    locals: dict[str, list[ast.expr]]
    providers: Providers


def _is_super_queryset(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_queryset"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
    )


def _nested(stmt: ast.stmt) -> list[ast.stmt]:
    body: list[ast.stmt] = list(getattr(stmt, "body", []))
    body.extend(getattr(stmt, "orelse", []))
    body.extend(getattr(stmt, "finalbody", []))
    for handler in getattr(stmt, "handlers", []):
        body.extend(handler.body)
    return body


def _queryset_rebinds(node: ast.ClassDef) -> list[tuple[str, ast.expr]]:
    """``self.queryset = <expr>`` anywhere in the class, with its method."""
    found: list[tuple[str, ast.expr]] = []
    for stmt in node.body:
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(stmt):
            if not isinstance(inner, ast.Assign):
                continue
            for target in inner.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "queryset"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    found.append((stmt.name, inner.value))
    return found


def _providers(chain: tuple[ClassRecord, ...]) -> Providers:
    """Every method in the ancestry, reachable as ``self.<name>``.

    Nearest declaration wins, matching Python: an override in the view shadows
    the base's, and reading the base's would answer about code that never runs.
    """
    found: Providers = {}
    for link in chain:
        for stmt in link.node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                found.setdefault(stmt.name, stmt)
    return found


def _method(node: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for stmt in node.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and stmt.name == name:
            return stmt
    return None


def _assigned(node: ast.ClassDef, name: str) -> ast.expr | None:
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == name
            and stmt.value
        ):
            return stmt.value
    return None


def _root_name(node: ast.expr) -> str | None:
    current: ast.expr = node
    while True:
        if isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Attribute):
            current = current.value
        elif isinstance(current, ast.Name):
            return current.id
        else:
            return None


def _single(values: Iterable[str | None]) -> str | None:
    """The one model every return agrees on, or ``None`` if they disagree."""
    seen = {value for value in values if value is not None}
    return next(iter(seen)) if len(seen) == 1 else None


def _single_calls(returns: tuple[Return, ...]) -> tuple[str, ...]:
    return returns[0].calls if len(returns) == 1 else ()
