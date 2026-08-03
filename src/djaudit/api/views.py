"""What a view exposes, and by which HTTP methods.

A serializer decides the shape of the data; a view decides who can ask for it
and which rows they get back. Both halves are needed before a rule can say
anything about authorization, and this is the half that says what an endpoint
*is*.

DRF spells a view four ways and they do not share a vocabulary:

* ``APIView`` answers the HTTP methods it defines handlers for;
* the concrete generics answer a fixed set implied by the class name, with no
  handler written anywhere in the project;
* viewsets answer *actions* -- ``list``, ``retrieve``, ``destroy`` -- which
  only become HTTP methods once a router binds them, which is 2.2.3;
* ``@api_view`` turns a plain function into a view class at import time, so
  nothing about it is a class at all.

The tables below are transcribed from DRF's source rather than remembered.
``rest_framework.generics`` defines each concrete generic's handlers directly
(``ListCreateAPIView`` gets ``get`` and ``post``), and
``rest_framework.viewsets`` composes ``ModelViewSet`` out of five mixins, each
of which contributes the action named after it -- except ``UpdateModelMixin``,
which contributes two.

Ancestry has to be enumerated rather than followed. DRF is not part of the
project, so the class index cannot see that ``ListAPIView`` descends from
``APIView``: the chain stops at the first name that leaves the repository.
Every DRF base a project can inherit from is therefore named here explicitly,
which is also what keeps a bare mixin from being mistaken for a view.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from djaudit.astutils import dotted_name, literal, resolve_dotted

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.graph.inheritance import ClassIndex, ClassRecord

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
"""``django.views.generic.base.View.http_method_names``."""

APIVIEW = "rest_framework.views.APIView"
GENERIC_APIVIEW = "rest_framework.generics.GenericAPIView"
VIEWSET_MIXIN = "rest_framework.viewsets.ViewSetMixin"
VIEWSET = "rest_framework.viewsets.ViewSet"
GENERIC_VIEWSET = "rest_framework.viewsets.GenericViewSet"
MODEL_VIEWSET = "rest_framework.viewsets.ModelViewSet"
READ_ONLY_MODEL_VIEWSET = "rest_framework.viewsets.ReadOnlyModelViewSet"

API_VIEW_DECORATOR = "rest_framework.decorators.api_view"
ACTION_DECORATOR = "rest_framework.decorators.action"

GENERIC_METHODS: dict[str, tuple[str, ...]] = {
    "rest_framework.generics.CreateAPIView": ("post",),
    "rest_framework.generics.ListAPIView": ("get",),
    "rest_framework.generics.RetrieveAPIView": ("get",),
    "rest_framework.generics.DestroyAPIView": ("delete",),
    "rest_framework.generics.UpdateAPIView": ("put", "patch"),
    "rest_framework.generics.ListCreateAPIView": ("get", "post"),
    "rest_framework.generics.RetrieveUpdateAPIView": ("get", "put", "patch"),
    "rest_framework.generics.RetrieveDestroyAPIView": ("get", "delete"),
    "rest_framework.generics.RetrieveUpdateDestroyAPIView": (
        "get",
        "put",
        "patch",
        "delete",
    ),
}
"""Each concrete generic's handlers, read off ``rest_framework/generics.py``.

These are the methods no grep will find: ``ListCreateAPIView`` answers GET and
POST with handlers defined in DRF, so a project subclassing it writes neither.
"""

ACTION_MIXINS: dict[str, tuple[str, ...]] = {
    "rest_framework.mixins.CreateModelMixin": ("create",),
    "rest_framework.mixins.ListModelMixin": ("list",),
    "rest_framework.mixins.RetrieveModelMixin": ("retrieve",),
    "rest_framework.mixins.UpdateModelMixin": ("update", "partial_update"),
    "rest_framework.mixins.DestroyModelMixin": ("destroy",),
}
"""``UpdateModelMixin`` is the one that does not follow its own name: it brings
``partial_update`` as well, which is how PATCH reaches a viewset."""

COMPOSITE_VIEWSETS: dict[str, tuple[str, ...]] = {
    MODEL_VIEWSET: (
        "create",
        "retrieve",
        "update",
        "partial_update",
        "destroy",
        "list",
    ),
    READ_ONLY_MODEL_VIEWSET: ("retrieve", "list"),
}
"""The two viewsets that are nothing but mixins, expanded here because the
mixins they compose are DRF's and so invisible to the class index."""

