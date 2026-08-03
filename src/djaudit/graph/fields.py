"""Reading a model's fields out of its class body.

A field declaration is a call, and almost everything a rule wants to know
about it is in the call's keywords: whether it can be null, whether it is
indexed, whether it has a default, what it is limited to. The work here is
mostly in being careful about the difference between *absent*, *present but
unreadable*, and *present and false* — three states that a boolean cannot
hold, and that rules downstream have to tell apart to avoid claiming
something the source never said.
"""

from __future__ import annotations

import ast
from typing import Any

from djaudit.astutils import UNKNOWN, dotted_name, literal, resolve_dotted
from djaudit.graph.nodes import FieldNode

DJANGO_FIELDS = frozenset(
    {
        "AutoField",
        "BigAutoField",
        "BigIntegerField",
        "BinaryField",
        "BooleanField",
        "CharField",
        "CommaSeparatedIntegerField",
        "CompositePrimaryKey",
        "DateField",
        "DateTimeField",
        "DecimalField",
        "DurationField",
        "EmailField",
        "Field",
        "FileField",
        "FilePathField",
        "FloatField",
        "ForeignKey",
        "ForeignObject",
        "GeneratedField",
        "GenericIPAddressField",
        "IPAddressField",
        "ImageField",
        "IntegerField",
        "JSONField",
        "ManyToManyField",
        "NullBooleanField",
        "OneToOneField",
        "OrderWrt",
        "PositiveBigIntegerField",
        "PositiveIntegerField",
        "PositiveSmallIntegerField",
        "SlugField",
        "SmallAutoField",
        "SmallIntegerField",
        "TextField",
        "TimeField",
        "URLField",
        "UUIDField",
    }
)
"""Every ``Field`` subclass exported by ``django.db.models`` in 6.0."""

CONTRIB_FIELDS = frozenset(
    {
        # django.contrib.contenttypes.fields — neither name ends in "Field",
        # so without listing them a generic relation would be invisible, and
        # generic relations are where a surprising amount of data hides.
        "GenericForeignKey",
        "GenericRelation",
        # django.contrib.postgres.fields
        "ArrayField",
        "HStoreField",
        "CIText",
        "CICharField",
        "CIEmailField",
        "CITextField",
        "IntegerRangeField",
        "BigIntegerRangeField",
        "DecimalRangeField",
        "DateTimeRangeField",
        "DateRangeField",
    }
)

RELATION_FIELDS = frozenset(
    {"ForeignKey", "OneToOneField", "ManyToManyField", "ForeignObject", "GenericRelation"}
)

NOT_A_FIELD = frozenset(
    {
        # Things that appear in a model body and are emphatically not columns.
        "Manager",
        "Index",
        "UniqueConstraint",
        "CheckConstraint",
        "Q",
        "F",
        "Value",
        "property",
        "classmethod",
        "staticmethod",
    }
)

_BOOL_KWARGS = {
    "null": False,
    "blank": False,
    "unique": False,
    "db_index": False,
    "primary_key": False,
    "editable": True,
    "auto_now": False,
    "auto_now_add": False,
}
"""Django's own defaults, so an absent keyword reports what Django would do."""


def field_kind(call: ast.Call, bindings: dict[str, str]) -> str | None:
    """The field class being called, or ``None`` if this is not a field.

    A name is a field when Django exports it as one, or -- for the project's
    own and third-party fields, which we cannot enumerate -- when it ends in
    ``Field``. That convention is near-universal and the alternative is
    dropping every custom column in the project, which for NetBox would be
    dozens.
    """
    name = dotted_name(call.func)
    if name is None:
        return None
    tail = resolve_dotted(bindings, name).rpartition(".")[2] or name
    if tail in NOT_A_FIELD:
        return None
    if tail in DJANGO_FIELDS or tail in CONTRIB_FIELDS:
        return tail
    return tail if tail.endswith("Field") else None


