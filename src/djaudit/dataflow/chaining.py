"""What a queryset chain actually did, in terms a rule can act on.

3.1.3 says a chain is ``("filter", "select_related")`` over ``shop.Book``. That
is not yet enough to decide anything: ``select_related`` alone is not a fact,
``select_related("author")`` is. This reads the arguments and answers the one
question the N+1 rules exist to ask -- **is this relation path already
fetched?**

**Django's semantics here are full of traps, and getting any of them wrong
produces a confident false positive.** Each of the following is implemented
because it changes the answer:

``select_related()`` with no arguments follows *every* non-null forward
relation, so nothing single-valued can be an N+1 on that queryset. Treating it
like ``select_related`` of nothing would flag every one of them.

``select_related(None)`` **clears** the list rather than adding to it. So does
``prefetch_related(None)``. A chain that sets and then clears has fetched
nothing, and a reader that only accumulates would report the opposite of the
truth.

Calls accumulate across a chain: ``.select_related("a").select_related("b")``
fetches both. Django only replaces on an explicit ``None``.

A lookup implies its prefixes. ``select_related("a__b")`` means ``obj.a`` and
``obj.a.b`` are both loaded, so a rule must not flag the shorter path.

``prefetch_related(Prefetch("books", queryset=...))`` carries the path inside an
object rather than as a string, and this shape is common in exactly the mature
code where a false positive is most expensive.

``.only()`` and ``.defer()`` are the reverse: they make attribute access
*cost* a query. A field left out of ``only()`` is loaded lazily, one query per
row -- the same defect as an N+1 and invisible to any rule that only looks at
``select_related``. This is why :attr:`ChainSpec.deferred_access` exists.

``.values()`` and ``.values_list()`` stop returning model instances at all, so
there is no attribute access to be lazy and no N+1 to report. A rule that skips
this check reports N+1s on dictionaries.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from djaudit.dataflow.querysets import INDEX, SLICE

if TYPE_CHECKING:
    from djaudit.dataflow.querysets import QuerysetValue, Step

#: Sentinel for ``select_related()`` with no arguments -- every forward relation.
ALL_FORWARD = "*"

#: Methods that make the queryset stop yielding model instances.
NON_INSTANCE = frozenset({"values", "values_list", "dates", "datetimes", "aggregate"})

#: Methods that reduce the rows fetched but change nothing about relations.
NEUTRAL = frozenset({"order_by", "reverse", "using", "select_for_update", "alias"})


def _string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _prefetch_path(node: ast.expr) -> str | None:
    """The lookup inside ``Prefetch("books", ...)``, or a plain string."""
    direct = _string(node)
    if direct is not None:
        return direct
    call = _prefetch_call(node)
    if call is None:
        return None
    if call.args:
        return _string(call.args[0])
    for keyword in call.keywords:
        if keyword.arg == "lookup":
            return _string(keyword.value)
    return None


def _prefetch_call(node: ast.expr) -> ast.Call | None:
    """``node`` if it constructs a ``Prefetch`` or ``GenericPrefetch``.

    ``GenericPrefetch`` takes the same leading ``lookup`` argument and is what
    NetBox reaches for whenever the relation is generic, so excluding it meant
    reading a real fetch as an unreadable one.

    Project-local subclasses are deliberately not matched by name. NetBox's
    ``RestrictedPrefetch(lookup, user, action='view', queryset=None)`` -- 18
    call sites -- reorders the positional arguments, so matching it would read
    ``user`` as the inner queryset. Left unmatched it reads as unreadable,
    which downgrades a finding to tentative rather than mis-stating it.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name | ast.Attribute):
        return None
    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
    return node if name in {"Prefetch", "GenericPrefetch"} else None


def _to_attr(node: ast.expr) -> tuple[bool, str | None]:
    """Whether the node redirects its rows, and the attribute name if readable.

    The two halves are separate because they answer different questions. Any
    ``to_attr`` at all means the related manager stays unpopulated, so the
    relation is not covered even when the name is computed; the name is only
    needed to recognise reads of the new attribute.
    """
    call = _prefetch_call(node)
    if call is None:
        return False, None
    for keyword in call.keywords:
        if keyword.arg == "to_attr":
            return True, _string(keyword.value)
    return False, None


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _inner_queryset(call: ast.Call) -> list[ast.expr]:
    """The queryset expressions a ``Prefetch`` narrows its lookup with.

    ``Prefetch(lookup, queryset=...)`` takes one and ``GenericPrefetch(lookup,
    querysets=[...])`` takes a list, both positionally second. The list is not
    unpacked because :func:`ast.walk` already descends into an ``ast.List``.
    """
    found: list[ast.expr] = []
    if len(call.args) > 1:
        found.append(call.args[1])
    found.extend(k.value for k in call.keywords if k.arg in {"queryset", "querysets"})
    return found