VIEWSET_BASES = frozenset({VIEWSET_MIXIN, VIEWSET, GENERIC_VIEWSET, *COMPOSITE_VIEWSETS})

VIEW_BASES = frozenset({APIVIEW, GENERIC_APIVIEW, *GENERIC_METHODS, *VIEWSET_BASES})
"""Every DRF class that makes its subclass a view.

The mixins are deliberately absent. A class inheriting only
``ListModelMixin`` is a fragment with a ``list`` method, not an endpoint, and
counting it would put a base class in the surface next to the twelve real
viewsets that use it.
"""

OVERRIDABLE = frozenset(
    {
        "get_queryset",
        "get_serializer_class",
        "get_permissions",
        "get_authenticators",
        "get_object",
    }
)
"""Methods whose presence means an attribute cannot be read as the answer.

Recorded here, resolved later: a ``get_queryset`` that narrows to
``request.user`` and one that returns ``Model.objects.all()`` are the whole
difference between 2.3's authorization rules firing and not, and reading that
is 2.2.5's job.
"""


UNREADABLE = "?"
"""Stands in for a ``*_classes`` entry we could not render to a name.

A sentinel rather than an omission so that "there was nothing here" and "there
was something here we could not read" stay distinguishable all the way to the
rule that has to decide whether to report."""


@dataclass(frozen=True, slots=True)
class ExtraAction:
    """A method promoted to a route by ``@action``."""

    name: str
    detail: bool | None
    """``None`` when it could not be read. DRF asserts it is passed, so this
    means the argument was an expression rather than a literal."""

    methods: tuple[str, ...] = ("get",)
    """DRF defaults to GET and lowercases whatever it is given."""

    url_path: str | None = None
    lineno: int = 0

    permission_refs: tuple[str, ...] = ()
    """``@action(permission_classes=[...])``, spelled as written.

    Worth reading despite being rare, because the kwargs of ``@action`` become
    ``initkwargs`` and DRF sets them as attributes on the view instance the
    router builds -- so this genuinely overrides the viewset's own
    ``permission_classes`` for this one route and nothing else."""

    permissions_set: bool = False
    """Whether the kwarg was passed at all, which an empty list cannot say."""


