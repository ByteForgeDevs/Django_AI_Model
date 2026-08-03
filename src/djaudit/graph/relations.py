"""Relations: which model points at which, and how the target was written.

Django lets you name the other side of a relation four ways -- the class
itself, ``"app.Model"``, a bare ``"Model"``, and ``"self"`` -- plus
``settings.AUTH_USER_MODEL``, which is a string that only means anything once
you have read the settings. All five appear in real projects, and an
authorization rule that understands four of them is a rule that goes quiet on
the fifth, which will be the one pointing at the user.
"""

from __future__ import annotations

import ast

from djaudit.astutils import dotted_name, literal, resolve_dotted
from djaudit.graph.nodes import FieldNode, ModelNode, RelationEdge

DEFAULT_USER_MODEL = "auth.User"
"""Django's ``AUTH_USER_MODEL`` default, per ``django/conf/global_settings.py``."""

SELF = "self"

_ON_DELETE_TAIL = {
    "CASCADE",
    "PROTECT",
    "RESTRICT",
    "SET_NULL",
    "SET_DEFAULT",
    "SET",
    "DO_NOTHING",
}

_USER_MODEL_SETTING = "AUTH_USER_MODEL"


def _target_expr(fld: FieldNode) -> ast.expr | None:
    """The expression naming the other model.

    ``to`` is the first positional argument of every relation field, and also
    accepted as a keyword. Both spellings are common enough that supporting one
    would lose relations rather than merely inconvenience anyone.
    """
    if "to" in fld.kwargs:
        return fld.kwargs["to"]
    return fld.args[0] if fld.args else None


def read_target(fld: FieldNode, bindings: dict[str, str]) -> tuple[str | None, bool]:
    """``(reference, is_the_user_setting)`` for a relation field.

    The reference is returned as written -- ``"self"`` stays ``"self"``, a bare
    class name stays bare -- because resolving it needs the model's own app
    label, which this does not have and the caller does.
    """
    expr = _target_expr(fld)
    if expr is None:
        return None, False

    text = literal(expr)
    if isinstance(text, str):
        return text, False

    name = dotted_name(expr)
    if name is None:
        return None, False

    resolved = resolve_dotted(bindings, name)
    if resolved.rpartition(".")[2] == _USER_MODEL_SETTING:
        # settings.AUTH_USER_MODEL is the correct way to point at the user, and
        # it is a string whose value lives in a different file entirely.
        return None, True

    # A direct class reference: ``ForeignKey(Order, ...)``. The tail is the
    # class name, which is what a bare string reference would have been.
    return resolved.rpartition(".")[2] or resolved, False


def read_on_delete(fld: FieldNode) -> str | None:
    """The ``on_delete`` policy as a bare name, e.g. ``CASCADE``.

    Positional because ``ForeignKey(Target, models.CASCADE)`` is legal and
    still written, even though the keyword form is now universal in docs.
    """
    expr = fld.kwargs.get("on_delete")
    if expr is None and len(fld.args) >= 2:
        expr = fld.args[1]
    if expr is None:
        return None
    if isinstance(expr, ast.Call):
        # models.SET(some_callable)
        expr = expr.func
    name = dotted_name(expr)
    if name is None:
        return None
    tail = name.rpartition(".")[2]
    return tail if tail in _ON_DELETE_TAIL else None


def read_through(fld: FieldNode) -> str | None:
    """The ``through`` model of a many-to-many, as written."""
    expr = fld.kwargs.get("through")
    if expr is None:
        return None
    text = literal(expr)
    if isinstance(text, str):
        return text
    name = dotted_name(expr)
    return name.rpartition(".")[2] if name else None


def _string_kwarg(fld: FieldNode, name: str) -> str | None:
    value = literal(fld.kwargs[name]) if name in fld.kwargs else None
    return value if isinstance(value, str) else None


def _bool_kwarg(fld: FieldNode, name: str) -> bool | None:
    value = literal(fld.kwargs[name]) if name in fld.kwargs else None
    return value if isinstance(value, bool) else None


def interpolate(template: str, model: ModelNode, *, model_name: bool = True) -> str:
    """Expand Django's ``related_name`` placeholders against the owning model.

    ``django/db/models/fields/related.py`` substitutes ``%(class)s``,
    ``%(app_label)s`` and -- for ``related_name`` only -- ``%(model_name)s``,
    using the class that ends up owning the field. An abstract base declaring
    ``related_name="%(class)s_orders"`` is the reason the mechanism exists: one
    declaration, a distinct accessor on every heir.
    """
    values = {"class": model.name.lower(), "app_label": model.app_label.lower()}
    if model_name:
        values["model_name"] = model.name.lower()
    try:
        return template % values
    except (KeyError, ValueError, TypeError):
        # A related_name containing a stray % is a crash in Django too. We are
        # not the place to discover that, so the template is kept verbatim.
        return template


