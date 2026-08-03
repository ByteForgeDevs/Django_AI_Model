"""Deciding what actually guards each endpoint.

The reason this is worth its own pass is a single line of DRF's own
configuration::

    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny']

An installed DRF that nobody configured is open to anonymous callers, and a
view that says nothing about permissions inherits exactly that. So "this view
declares no ``permission_classes``" is not by itself a finding, and neither is
"the project sets no default" -- the answer is the *combination*, which means
the view's ancestry, the project's ``REST_FRAMEWORK`` block and DRF's shipped
default all have to be resolved together before anything can be said.

Both benchmark projects make the second half of that unavoidable. NetBox
defaults to its own ``TokenPermissions`` and pretix to its own
``EventPermission``; neither is a class DRF has heard of, so a pass that only
recognised DRF's eight permission classes would resolve the default on neither
project and report nothing on both while appearing to work.

What this deliberately does not do is guess. A permission class whose
``has_permission`` is hand-written is recorded as :attr:`Enforcement.UNKNOWN`
rather than assumed open or assumed safe, and a :class:`Guard` carries the
unreadable references alongside its verdict so a rule can require certainty
before it reports.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

from djaudit.api.views import UNREADABLE, ExtraAction, ViewNode
from djaudit.settings import entries

if TYPE_CHECKING:
    from djaudit.api.discovery import ApiSurface
    from djaudit.graph.inheritance import ClassIndex
    from djaudit.settings import SettingsView

DEFAULT_PERMISSION_SETTING = "DEFAULT_PERMISSION_CLASSES"
DEFAULT_AUTHENTICATION_SETTING = "DEFAULT_AUTHENTICATION_CLASSES"
DEFAULT_PAGINATION_SETTING = "DEFAULT_PAGINATION_CLASS"
PAGE_SIZE_SETTING = "PAGE_SIZE"
DEFAULT_THROTTLE_SETTING = "DEFAULT_THROTTLE_CLASSES"
THROTTLE_RATES_SETTING = "DEFAULT_THROTTLE_RATES"
DRF_SETTING = "REST_FRAMEWORK"

DRF_DEFAULT_PERMISSIONS = ("rest_framework.permissions.AllowAny",)
"""Transcribed from ``rest_framework/settings.py``. This is the whole reason
the pass exists."""

DRF_DEFAULT_AUTHENTICATION = (
    "rest_framework.authentication.SessionAuthentication",
    "rest_framework.authentication.BasicAuthentication",
)

SAFE_METHODS = frozenset({"get", "head", "options"})
"""``rest_framework.permissions.SAFE_METHODS``, lowercased to match the way
routers and :class:`~djaudit.api.views.ViewNode` spell methods."""


class Enforcement(IntEnum):
    """How much a permission class guarantees, ordered by strength.

    Ordered because DRF's ``check_permissions`` runs every class in the list
    and denies if any one of them denies -- the list is an AND -- so the
    guarantee of a list is the guarantee of its *strongest* member, and
    combining is a maximum.

    :attr:`UNKNOWN` sits below everything on purpose. It is not a strength; it
    is the absence of one, and treating it as weak means combining it with a
    class we do read yields that class's guarantee, which is correct: an
    unreadable class ANDed with ``IsAuthenticated`` still cannot let an
    anonymous caller through.
    """

    UNKNOWN = -1
    """A hand-written class, or one outside the project we cannot see."""

    OPEN = 0
    """Anonymous callers, every method."""

    READ_OPEN = 1
    """Anonymous reads, authenticated writes."""

    AUTHENTICATED = 2
    STAFF = 3


DRF_PERMISSIONS: dict[str, Enforcement] = {
    "rest_framework.permissions.AllowAny": Enforcement.OPEN,
    "rest_framework.permissions.BasePermission": Enforcement.OPEN,
    "rest_framework.permissions.IsAuthenticated": Enforcement.AUTHENTICATED,
    "rest_framework.permissions.IsAdminUser": Enforcement.STAFF,
    "rest_framework.permissions.IsAuthenticatedOrReadOnly": Enforcement.READ_OPEN,
    "rest_framework.permissions.DjangoModelPermissions": Enforcement.AUTHENTICATED,
    "rest_framework.permissions.DjangoModelPermissionsOrAnonReadOnly": Enforcement.READ_OPEN,
    "rest_framework.permissions.DjangoObjectPermissions": Enforcement.AUTHENTICATED,
}
"""Every permission class DRF ships, read from ``rest_framework/permissions.py``.