@dataclass
class ViewNode:
    """One view, however it was spelled."""

    name: str
    module: str
    path: Path
    lineno: int
    end_lineno: int

    kind: str = "apiview"
    """``apiview``, ``generic``, ``viewset`` or ``function``."""

    http_methods: tuple[str, ...] = ()
    """Methods this view answers directly.

    Empty for a viewset, whose methods are decided by the router that binds
    it, and which reports :attr:`actions` instead.
    """

    actions: tuple[str, ...] = ()
    """Viewset actions, from the mixins in its ancestry."""

    extra_actions: tuple[ExtraAction, ...] = ()

    serializer_ref: str | None = None
    """``serializer_class`` as written."""

    queryset_ref: str | None = None
    """``queryset`` as written, unresolved -- ``Circuit.objects.all()``."""

    queryset_model_ref: str | None = None
    """The name the queryset starts from, which is almost always the model.

    ``Circuit.objects.all()`` gives ``Circuit``. Separated from the source text
    because a rule asking "which model does this endpoint expose" should not
    have to parse a string, and 2.2.5 resolves this against the model graph.
    """

    permission_refs: tuple[str, ...] = ()
    authentication_refs: tuple[str, ...] = ()

    permission_source: str | None = None
    """The class whose body declared :attr:`permission_refs`.

    Not always this view. The names are written in *that* class's module and
    resolve through *its* imports, so without recording it a reference
    inherited from a base in another package resolves against the wrong
    namespace and lands on nothing."""

    authentication_source: str | None = None

    permissions_unset: bool = True
    """No ``permission_classes`` anywhere in the ancestry, so the project's
    ``DEFAULT_PERMISSION_CLASSES`` decides. Whether that default is safe is
    2.2.4's question; this only records that nobody answered it here."""

    overrides: frozenset[str] = frozenset()
    """Which of :data:`OVERRIDABLE` this view or a project ancestor defines."""

    defined: frozenset[str] = frozenset()
    """Every method name this view or a project ancestor defines.

    DRF's router binds a mapping entry only when ``hasattr(viewset, action)``
    holds, and the actions a router can name are not limited to the standard
    six. NetBox adds ``bulk_update``, ``bulk_partial_update`` and
    ``bulk_destroy`` in a mixin and points its list route at them, so the
    honest test for "is this action implemented" is the same one DRF makes --
    does the name exist -- rather than membership of a fixed list.
    """

    pagination_ref: str | None = None
    """``pagination_class`` as written, or ``None`` when nobody set one."""

    pagination_disabled: bool = False
    """``pagination_class = None``, which switches the project default off.

    Distinct from never setting it, because one is a decision and the other is
    a default, and a finding that cannot tell them apart names the wrong file.
    """

    filter_backend_refs: tuple[str, ...] = ()
    filterset_ref: str | None = None
    filterset_fields_node: ast.expr | None = None
    """``filterset_fields`` as written, kept unevaluated so ``'__all__'`` and a
    list are distinguishable without a second pass."""

    throttle_refs: tuple[str, ...] = ()
    throttle_scope: str | None = None
    throttles_unset: bool = True
    """No ``throttle_classes`` anywhere in the ancestry, so
    ``DEFAULT_THROTTLE_CLASSES`` decides -- which DRF ships empty."""

    bases: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.module}.{self.name}"

    @property
    def is_viewset(self) -> bool:
        return self.kind == "viewset"

    @property
    def writes(self) -> bool:
        """Whether anything here can change data.

        The question every authorization rule opens with, and it has to span
        both vocabularies: an endpoint is writable through an HTTP method on a
        generic, an action on a viewset, or either on an ``@action``.
        """
        if {"post", "put", "patch", "delete"} & set(self.http_methods):
            return True
        if {"create", "update", "partial_update", "destroy"} & set(self.actions):
            return True
        return any(
            {"post", "put", "patch", "delete"} & set(extra.methods) for extra in self.extra_actions
        )

    def overrides_queryset(self) -> bool:
        return "get_queryset" in self.overrides