def _nested_paths(node: ast.expr) -> list[str]:
    """Paths a ``Prefetch``'s inner queryset fetches *below* its own lookup.

    ``Prefetch("interfaces", queryset=Iface.objects.select_related("site"))``
    fetches ``interfaces__site``, and reading the lookup alone would report an
    N+1 the author has already fixed. Measured in
    ``scripts/prefetch_cache_probe.py``: over three VMs with two interfaces
    each, reading ``iface.site`` costs 8 queries under a plain prefetch, 2 with
    a nested ``select_related`` and 3 with a nested ``prefetch_related``.

    The inner expression is walked syntactically rather than tracked, because
    what it is built from does not matter -- only which fetch calls appear on
    it. Redirection is not checked here: :func:`_lookups` already declines to
    call this for a ``to_attr`` prefetch, and a second check there proved
    unreachable under injection.
    """
    call = _prefetch_call(node)
    lookup = _prefetch_path(node)
    if call is None or lookup is None:
        return []
    found: list[str] = []
    for queryset in _inner_queryset(call):
        for inner in ast.walk(queryset):
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                continue
            if inner.func.attr not in {"select_related", "prefetch_related"}:
                continue
            found.extend(f"{lookup}__{path}" for arg in inner.args if (path := _prefetch_path(arg)))
    return found


@dataclass(frozen=True, slots=True)
class ChainSpec:
    """What a chain fetched, deferred, and returned."""

    select_related: frozenset[str] = frozenset()
    """Forward paths joined in. Contains :data:`ALL_FORWARD` for a bare
    ``select_related()``."""

    prefetch_related: frozenset[str] = frozenset()
    only: frozenset[str] = frozenset()
    defer: frozenset[str] = frozenset()
    annotated: frozenset[str] = frozenset()
    filters: tuple[str, ...] = ()
    """Lookup keywords passed to ``filter``/``exclude``, as written."""

    sliced: bool = False
    ordered: bool = False
    distinct: bool = False
    yields_instances: bool = True
    """``False`` after ``.values()`` and friends, where there is no model
    instance to lazily load anything."""

    unreadable: frozenset[str] = field(default_factory=frozenset)
    """Methods whose arguments we could not read -- a splat, a variable, a
    computed lookup. Recorded rather than ignored, because "we saw
    ``select_related(*paths)``" and "we saw no ``select_related``" must not
    look alike to a rule deciding whether to speak firmly."""

    to_attr: frozenset[str] = frozenset()
    """Attribute names a ``Prefetch(..., to_attr=...)`` redirected rows to.

    These are *not* in :attr:`prefetch_related`, because Django does not
    populate the ordinary related manager when ``to_attr`` is given. Measured
    by ``scripts/prefetch_cache_probe.py``: over three rows, reading
    ``obj.interfaces.all()`` under a plain prefetch costs 2 queries and under a
    ``to_attr`` prefetch costs 5. Treating the two alike would call a real N+1
    covered, which is the one mistake a performance rule cannot afford.
    """

    def covers(self, path: str) -> bool:
        """Whether traversing ``path`` on a result row is already fetched.

        A path is covered by any fetch of itself or of a longer path through
        it, since ``select_related("a__b")`` loads ``a`` on the way to ``b``.
        """
        if ALL_FORWARD in self.select_related:
            return True
        for fetched in self.select_related | self.prefetch_related:
            if fetched == path or fetched.startswith(f"{path}__"):
                return True
        return False

    def prefetches(self, path: str) -> bool:
        """Whether ``path`` -- which ends at many rows -- is already prefetched.

        Deliberately not :meth:`covers`. A bare ``select_related()`` fetches
        every forward single-valued relation and so covers any path made of
        them, but it cannot touch a many-to-many or a reverse foreign key:
        those are more rows than the row being selected, and Django will not
        join them in. Only ``prefetch_related`` reaches them, and unlike
        ``select_related`` its no-argument form *clears* the list rather than
        asking for everything, so there is no all-paths marker to honour here.
        """
        for fetched in self.prefetch_related:
            if fetched == path or fetched.startswith(f"{path}__"):
                return True
        return False

    def deferred_access(self, field_name: str) -> bool:
        """Whether reading ``field_name`` on a row costs an extra query.

        ``only()`` names what was loaded, so anything absent is deferred.
        ``defer()`` names what was not.
        """
        if self.only:
            return field_name not in self.only and field_name not in self.annotated
        return field_name in self.defer

    @property
    def fetches_anything(self) -> bool:
        return bool(self.select_related or self.prefetch_related)

    @property
    def confident(self) -> bool:
        """Every relevant argument was readable, so a rule may speak firmly."""
        return not self.unreadable