``BasePermission`` is in here, and it is the entry that earns the table. Its
``has_permission`` returns ``True`` -- so a project class that inherits it and
forgets to override that method is not "a custom permission", it is
``AllowAny`` wearing a reassuring name, and it is the single most valuable
thing this pass can find.

``DjangoModelPermissions`` and ``DjangoObjectPermissions`` set
``authenticated_users_only = True``; ``DjangoModelPermissionsOrAnonReadOnly``
is the same class with that flag flipped off, which is why the two sit at
different strengths.
"""

DRF_AUTHENTICATION = frozenset(
    {
        "rest_framework.authentication.BasicAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.RemoteUserAuthentication",
        "rest_framework.authentication.BaseAuthentication",
    }
)


class Source(StrEnum):
    """Where the answer came from, so a finding can point at it."""

    VIEW = "view"
    """Declared on the view class itself."""

    ANCESTOR = "ancestor"
    """Inherited from a base class the project wrote."""

    ACTION = "action"
    """``@action(permission_classes=[...])`` on one method."""

    DECORATOR = "decorator"
    """``@permission_classes([...])`` on a function view."""

    SETTING = "setting"
    """``REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES']``."""

    DRF_DEFAULT = "drf_default"
    """Nobody configured anything, so DRF's own default applies."""


@dataclass(frozen=True, slots=True)
class Defaults:
    """The project-wide fallback, read from one settings module.

    Built per settings module rather than per project because a project with a
    permissive ``dev.py`` and a locked-down ``production.py`` has two different
    answers, and collapsing them would report the wrong one for both. Choosing
    which module matters is the rules layer's job, which already walks them.
    """

    permissions: tuple[str, ...] = DRF_DEFAULT_PERMISSIONS
    authentication: tuple[str, ...] = DRF_DEFAULT_AUTHENTICATION
    source: Source = Source.DRF_DEFAULT
    permissions_node: ast.expr | None = None
    """The list expression in the settings file, when there was one."""

    settings_node: ast.expr | None = None
    """The ``REST_FRAMEWORK`` value, for when the entry itself is absent and a
    finding has to point at the block that should have contained it."""

    declared_by: str | None = None
    """The settings module the block was written in."""

    permissions_unreadable: bool = False
    """``DEFAULT_PERMISSION_CLASSES`` is assigned but could not be evaluated,
    so the fallback is genuinely unknown rather than DRF's."""

    pagination: str | None = None
    """``DEFAULT_PAGINATION_CLASS``. DRF ships this as ``None``."""

    page_size: int | None = None
    """``PAGE_SIZE``, which DRF also ships as ``None``.

    Kept separate from :attr:`pagination` because the pair is the whole point:
    ``PageNumberPagination.page_size`` *is* ``api_settings.PAGE_SIZE``, and
    ``paginate_queryset`` returns ``None`` when it is falsy. A project that
    names a pagination class and never sets a page size has configured
    pagination that does nothing at all, and looks paginated in every review.
    """

    throttles: tuple[str, ...] = ()
    """``DEFAULT_THROTTLE_CLASSES``, which DRF ships empty."""

    throttle_rates: tuple[str, ...] = ()
    """The scope names in ``DEFAULT_THROTTLE_RATES`` that have a rate set."""


