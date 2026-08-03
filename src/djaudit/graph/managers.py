"""Managers, and what `Model.objects` actually is.

Every ORM question starts at a manager, and on a real project it is rarely
Django's. NetBox attaches `RestrictedQuerySet.as_manager()` to most of its
models, and that queryset's `restrict()` is how permission scoping is applied
— so an authorization rule that assumes `objects` is a plain `Manager` reports
every one of those viewsets as unscoped. The opposite mistake is worse: a
manager overriding `get_queryset` may be filtering rows out, and a rule that
counts what it returns as the whole table is measuring the wrong set.

So the graph records which manager is the default, what queryset it produces,
and whether either of them narrows what comes back.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from djaudit.astutils import dotted_name

if TYPE_CHECKING:
    from djaudit.graph.inheritance import ClassIndex, ClassRecord

from djaudit.graph.nodes import ManagerNode

MANAGER_BASES = frozenset(
    {
        "django.db.models.Manager",
        "django.db.models.manager.Manager",
        "django.db.models.manager.BaseManager",
        "django.db.models.BaseManager",
    }
)

QUERYSET_BASES = frozenset(
    {
        "django.db.models.QuerySet",
        "django.db.models.query.QuerySet",
    }
)

DEFAULT_MANAGER_NAME = "objects"


def extract_managers(
    node: ast.ClassDef, record: ClassRecord, index: ClassIndex
) -> dict[str, ManagerNode]:
    """Managers assigned directly in a class body, in declaration order.

    Order is Django's tie-breaker for which manager is the default, so it is
    load-bearing rather than incidental.
    """
    found: dict[str, ManagerNode] = {}
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
            continue
        for target in stmt.targets:
            if not isinstance(target, ast.Name):
                continue
            manager = _read(target.id, stmt.value, record, index)
            if manager is not None:
                found[target.id] = manager
    return found


def _read(name: str, call: ast.Call, record: ClassRecord, index: ClassIndex) -> ManagerNode | None:
    """One assignment, if it is a manager."""
    composed = _from_queryset_call(call)
    if composed is not None:
        owner, queryset = composed
        return ManagerNode(
            name=name,
            manager_class=owner.rsplit(".", 1)[-1],
            dotted=index.base_target(record, owner),
            queryset_class=queryset,
            is_plain=index.base_target(record, owner) in MANAGER_BASES,
            lineno=call.lineno,
            narrows=_narrows(index, index.base_target(record, owner))
            or _narrows(index, index.base_target(record, queryset)),
        )

    callee = dotted_name(call.func)
    if callee is None:
        return None
    tail = callee.rsplit(".", 1)[-1]

    if tail == "as_manager":
        # QuerySet.as_manager() -- NetBox's dominant form. The queryset is the
        # interesting half: it is where restrict() and any narrowing live.
        owner = callee.rsplit(".", 1)[0]
        return ManagerNode(
            name=name,
            manager_class="Manager",
            dotted=index.base_target(record, owner),
            queryset_class=owner,
            lineno=call.lineno,
            narrows=_narrows(index, index.base_target(record, owner)),
        )

    if tail == "from_queryset":
        # Manager.from_queryset(QuerySet) yields a class, so this form is only
        # a manager once called: Manager.from_queryset(QuerySet)().
        return None

    resolved = index.base_target(record, callee)
    if not _is_manager(index, resolved, tail):
        return None

    return ManagerNode(
        name=name,
        manager_class=tail,
        dotted=resolved,
        is_plain=resolved in MANAGER_BASES,
        lineno=call.lineno,
        narrows=_narrows(index, resolved),
    )


def _from_queryset_call(call: ast.Call) -> tuple[str, str] | None:
    """``Manager.from_queryset(QuerySet)()`` -- the manager class and queryset.

    Written as a call of a call, so the outer callee is not a name at all.
    Reading it as one gives up before reaching the queryset, which is the
    entire point of the expression.
    """
    func = call.func
    if not isinstance(func, ast.Call):
        return None
    inner = dotted_name(func.func)
    if inner is None or "." not in inner:
        return None
    owner, _, tail = inner.rpartition(".")
    if tail != "from_queryset":
        return None
    for arg in func.args:
        named = dotted_name(arg)
        if named is not None:
            return owner, named
    return None


def _is_manager(index: ClassIndex, dotted: str, tail: str) -> bool:
    """Whether a call names a manager class.

    Resolved through the class index where the definition is in the project,
    because ``objects = {s.name: s for s in ...}`` sits in a NetBox model body
    and a name-based guess has no way to decline it. The suffix convention is
    the fallback for third-party managers we cannot see -- django-mptt's
    ``TreeManager`` is three NetBox models' default and is not in the checkout.
    """
    if dotted in MANAGER_BASES:
        return True
    record = index.lookup(dotted)
    if record is not None and index.inherits(record, MANAGER_BASES):
        return True
    # The suffix convention, which is the only thing left for a manager whose
    # definition we cannot see -- django-mptt's TreeManager is three NetBox
    # models' default and is not in the checkout -- and also the fallback for
    # one whose own bases we could not read.
    return tail.endswith("Manager")


def _narrows(index: ClassIndex, dotted: str | None) -> bool:
    """Whether this class or an ancestor overrides ``get_queryset``.

    The signal that a manager returns less than the table. A soft-delete
    manager hiding deleted rows means a rule counting what it returns is
    measuring the wrong set, and a manager that scopes by user is the whole
    answer to an authorization question rather than a false positive.
    """
    if dotted is None:
        return False
    record = index.lookup(dotted)
    if record is None:
        return False
    if _defines_get_queryset(record.node):
        return True
    return any(_defines_get_queryset(a.node) for a in index.ancestry(record))


def _defines_get_queryset(node: ast.ClassDef) -> bool:
    return any(
        isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "get_queryset"
        for stmt in node.body
    )


def implicit_manager(lineno: int) -> ManagerNode:
    """The ``objects`` Django adds when a model declares no manager at all.

    ``ModelBase._prepare`` creates it only when nothing was declared anywhere
    in the MRO, so it must be added after inheritance rather than during it --
    a model whose abstract base declares ``objects`` does not get a second one.
    """
    return ManagerNode(
        name=DEFAULT_MANAGER_NAME,
        manager_class="Manager",
        dotted="django.db.models.Manager",
        is_plain=True,
        implicit=True,
        lineno=lineno,
    )