def _defined_methods(node: ast.ClassDef) -> set[str]:
    return {
        stmt.name for stmt in node.body if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _assigned(body: list[ast.stmt], name: str) -> ast.expr | None:
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            annotated = stmt.target
            if isinstance(annotated, ast.Name) and annotated.id == name and stmt.value:
                return stmt.value
    return None


def _class_refs(body: list[ast.stmt], name: str) -> tuple[str, ...]:
    """Entries of a ``*_classes`` list, spelled as written.

    Element-wise, so one entry built by a call does not discard the rest --
    the same fidelity rule the serializer field lists follow.

    An entry that renders to nothing becomes :data:`UNREADABLE` rather than
    disappearing, because ``permission_classes = []`` and
    ``permission_classes = [IsAuthenticated | IsAdminUser]`` must not arrive
    at the caller looking identical. The first genuinely disables the check
    and is a finding; the second is DRF's ``OperandHolder`` composition, whose
    guarantee is its *weakest* operand, and silently reading it as an empty
    list would turn a locked-down view into a reported vulnerability.
    """
    value = _assigned(body, name)
    return () if value is None else _list_refs(value)


def _list_refs(value: ast.expr) -> tuple[str, ...]:
    """The same read, for a list written as a keyword argument."""
    if not isinstance(value, ast.List | ast.Tuple):
        rendered = dotted_name(value)
        return (rendered,) if rendered else (UNREADABLE,)
    return tuple(dotted_name(element) or UNREADABLE for element in value.elts)


def _methods_kwarg(call: ast.Call) -> tuple[str, ...]:
    for kw in call.keywords:
        if kw.arg != "methods":
            continue
        value = literal(kw.value)
        if isinstance(value, list | tuple):
            return tuple(str(m).lower() for m in value if isinstance(m, str))
        return ()
    return ("get",)


def read_extra_actions(node: ast.ClassDef, bindings: dict[str, str]) -> tuple[ExtraAction, ...]:
    """Methods carrying ``@action``, which the router turns into extra routes.

    These matter out of proportion to how often they are written. A viewset's
    six standard actions are covered by whatever guards the viewset, while an
    ``@action`` is a hand-written endpoint that frequently forgets to be --
    and ``detail=False`` means it never loads an object, so an object-level
    permission cannot save it.
    """
    found: list[ExtraAction] = []
    for stmt in node.body:
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in stmt.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if resolve_dotted(bindings, dotted_name(decorator.func) or "") != ACTION_DECORATOR:
                continue
            detail = None
            url_path = None
            permissions: tuple[str, ...] = ()
            permissions_set = False
            for kw in decorator.keywords:
                if kw.arg == "detail":
                    value = literal(kw.value)
                    detail = value if isinstance(value, bool) else None
                elif kw.arg == "url_path":
                    value = literal(kw.value)
                    url_path = value if isinstance(value, str) else None
                elif kw.arg == "permission_classes":
                    permissions_set = True
                    permissions = _list_refs(kw.value)
            found.append(
                ExtraAction(
                    name=stmt.name,
                    detail=detail,
                    methods=_methods_kwarg(decorator),
                    url_path=url_path,
                    lineno=stmt.lineno,
                    permission_refs=permissions,
                    permissions_set=permissions_set,
                )
            )
    return tuple(found)


def base_targets(record: ClassRecord, index: ClassIndex) -> set[str]:
    """Every base named anywhere in this class's chain, resolved to dotted paths.

    Unlike ``ClassIndex.ancestry``, which yields only classes that exist in
    this project, this keeps the names that leave it -- and those are exactly
    the DRF ones the tables key on.
    """
    targets = {index.base_target(record, base) for base in record.bases}
    for parent in index.ancestry(record):
        targets |= {index.base_target(parent, base) for base in parent.bases}
    return targets


def _root_name(node: ast.expr) -> str | None:
    """The leftmost name in an attribute or call chain.

    ``Circuit.objects.all()`` and ``Circuit.objects.filter(x=1)`` both answer
    ``Circuit``. A queryset built from anything else -- a function call, a
    variable -- answers ``None`` rather than a guess.
    """
    current = node
    while True:
        if isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Attribute):
            current = current.value
        elif isinstance(current, ast.Name):
            return current.id
        else:
            return None


def _inherited_actions(chain: tuple[ClassRecord, ...]) -> tuple[ExtraAction, ...]:
    """``@action`` methods from the whole chain, nearest declaration winning.

    An action declared on a mixin is a route on every viewset that mixes it
    in. NetBox puts two of its twelve there -- ``render-config`` in
    ``extras/api/mixins.py`` and one in ``netbox/api/features.py`` -- so
    reading only the class's own body misses real, writable endpoints on
    dozens of viewsets.
    """
    found: dict[str, ExtraAction] = {}
    for link in chain:
        for extra in read_extra_actions(link.node, link.bindings):
            found.setdefault(extra.name, extra)
    return tuple(found.values())