def read_defaults(view: SettingsView | None) -> Defaults:
    """Read ``REST_FRAMEWORK`` out of one settings module.

    Entry-wise rather than through the resolved value, for the reason
    :func:`~djaudit.settings.entries` exists: a real ``REST_FRAMEWORK`` block
    mentions page sizes and renderer lists and version strings, any one of
    which can be built by a call, and a dict collapses to unknown the moment
    one of its entries does. Reading the one key we want on its own keeps the
    answer even when its neighbours are unresolvable.
    """
    if view is None:
        return Defaults()

    module = view.module.dotted
    setting = view.get(DRF_SETTING)
    node = setting.definition.node if setting.definition is not None else None
    value_node = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
    block = entries(view, value_node, setting.value)
    if not block:
        return Defaults(settings_node=value_node, declared_by=module)

    permissions = block.get(DEFAULT_PERMISSION_SETTING)
    authentication = block.get(DEFAULT_AUTHENTICATION_SETTING)
    pagination = block.get(DEFAULT_PAGINATION_SETTING)
    page_size = block.get(PAGE_SIZE_SETTING)
    throttles = block.get(DEFAULT_THROTTLE_SETTING)
    rates = block.get(THROTTLE_RATES_SETTING)

    pagination_value = pagination.value.literal if pagination is not None else None
    size_value = page_size.value.literal if page_size is not None else None
    throttle_value = _string_list(throttles.value.literal) if throttles is not None else None
    rate_value = rates.value.literal if rates is not None else None

    resolved = _string_list(permissions.value.literal) if permissions is not None else None
    authenticators = (
        _string_list(authentication.value.literal) if authentication is not None else None
    )

    return Defaults(
        permissions=resolved if resolved is not None else DRF_DEFAULT_PERMISSIONS,
        authentication=(
            authenticators if authenticators is not None else DRF_DEFAULT_AUTHENTICATION
        ),
        source=Source.SETTING if permissions is not None else Source.DRF_DEFAULT,
        permissions_node=permissions.node if permissions is not None else None,
        settings_node=value_node,
        declared_by=module,
        permissions_unreadable=permissions is not None and resolved is None,
        pagination=pagination_value if isinstance(pagination_value, str) else None,
        page_size=(
            size_value if isinstance(size_value, int) and not isinstance(size_value, bool) else None
        ),
        throttles=throttle_value if throttle_value is not None else (),
        throttle_rates=(
            tuple(str(k) for k, v in rate_value.items() if v)
            if isinstance(rate_value, dict)
            else ()
        ),
    )


def _string_list(literal: object) -> tuple[str, ...] | None:
    """A settings list of dotted import strings, or ``None`` if it is not one.

    Settings spell permission classes as strings, never as imported classes --
    DRF calls ``import_string`` on each one -- so unlike a view's
    ``permission_classes`` there is no module binding to resolve here. An empty
    list is a real and meaningful answer, so it is returned rather than
    conflated with a failure to read.
    """
    if not isinstance(literal, list | tuple):
        return None
    if not all(isinstance(item, str) for item in literal):
        return None
    return tuple(str(item) for item in literal)


@dataclass(frozen=True, slots=True)
class Guard:
    """What stands between an anonymous caller and one endpoint."""

    enforcement: Enforcement
    """The weakest thing guaranteed. A floor, not an estimate: if unreadable
    classes are present the real guarantee may be stronger, never weaker."""

    classes: tuple[str, ...] = ()
    """The permission classes that decided it, resolved to dotted paths."""

    source: Source = Source.DRF_DEFAULT
    declared_by: str | None = None
    """The class or settings module the declaration was written in."""

    unresolved: tuple[str, ...] = ()
    """References that could not be classified. Non-empty means the verdict is
    a floor rather than the answer, and a rule wanting to report "anonymous
    callers can reach this" has to require it to be empty."""

    dynamic: bool = False
    """The view overrides ``get_permissions``, so the attribute is not the
    answer -- whatever it says, a method decides at request time."""

    authentication: tuple[str, ...] = ()
    authentication_source: Source = Source.DRF_DEFAULT

    @property
    def certain(self) -> bool:
        return not self.unresolved and not self.dynamic

    @property
    def is_open(self) -> bool:
        """Anonymous callers can reach every method, and we are sure of it."""
        return self.enforcement is Enforcement.OPEN and self.certain

    @property
    def unauthenticated(self) -> bool:
        """No authentication class at all, so ``request.user`` is always
        anonymous. Harmless with a permissive guard and fatal with a strict
        one -- either way it is never what the author meant."""
        return not self.authentication

    def allows_anonymous(self, method: str) -> bool | None:
        """Whether ``method`` is reachable without logging in.

        ``None`` when we cannot say, which callers must distinguish from
        ``False``.
        """
        if not self.certain:
            return None
        if self.enforcement is Enforcement.OPEN:
            return True
        if self.enforcement is Enforcement.READ_OPEN:
            return method.lower() in SAFE_METHODS
        return False


