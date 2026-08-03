"""Reading ``class Meta``.

Meta is where a model says the things that are not fields: what its table is
called, how it is ordered by default, which columns are indexed and which
combinations must be unique. Later rules ask exactly those questions — is this
filtered column indexed, does this table have a default ordering that sorts
every page, does this constraint exist — so the answers have to come from
Django's own semantics rather than from what the option name suggests.

Three of those semantics are easy to get wrong and are handled explicitly:
``unique_together`` accepts both a tuple of tuples and a bare tuple of strings
and means one constraint either way; ``db_table`` is derived rather than absent
when unset; and an index carrying a ``condition`` is partial, so it does not
answer "is this column indexed" in the affirmative.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from djaudit.astutils import literal

if TYPE_CHECKING:
    from djaudit.graph.nodes import ModelNode

from djaudit.graph.nodes import ConstraintNode, IndexNode

META_FLAGS = {
    "abstract": "is_abstract",
    "proxy": "is_proxy",
    "managed": "managed",
}

META_STRINGS = {
    "default_related_name": "default_related_name",
    "swappable": "swappable",
    "base_manager_name": "base_manager_name",
    "default_manager_name": "default_manager_name",
}

INDEX_NAMES = frozenset({"Index"})
CONSTRAINT_NAMES = frozenset(
    {"UniqueConstraint", "CheckConstraint", "ExclusionConstraint", "BaseConstraint"}
)


def meta_class(node: ast.ClassDef) -> ast.ClassDef | None:
    """The inner ``class Meta`` of a model, if it has one."""
    for stmt in node.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
            return stmt
    return None


def class_attr(node: ast.ClassDef, name: str) -> ast.expr | None:
    """The last assignment to ``name`` directly in a class body."""
    found: ast.expr | None = None
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
                found = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            if isinstance(target, ast.Name) and target.id == name and stmt.value is not None:
                found = stmt.value
    return found


def class_attr_literal(node: ast.ClassDef, name: str) -> object | None:
    value = class_attr(node, name)
    return None if value is None else literal(value)


SEQUENCES = (ast.List, ast.Tuple, ast.Set)


def string_sequence(node: ast.expr | None) -> tuple[tuple[str, ...], bool]:
    """The string elements of a sequence, and whether any element was not one.

    Read element by element rather than by evaluating the container, because
    NetBox writes ``ordering = ('device', CollateAsChar('_name'))`` and a
    whole-container read gives up on both. The first column is the one a rule
    asking "is the default ordering indexed?" needs, and it is right there.

    The shortfall is reported alongside so that "no ordering" and "ordering we
    could only partly read" stay distinguishable; they warrant different
    findings, and merging them produces a confident claim about a model whose
    ordering we do not actually know.
    """
    if node is None:
        return (), False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,), False
    if not isinstance(node, SEQUENCES):
        # A name, a call, a concatenation: nothing element-wise to salvage.
        return (), True
    names: list[str] = []
    partial = False
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            names.append(element.value)
        else:
            partial = True
    return tuple(names), partial


def normalize_together(node: ast.expr | None) -> tuple[tuple[str, ...], ...]:
    """``unique_together`` as a tuple of tuples, following Django.

    ``django.db.models.options.normalize_together`` accepts ``("a", "b")`` and
    ``(("a", "b"),)`` and means the same thing by neither: the first is one
    constraint over two columns, the second is the same constraint written
    unambiguously. Reading the bare form as two single-column constraints
    inverts what the model guarantees -- it would claim each column is unique
    on its own.
    """
    if not isinstance(node, SEQUENCES) or not node.elts:
        return ()
    if not isinstance(node.elts[0], SEQUENCES):
        group, _ = string_sequence(node)
        return (group,) if group else ()
    out = []
    for element in node.elts:
        group, _ = string_sequence(element)
        if group:
            out.append(group)
    return tuple(out)


def call_name(node: ast.expr) -> str | None:
    """The callee of a call, as its final attribute name."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _string_list(call: ast.Call, name: str) -> tuple[str, ...]:
    fields, _ = string_sequence(_kwarg(call, name))
    return fields


def read_indexes(node: ast.expr | None) -> tuple[IndexNode, ...]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return ()
    out: list[IndexNode] = []
    for item in node.elts:
        if call_name(item) not in INDEX_NAMES or not isinstance(item, ast.Call):
            continue
        name = literal(_kwarg(item, "name") or ast.Constant(None))
        out.append(
            IndexNode(
                fields=_string_list(item, "fields"),
                name=name if isinstance(name, str) else None,
                conditional=_kwarg(item, "condition") is not None,
                # Positional arguments to Index() are expressions, which is how
                # a functional index like Index(Lower("email")) is written.
                expressions=bool(item.args),
                lineno=item.lineno,
            )
        )
    return tuple(out)


def read_constraints(node: ast.expr | None) -> tuple[ConstraintNode, ...]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return ()
    out: list[ConstraintNode] = []
    for item in node.elts:
        kind = call_name(item)
        if kind is None or not isinstance(item, ast.Call):
            continue
        if kind not in CONSTRAINT_NAMES and not kind.endswith("Constraint"):
            continue
        name = literal(_kwarg(item, "name") or ast.Constant(None))
        out.append(
            ConstraintNode(
                kind=kind,
                fields=_string_list(item, "fields"),
                name=name if isinstance(name, str) else None,
                conditional=_kwarg(item, "condition") is not None,
                lineno=item.lineno,
            )
        )
    return tuple(out)


def derive_table(model: ModelNode) -> str:
    """The table name Django gives a model with no ``Meta.db_table``.

    ``Options.contribute_to_class`` builds ``app_label_modelname`` and then
    truncates it to the backend's identifier limit. The limit is backend
    knowledge we do not have statically, so the untruncated form is used: it is
    the name in every project that has not gone past 63 characters, which on
    Postgres is nearly all of them.
    """
    return f"{model.app_label}_{model.name.lower()}"


def read_meta(model: ModelNode, meta: ast.ClassDef | None) -> None:
    """Fill in everything ``class Meta`` says about ``model``.

    Called before the model is keyed into the graph, because ``Meta.app_label``
    changes the label every string reference resolves against.
    """
    if meta is not None:
        model.meta_bases = tuple(
            base.attr if isinstance(base, ast.Attribute) else base.id
            for base in meta.bases
            if isinstance(base, (ast.Name, ast.Attribute))
        )

        for attr, target in META_FLAGS.items():
            value = class_attr_literal(meta, attr)
            if isinstance(value, bool):
                setattr(model, target, value)

        for attr, target in META_STRINGS.items():
            value = class_attr_literal(meta, attr)
            if isinstance(value, str) and value:
                setattr(model, target, value)

        label = class_attr_literal(meta, "app_label")
        if isinstance(label, str) and label:
            model.app_label = label

        table = class_attr_literal(meta, "db_table")
        if isinstance(table, str) and table:
            model.db_table = table
            model.db_table_explicit = True

        ordering = class_attr(meta, "ordering")
        if ordering is not None:
            model.ordering, model.ordering_unreadable = string_sequence(ordering)

        model.unique_together = normalize_together(class_attr(meta, "unique_together"))

        model.indexes = read_indexes(class_attr(meta, "indexes"))
        model.constraints = read_constraints(class_attr(meta, "constraints"))

    if not model.db_table_explicit and not model.is_abstract:
        model.db_table = derive_table(model)