@dataclass(slots=True)
class _Availability:
    """The four class attributes that decide how much work one request can ask for.

    Read in a pass of their own rather than inside the main ancestry walk,
    because they answer a different question from authorization and grouping
    them keeps either from having to be understood to change the other.
    """

    pagination_ref: str | None = None
    pagination_disabled: bool = False
    filter_backend_refs: tuple[str, ...] = ()
    filterset_ref: str | None = None
    filterset_fields_node: ast.expr | None = None
    throttle_refs: tuple[str, ...] = ()
    throttle_scope: str | None = None
    throttles_unset: bool = True


def _read_availability(chain: Sequence[ClassRecord]) -> _Availability:
    """Nearest declaration wins, as everywhere else in this pass."""
    out = _Availability()
    for link in chain:
        body = link.node.body
        if out.pagination_ref is None and not out.pagination_disabled:
            declared = _assigned(body, "pagination_class")
            if declared is not None:
                # `pagination_class = None` switches the project default off
                # deliberately; never setting it accepts whatever the default
                # is. A rule that cannot tell those apart names the wrong file.
                if isinstance(declared, ast.Constant) and declared.value is None:
                    out.pagination_disabled = True
                else:
                    out.pagination_ref = dotted_name(declared)
        if not out.filter_backend_refs:
            out.filter_backend_refs = _class_refs(body, "filter_backends")
        if out.filterset_ref is None:
            declared = _assigned(body, "filterset_class")
            out.filterset_ref = dotted_name(declared) if declared is not None else None
        if out.filterset_fields_node is None:
            out.filterset_fields_node = _assigned(body, "filterset_fields")
        if out.throttles_unset:
            throttles = _class_refs(body, "throttle_classes")
            if throttles or _assigned(body, "throttle_classes") is not None:
                out.throttles_unset = False
                out.throttle_refs = throttles
        if out.throttle_scope is None:
            declared = _assigned(body, "throttle_scope")
            scope = literal(declared) if declared is not None else None
            out.throttle_scope = scope if isinstance(scope, str) else None
    return out


def build_view(record: ClassRecord, index: ClassIndex) -> ViewNode:
    """Read one view class, following its ancestry for anything inherited."""
    targets = base_targets(record, index)
    chain = (record, *index.ancestry(record))

    actions: set[str] = set()
    for target in targets:
        actions.update(ACTION_MIXINS.get(target, ()))
        actions.update(COMPOSITE_VIEWSETS.get(target, ()))

    is_viewset = bool(targets & VIEWSET_BASES) or bool(actions)

    methods: set[str] = set()
    if not is_viewset:
        for target in targets:
            methods.update(GENERIC_METHODS.get(target, ()))
        for link in chain:
            methods |= _defined_methods(link.node) & HTTP_METHODS

    # A viewset written by hand rather than composed from mixins names its
    # actions as plain methods, so they are only visible this way.
    if is_viewset:
        for link in chain:
            actions |= _defined_methods(link.node) & {
                "list",
                "create",
                "retrieve",
                "update",
                "partial_update",
                "destroy",
            }

    overrides: set[str] = set()
    defined: set[str] = set()
    serializer_ref: str | None = None
    queryset_ref: str | None = None
    queryset_model_ref: str | None = None
    permissions: tuple[str, ...] = ()
    authentication: tuple[str, ...] = ()
    permissions_unset = True
    permission_source: str | None = None
    authentication_source: str | None = None

    # Nearest declaration wins, which is why the chain is walked in order and
    # the first answer is kept rather than the last.
    for link in chain:
        own = _defined_methods(link.node)
        defined |= own
        overrides |= own & OVERRIDABLE
        if serializer_ref is None:
            declared = _assigned(link.node.body, "serializer_class")
            serializer_ref = dotted_name(declared) if declared is not None else None
        if queryset_ref is None:
            assigned = _assigned(link.node.body, "queryset")
            if assigned is not None:
                queryset_ref = ast.unparse(assigned)
                queryset_model_ref = _root_name(assigned)
        if permission_source is None:
            permissions = _class_refs(link.node.body, "permission_classes")
            if permissions or _assigned(link.node.body, "permission_classes") is not None:
                permissions_unset = False
                permission_source = link.dotted
        if authentication_source is None:
            authentication = _class_refs(link.node.body, "authentication_classes")
            if authentication or _assigned(link.node.body, "authentication_classes") is not None:
                authentication_source = link.dotted

    availability = _read_availability(chain)

    kind = (
        "viewset"
        if is_viewset
        else ("generic" if targets & {GENERIC_APIVIEW, *GENERIC_METHODS} else "apiview")
    )

    return ViewNode(
        name=record.name,
        module=record.module,
        path=record.path,
        lineno=record.node.lineno,
        end_lineno=record.node.end_lineno or record.node.lineno,
        kind=kind,
        http_methods=tuple(sorted(methods)),
        actions=tuple(sorted(actions)),
        extra_actions=_inherited_actions(chain),
        serializer_ref=serializer_ref,
        queryset_ref=queryset_ref,
        queryset_model_ref=queryset_model_ref,
        permission_refs=permissions,
        authentication_refs=authentication,
        permission_source=permission_source,
        authentication_source=authentication_source,
        permissions_unset=permissions_unset,
        pagination_ref=availability.pagination_ref,
        pagination_disabled=availability.pagination_disabled,
        filter_backend_refs=availability.filter_backend_refs,
        filterset_ref=availability.filterset_ref,
        filterset_fields_node=availability.filterset_fields_node,
        throttle_refs=availability.throttle_refs,
        throttle_scope=availability.throttle_scope,
        throttles_unset=availability.throttles_unset,
        overrides=frozenset(overrides),
        defined=frozenset(defined),
        bases=record.bases,
    )