def accessor_for(edge: RelationEdge, owner: ModelNode) -> None:
    """Fill in the reverse accessor and query name, following Django exactly.

    ``ForeignObjectRel.get_accessor_name`` is ``related_name`` if set, else the
    target's lowercased model name plus ``_set`` when one row can have many.
    ``related_query_name()`` falls back differently -- through
    ``related_query_name``, then ``related_name``, then the bare model name --
    and the two being confused is how a rule builds a ``filter()`` path Django
    would reject.
    """
    if owner.is_abstract:
        # An abstract model has no table, so Django creates no reverse relation
        # for it and leaves any placeholder unexpanded -- deliberately, because
        # the same declaration has to yield a different name on every heir.
        # Naming one here would invent an accessor nothing can use.
        return

    # default_related_name lives on the model that *declares* the field, not
    # the one it points at: it names the relation "from a related object back
    # to this one", so Book.Meta.default_related_name = "books" gives
    # Author.books.
    declared = edge.related_name or owner.default_related_name
    if declared is not None:
        declared = interpolate(declared, owner)

    edge.hidden = bool(declared and declared.endswith("+"))

    if edge.symmetrical is None:
        # ManyToManyField(symmetrical=...) defaults to True for a relation to
        # self, and a symmetrical self-relation has no reverse accessor at all.
        edge.symmetrical = edge.kind == "ManyToManyField" and edge.is_self

    if edge.hidden or (edge.symmetrical and edge.is_self and edge.kind == "ManyToManyField"):
        edge.accessor = None
    elif declared:
        edge.accessor = declared
    else:
        edge.accessor = owner.name.lower() + ("_set" if edge.is_multi_reverse else "")

    query = edge.related_query_name
    if query is not None:
        edge.query_name = interpolate(query, owner, model_name=False)
    elif declared and not edge.hidden:
        edge.query_name = declared
    else:
        edge.query_name = owner.name.lower()


def build_edges(model: ModelNode, bindings: dict[str, str]) -> list[RelationEdge]:
    """Every relation declared on this model, unresolved.

    Resolution happens once the whole graph exists, because a bare ``"Order"``
    may refer to a model in a module we have not read yet.
    """
    edges = []
    for fld in model.fields.values():
        if not fld.is_relation:
            continue
        ref, is_user_setting = read_target(fld, bindings)
        edges.append(
            RelationEdge(
                source=model.label,
                field_name=fld.name,
                kind=fld.kind,
                target_ref=ref,
                via_user_setting=is_user_setting,
                on_delete=read_on_delete(fld),
                through=read_through(fld),
                related_name=_string_kwarg(fld, "related_name"),
                related_query_name=_string_kwarg(fld, "related_query_name"),
                symmetrical=_bool_kwarg(fld, "symmetrical"),
                null=fld.null,
                lineno=fld.lineno,
                end_lineno=fld.end_lineno,
            )
        )
    return edges


def resolve_edges(
    graph_models: dict[str, ModelNode],
    user_model: str,
    incoming: dict[str, list[RelationEdge]] | None = None,
) -> None:
    """Point every edge at a model, now that all of them are known.

    ``self`` is the declaring model. ``settings.AUTH_USER_MODEL`` is whatever
    the settings said, which for most projects is a model Django ships and we
    therefore do not have -- so the edge records that it points at the user
    without pretending to have the class.
    """
    by_label = graph_models

    def lookup(ref: str, app_label: str) -> str | None:
        if "." in ref:
            return ref if ref in by_label else None
        local = f"{app_label}.{ref}"
        if local in by_label:
            return local
        matches = [label for label, m in by_label.items() if m.name == ref]
        return matches[0] if len(matches) == 1 else None

    for model in by_label.values():
        # The user model resolved from this model's app, computed once: a bare
        # reference is looked up in the declaring app first, so in principle it
        # varies, and in practice AUTH_USER_MODEL is always fully qualified.
        resolved_user = lookup(user_model, model.app_label)
        user_name = user_model.rpartition(".")[2]

        for edge in model.relations:
            if edge.via_user_setting:
                edge.target = resolved_user
                edge.points_at_user = True
                continue
            if edge.target_ref is None:
                continue
            if edge.target_ref == SELF:
                edge.target = model.label
                edge.is_self = True
                edge.points_at_user = model.label == resolved_user
                continue

            edge.target = lookup(edge.target_ref, model.app_label)
            if edge.target is not None:
                edge.points_at_user = edge.target == resolved_user
            else:
                # An unresolved reference naming the configured user model is
                # pointing at the user: Django's own auth.User is referenced
                # constantly and is never in the repository. Restricted to the
                # unresolved case on purpose -- a project with its own User in
                # some other app resolves to that model, and this never fires.
                edge.points_at_user = edge.target_ref in {user_model, user_name}

    for model in by_label.values():
        for edge in model.relations:
            accessor_for(edge, model)
            if incoming is not None and edge.target is not None and edge.accessor:
                incoming.setdefault(edge.target, []).append(edge)