def _lookups(step: Step) -> tuple[list[str], bool]:
    """String arguments of a step, and whether any argument was unreadable.

    A ``Prefetch`` carrying ``to_attr`` is deliberately *not* returned as a
    fetched path: its rows land on a new attribute and the relation itself
    stays unfetched, so nothing below the relation is reached either and its
    inner queryset is not read.
    """
    found: list[str] = []
    unreadable = False
    for arg in step.args:
        path = _prefetch_path(arg)
        redirected, name = _to_attr(arg)
        if path is None:
            unreadable = True
        elif not redirected:
            found.append(path)
            found.extend(_nested_paths(arg))
        elif name is None:
            # We know the relation is not covered, which is the safe direction,
            # but we cannot name the attribute that did receive the rows.
            unreadable = True
    if any(k.arg is None for k in step.keywords):
        unreadable = True
    return found, unreadable


def _redirects(step: Step) -> frozenset[str]:
    """Attribute names this step's ``Prefetch`` arguments redirect rows to."""
    return frozenset(name for arg in step.args if (name := _to_attr(arg)[1]) is not None)


def analyse(value: QuerysetValue) -> ChainSpec:
    """Read a tracked queryset's chain into a :class:`ChainSpec`."""
    spec = ChainSpec()
    for step in value.chain:
        spec = _apply(spec, step)
    return spec


def _apply(spec: ChainSpec, step: Step) -> ChainSpec:
    name = step.name
    if name in {"select_related", "prefetch_related"}:
        return _apply_fetch(spec, step)
    if name in {"only", "defer"}:
        paths, bad = _lookups(step)
        current = spec.only if name == "only" else spec.defer
        merged = current | frozenset(paths)
        unreadable = spec.unreadable | ({name} if bad else frozenset())
        if name == "only":
            return replace(spec, only=merged, unreadable=unreadable)
        return replace(spec, defer=merged, unreadable=unreadable)
    if name in {"filter", "exclude"}:
        named = tuple(k.arg for k in step.keywords if k.arg is not None)
        bad = bool(step.args) or any(k.arg is None for k in step.keywords)
        return replace(
            spec,
            filters=(*spec.filters, *named),
            unreadable=spec.unreadable | ({name} if bad else frozenset()),
        )
    if name in {"annotate", "alias"}:
        named = tuple(k.arg for k in step.keywords if k.arg is not None)
        return replace(spec, annotated=spec.annotated | frozenset(named))
    if name in NON_INSTANCE:
        return replace(spec, yields_instances=False)
    if name == "order_by":
        return replace(spec, ordered=True)
    if name == "distinct":
        return replace(spec, distinct=True)
    if name in {SLICE, INDEX}:
        return replace(spec, sliced=True, yields_instances=name is not INDEX)
    return spec


def _apply_fetch(spec: ChainSpec, step: Step) -> ChainSpec:
    name = step.name
    current = spec.select_related if name == "select_related" else spec.prefetch_related

    merged: frozenset[str]
    redirected = spec.to_attr
    if any(_is_none(a) for a in step.args):
        # `select_related(None)` clears rather than adds. A reader that only
        # accumulates reports the exact opposite of what the code does.
        merged = frozenset()
        unreadable = spec.unreadable - {name}
        if name != "select_related":
            redirected = frozenset()
    elif name == "select_related" and not step.args and not step.keywords and step.called:
        merged = current | {ALL_FORWARD}
        unreadable = spec.unreadable
    else:
        paths, bad = _lookups(step)
        merged = current | frozenset(paths)
        unreadable = spec.unreadable | ({name} if bad else frozenset())
        redirected = spec.to_attr | _redirects(step)

    if name == "select_related":
        return replace(spec, select_related=merged, unreadable=unreadable)
    return replace(spec, prefetch_related=merged, unreadable=unreadable, to_attr=redirected)