def build_function_view(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    path: Path,
    bindings: dict[str, str],
) -> ViewNode | None:
    """A function carrying ``@api_view``, which DRF rebuilds as an ``APIView``.

    ``@api_view`` with no argument list means GET only, which is easy to write
    by accident and is the reason the default is recorded rather than left
    empty: a POST-shaped function decorated with a bare ``@api_view`` does not
    answer POST at all.
    """
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        named = dotted_name(call.func if call else decorator) or ""
        if resolve_dotted(bindings, named) != API_VIEW_DECORATOR:
            continue
        methods: tuple[str, ...] = ("get",)
        if call and call.args:
            value = literal(call.args[0])
            if isinstance(value, list | tuple):
                methods = tuple(sorted({str(m).lower() for m in value if isinstance(m, str)}))
        decorators = [
            dotted_name(d.func if isinstance(d, ast.Call) else d) or "" for d in node.decorator_list
        ]
        return ViewNode(
            name=node.name,
            module=module,
            path=path,
            lineno=node.lineno,
            end_lineno=node.end_lineno or node.lineno,
            kind="function",
            http_methods=methods,
            permission_refs=_decorator_refs(node, bindings, "permission_classes"),
            authentication_refs=_decorator_refs(node, bindings, "authentication_classes"),
            permission_source=f"{module}.{node.name}",
            authentication_source=f"{module}.{node.name}",
            permissions_unset="rest_framework.decorators.permission_classes"
            not in {resolve_dotted(bindings, d) for d in decorators},
            bases=(API_VIEW_DECORATOR,),
        )
    return None


def _decorator_refs(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: dict[str, str],
    name: str,
) -> tuple[str, ...]:
    """``@permission_classes([...])`` -- the function-view spelling.

    A function view cannot carry class attributes, so DRF provides a decorator
    per attribute. Same information, different syntax, and a rule that only
    looked for the attribute would find every function view unguarded.
    """
    wanted = f"rest_framework.decorators.{name}"
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if resolve_dotted(bindings, dotted_name(decorator.func) or "") != wanted:
            continue
        if not decorator.args:
            return ()
        argument = decorator.args[0]
        if not isinstance(argument, ast.List | ast.Tuple):
            rendered = dotted_name(argument)
            return (rendered,) if rendered else ()
        return tuple(r for r in (dotted_name(e) for e in argument.elts) if r)
    return ()