def _iter_field_assignments(body: list[ast.stmt]) -> list[tuple[str, ast.Assign | ast.AnnAssign]]:
    """Name/statement pairs for every assignment in a class body.

    Descends into ``if`` and ``try`` because a field guarded by a database
    backend check or an optional dependency is still a column when the branch
    is taken, and a rule that never sees it will report the model as missing
    something it has.
    """
    found: list[tuple[str, ast.Assign | ast.AnnAssign]] = []
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    found.append((target.id, stmt))
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                found.append((stmt.target.id, stmt))
        elif isinstance(stmt, ast.If):
            found.extend(_iter_field_assignments(stmt.body))
            found.extend(_iter_field_assignments(stmt.orelse))
        elif isinstance(stmt, ast.Try):
            found.extend(_iter_field_assignments(stmt.body))
            for handler in stmt.handlers:
                found.extend(_iter_field_assignments(handler.body))
            found.extend(_iter_field_assignments(stmt.orelse))
    return found


def _read_choices(node: ast.expr) -> tuple[Any, bool]:
    """``(value, is_readable)`` for a ``choices=`` argument.

    A literal list of pairs is read. A ``TextChoices`` class, a callable or a
    module-level constant is not, and says so -- Django accepts all three, and
    a rule that treats unreadable choices as no choices would report every
    project that keeps its choices in an enum.
    """
    value = literal(node)
    if value is UNKNOWN:
        return UNKNOWN, False
    if isinstance(value, (list, tuple)):
        return tuple(
            tuple(item) if isinstance(item, (list, tuple)) else item for item in value
        ), True
    return value, True


def extract_fields(class_node: ast.ClassDef, bindings: dict[str, str]) -> dict[str, FieldNode]:
    """Every field declared directly in this class body, in declaration order.

    Inherited fields are not here: they belong to the class that declared them,
    and flattening inheritance is substep 2.1.6, which needs the whole project.
    """
    fields: dict[str, FieldNode] = {}

    for name, stmt in _iter_field_assignments(class_node.body):
        value = stmt.value
        if not isinstance(value, ast.Call):
            continue
        kind = field_kind(value, bindings)
        if kind is None:
            continue

        keywords = {kw.arg: kw.value for kw in value.keywords if kw.arg is not None}
        field = FieldNode(
            name=name,
            kind=kind,
            dotted=resolve_dotted(bindings, dotted_name(value.func) or kind),
            lineno=stmt.lineno,
            end_lineno=stmt.end_lineno or stmt.lineno,
            is_django=kind in DJANGO_FIELDS or kind in CONTRIB_FIELDS,
            is_relation=kind in RELATION_FIELDS,
            args=tuple(value.args),
            kwargs=keywords,
            node=value,
        )

        unreadable: list[str] = []
        for kwarg, default in _BOOL_KWARGS.items():
            if kwarg not in keywords:
                setattr(field, kwarg, default)
                continue
            resolved = literal(keywords[kwarg])
            if isinstance(resolved, bool):
                setattr(field, kwarg, resolved)
            else:
                # A flag computed at import time -- from a setting, a feature
                # check -- is not a flag we can report on. Django's default is
                # recorded so the field is still usable, and the name is listed
                # so a rule can decline to speak about it.
                setattr(field, kwarg, default)
                unreadable.append(kwarg)

        if "max_length" in keywords:
            resolved = literal(keywords["max_length"])
            if isinstance(resolved, int):
                field.max_length = resolved
            else:
                unreadable.append("max_length")

        if "default" in keywords:
            field.has_default = True
            field.default = literal(keywords["default"])
            if field.default is UNKNOWN:
                # A callable default is the normal case, not a failure:
                # ``default=timezone.now`` is exactly right and unreadable.
                unreadable.append("default")

        if "choices" in keywords:
            field.has_choices = True
            field.choices, readable = _read_choices(keywords["choices"])
            if not readable:
                unreadable.append("choices")

        field.unreadable = tuple(unreadable)
        fields[name] = field

    return fields