def classify(dotted: str, index: ClassIndex) -> Enforcement:
    """What one permission class guarantees.

    A DRF class answers from the table. A project class answers from its
    ancestry -- but only if it left the decision to its base. The moment it
    writes its own ``has_permission`` the base's guarantee stops being a
    guarantee, because that method *is* the check.

    With one exception, and it is not a corner case: NetBox's
    ``TokenPermissions`` overrides ``has_permission`` to deny writes from
    read-only tokens and then ends ``return super().has_permission(...)``. An
    override whose every other return is ``False`` can only take permissions
    away, so the base's guarantee still holds as a floor. Refusing to see that
    would make the default permission class of one of the two benchmark
    projects unreadable, and with it every one of its 151 views.

    ``has_object_permission`` is deliberately not consulted. It runs only from
    ``get_object``, so it can protect a detail route and does nothing for a
    list route -- it cannot raise this floor.
    """
    known = DRF_PERMISSIONS.get(dotted)
    if known is not None:
        return known

    record = index.lookup(dotted)
    if record is None:
        return Enforcement.UNKNOWN

    # One ordered walk, nearest first, and each link is asked three questions
    # in the order that makes the answer correct. Is this class itself one DRF
    # ships -- in which case the table is the answer and walking further would
    # descend into DRF's own internals and find ``BasePermission`` writing the
    # very method whose presence we treat as unreadable. Failing that, does it
    # write its own check. Failing that, does it name a DRF base.
    for link in (record, *index.ancestry(record)):
        known = DRF_PERMISSIONS.get(link.dotted)
        if known is not None:
            return known
        method = _method(link.node, "has_permission")
        if method is not None and not _narrowing(method):
            return Enforcement.UNKNOWN
        for base in link.bases:
            found = DRF_PERMISSIONS.get(index.base_target(link, base))
            if found is not None:
                return found
    return Enforcement.UNKNOWN


def combine(refs: tuple[str, ...], index: ClassIndex) -> tuple[Enforcement, tuple[str, ...]]:
    """Fold a ``permission_classes`` list into one verdict plus what was unreadable.

    An empty list is ``OPEN`` and certain: DRF iterates the list and permits
    when nothing denies, so writing ``permission_classes = []`` disables the
    check outright. That is a different fact from "we could not read it", and
    the two must not collapse together -- one is a finding and the other is a
    reason not to report.
    """
    unresolved: list[str] = []
    strength = Enforcement.OPEN
    for ref in refs:
        if ref == UNREADABLE:
            unresolved.append(ref)
            continue
        verdict = classify(ref, index)
        if verdict is Enforcement.UNKNOWN:
            unresolved.append(ref)
            continue
        strength = max(strength, verdict)
    return strength, tuple(unresolved)


def resolve(
    view: ViewNode,
    index: ClassIndex,
    defaults: Defaults,
    action: ExtraAction | None = None,
) -> Guard:
    """What guards this view, or one ``@action`` on it.

    Resolution order is DRF's own: the nearest declaration wins, and the
    project default only applies when nothing in the ancestry declared
    anything. ``@action(permission_classes=[...])`` sits above all of it,
    because those kwargs become ``initkwargs`` and are set as instance
    attributes on the view the router builds.
    """
    if action is not None and action.permissions_set:
        refs = _absolute(action.permission_refs, view.module, index)
        strength, unresolved = combine(refs, index)
        return Guard(
            enforcement=strength,
            classes=refs,
            source=Source.ACTION,
            declared_by=f"{view.label}.{action.name}",
            unresolved=unresolved,
            dynamic="get_permissions" in view.overrides,
            authentication=_authentication(view, index, defaults),
            authentication_source=_authentication_source(view, defaults),
        )

    authentication = _authentication(view, index, defaults)
    authentication_source = _authentication_source(view, defaults)

    if view.permissions_unset:
        strength, unresolved = combine(defaults.permissions, index)
        if defaults.permissions_unreadable:
            unresolved = (*unresolved, DEFAULT_PERMISSION_SETTING)
        return Guard(
            enforcement=strength,
            classes=defaults.permissions,
            source=defaults.source,
            declared_by=defaults.declared_by,
            unresolved=unresolved,
            dynamic="get_permissions" in view.overrides,
            authentication=authentication,
            authentication_source=authentication_source,
        )

    refs = _absolute(view.permission_refs, _module_of(view.permission_source, view), index)
    strength, unresolved = combine(refs, index)
    declared = view.permission_source or view.label
    return Guard(
        enforcement=strength,
        classes=refs,
        source=Source.VIEW if declared == view.label else Source.ANCESTOR,
        declared_by=declared,
        unresolved=unresolved,
        dynamic="get_permissions" in view.overrides,
        authentication=authentication,
        authentication_source=authentication_source,
    )


def _authentication(view: ViewNode, index: ClassIndex, defaults: Defaults) -> tuple[str, ...]:
    """The authenticators for a view, falling back only when it declared none.

    Tested on the declaring class rather than on the list, because
    ``authentication_classes = ()`` is a deliberate and consequential thing to
    write -- pretix's device-initialisation and idempotency endpoints both do
    -- and it means ``request.user`` is anonymous no matter what the project
    default says. Reading an empty list as "nothing declared" would hand those
    views the project's four authenticators and hide the fact.
    """
    if view.authentication_source is None:
        return defaults.authentication
    return _absolute(view.authentication_refs, _module_of(view.authentication_source, view), index)


def _authentication_source(view: ViewNode, defaults: Defaults) -> Source:
    declared = view.authentication_source
    if declared is None:
        return defaults.source if defaults.authentication else Source.DRF_DEFAULT
    return Source.VIEW if declared == view.label else Source.ANCESTOR


def _module_of(declared: str | None, view: ViewNode) -> str:
    """The module a declaration was written in.

    ``declared`` is a class label -- ``netbox.api.viewsets.NetBoxModelViewSet``
    -- because that is what a finding needs to name. Name resolution needs the
    module it sits in, which is everything before the last dot.
    """
    return declared.rpartition(".")[0] if declared else view.module


def _absolute(refs: tuple[str, ...], module: str, index: ClassIndex) -> tuple[str, ...]:
    """Turn names as written into dotted paths.

    A view writes ``permission_classes = [IsAuthenticated]``, which is a name
    in *its* module and means whatever that module imported. When the
    declaration was inherited the module is the ancestor's, not the view's,
    which is why the declaring class is recorded alongside the references.
    """
    out = []
    for ref in refs:
        out.append(ref if ref == UNREADABLE else index.resolve_name(module, ref))
    return tuple(out)


def _method(node: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for stmt in node.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and stmt.name == name:
            return stmt
    return None


def _narrowing(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether an override can only remove permissions its base granted.

    True when every return is either ``False`` or a bare
    ``super().has_permission(...)``, and at least one is the latter. Anything
    else -- returning ``True``, returning an expression, ``or``-ing something
    onto the super call -- is a path that can admit a caller the base would
    have refused, and the base's guarantee no longer bounds it.
    """
    delegates = False
    for stmt in ast.walk(func):
        if not isinstance(stmt, ast.Return):
            continue
        if _is_super_permission_call(stmt.value):
            delegates = True
            continue
        if isinstance(stmt.value, ast.Constant) and stmt.value.value is False:
            continue
        return False
    return delegates


def _is_super_permission_call(node: ast.expr | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "has_permission"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
    )


@dataclass
class GuardMap:
    """Every reachable endpoint's guard, keyed by view label."""

    views: dict[str, Guard] = field(default_factory=dict)
    actions: dict[tuple[str, str], Guard] = field(default_factory=dict)
    """Keyed by ``(view label, action name)`` for ``@action`` overrides."""

    def get(self, label: str, action: str | None = None) -> Guard | None:
        if action is not None and (label, action) in self.actions:
            return self.actions[(label, action)]
        return self.views.get(label)

    def anonymous(self) -> list[str]:
        """Views anonymous callers can reach entirely, with no ambiguity."""
        return sorted(label for label, guard in self.views.items() if guard.is_open)

    def uncertain(self) -> list[str]:
        return sorted(label for label, guard in self.views.items() if not guard.certain)

    def __len__(self) -> int:
        return len(self.views)


def build_guards(surface: ApiSurface, index: ClassIndex, defaults: Defaults) -> GuardMap:
    """Resolve a guard for every routed view.

    Only routed views, because an unrouted one is a base class the project
    wrote for its own viewsets to inherit. It has no URL, so it cannot be an
    authorization defect, and reporting one is how a tool teaches people to
    stop reading its output.
    """
    guards = GuardMap()
    routed = surface.routes.routed()
    for label, view in surface.views.items():
        if label not in routed:
            continue
        guards.views[label] = resolve(view, index, defaults)
        for action in view.extra_actions:
            if action.permissions_set:
                guards.actions[(label, action.name)] = resolve(view, index, defaults, action)
    return guards
