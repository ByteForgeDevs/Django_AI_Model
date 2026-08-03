"""What a serializer actually puts on the wire.

A model field is private until something serialises it, so this is where
exposure becomes real. The question a rule needs answered is not "which fields
did the developer list" but "which fields will DRF emit", and those differ
whenever `fields = "__all__"` or `exclude` is used: both are open-ended, so a
column added to the model months later is published without anyone editing
this file.

The distinction that matters most is allowlist versus denylist. `fields = [...]`
is a decision about every column, re-made whenever the list is edited.
`exclude = [...]` is a decision about the columns that existed the day it was
written. `__all__` is no decision at all.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from djaudit.astutils import dotted_name, literal
from djaudit.graph.meta import meta_class
from djaudit.values import UNKNOWN

if TYPE_CHECKING:
    from pathlib import Path

    from djaudit.graph.inheritance import ClassIndex, ClassRecord

MODEL_SERIALIZER_BASES = frozenset(
    {
        "rest_framework.serializers.ModelSerializer",
        "rest_framework.serializers.HyperlinkedModelSerializer",
    }
)

SERIALIZER_BASES = frozenset(
    {
        "rest_framework.serializers.Serializer",
        "rest_framework.serializers.ListSerializer",
        *MODEL_SERIALIZER_BASES,
    }
)

ALL_FIELDS = "__all__"
"""``rest_framework.serializers.ALL_FIELDS``. Named once here, and checked
against the installed DRF by the test suite rather than trusted."""


@dataclass(frozen=True, slots=True)
class SerializerField:
    """A field written out in the serializer body, as opposed to derived."""

    name: str

    kind: str | None = None
    """The class being instantiated, spelled as written -- ``CharField``,
    ``RelatedObjectCountField``. Deliberately unresolved: a project's own field
    class is still a field, and we cannot know what its arguments mean."""

    read_only: bool = False
    write_only: bool = False

    source: str | None = None
    """``source="user.email"`` re-points a field somewhere else, so the name on
    the wire and the column behind it are not the same thing."""

    many: bool = False
    lineno: int = 0
    end_lineno: int = 0


@dataclass
class SerializerNode:
    """One serializer class, and what it will emit."""

    name: str
    module: str
    path: Path
    lineno: int
    end_lineno: int

    is_model_serializer: bool = False
    """False for a plain ``Serializer``, whose fields are entirely declared and
    which therefore cannot over-expose a model by accident."""

    model_ref: str | None = None
    """``Meta.model`` exactly as written."""

    model: str | None = None
    """The resolved ``app_label.ModelName``, when it names a project model."""

    mode: str = "unset"
    """How the field set was chosen: ``explicit``, ``all``, ``exclude`` or
    ``unset``. DRF refuses to build a ModelSerializer with neither option, so
    ``unset`` here means the class inherits its ``Meta`` from an ancestor."""

    fields: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    read_only_fields: tuple[str, ...] = ()

    depth: int = 0
    """``Meta.depth``. Above zero DRF walks relations and serialises whole
    related objects, so what ships is no longer this model's columns."""

    declared: dict[str, SerializerField] = field(default_factory=dict)
    extra_kwargs_read_only: tuple[str, ...] = ()

    unreadable: tuple[str, ...] = ()
    """Meta options that are present but not statically resolvable -- a
    ``fields`` list built by concatenation, say. A rule must not read these as
    absent, which is the same three-state contract the model graph uses."""

    bases: tuple[str, ...] = ()

    meta_inherited: bool = False
    """No ``class Meta`` of its own, so Python resolves ``Meta`` through the
    MRO and an ancestor's options govern this serializer."""

    @property
    def label(self) -> str:
        return f"{self.module}.{self.name}"

    @property
    def is_open_ended(self) -> bool:
        """The field set is not an allowlist, so the model decides what ships.

        True for ``__all__`` and for ``exclude``. Both mean a column added to
        the model later appears on the wire with nobody editing this file,
        which is the mechanism behind most accidental field exposure.
        """
        return self.mode in {"all", "exclude"}

    def declares(self, name: str) -> bool:
        return name in self.declared

    def is_read_only(self, name: str) -> bool:
        """Whether DRF will refuse writes to ``name``.

        Three spellings mean the same thing -- ``read_only_fields``,
        ``extra_kwargs``, and ``read_only=True`` on a declared field -- and a
        rule checking only one of them reports fields that are protected.
        """
        declared = self.declared.get(name)
        return (
            name in self.read_only_fields
            or name in self.extra_kwargs_read_only
            or bool(declared and declared.read_only)
        )


def _names(node: ast.expr | None) -> tuple[tuple[str, ...], bool]:
    """String entries of a list or tuple, and whether anything was lost.

    Element-wise on purpose. NetBox writes field lists across several lines,
    and one entry built by a call must not discard the ninety that are plain
    strings -- the same fidelity problem `Meta.ordering` had in 2.1.5.
    """
    if node is None:
        return (), False
    if not isinstance(node, ast.List | ast.Tuple):
        return (), literal(node) is UNKNOWN
    out: list[str] = []
    partial = False
    for element in node.elts:
        value = literal(element)
        if isinstance(value, str):
            out.append(value)
        else:
            partial = True
    return tuple(out), partial


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


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _bool_kwarg(call: ast.Call, name: str) -> bool:
    found = _kwarg(call, name)
    return found is not None and literal(found) is True


def read_declared(node: ast.ClassDef) -> dict[str, SerializerField]:
    """Fields written in the class body.

    Anything instantiated by a call counts. DRF decides with ``isinstance(obj,
    Field)`` at class-creation time, which we cannot evaluate, and a project's
    own field class is exactly the case not to drop: NetBox's
    ``RelatedObjectCountField`` is a field by any useful definition.
    """
    out: dict[str, SerializerField] = {}
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        kind = dotted_name(call.func)
        source = _kwarg(call, "source")
        source_name = literal(source) if source is not None else None
        for target in stmt.targets:
            if not isinstance(target, ast.Name):
                continue
            out[target.id] = SerializerField(
                name=target.id,
                kind=kind.rpartition(".")[2] if kind else None,
                read_only=_bool_kwarg(call, "read_only"),
                write_only=_bool_kwarg(call, "write_only"),
                source=source_name if isinstance(source_name, str) else None,
                many=_bool_kwarg(call, "many"),
                lineno=stmt.lineno,
                end_lineno=stmt.end_lineno or stmt.lineno,
            )
    return out


def read_extra_kwargs(node: ast.expr | None) -> tuple[tuple[str, ...], bool]:
    """Field names that ``extra_kwargs`` marks read-only.

    ``extra_kwargs = {"password": {"write_only": True}}`` is the usual shape;
    only the read-only half matters here, because that is what decides whether
    a write can reach a field.
    """
    if node is None:
        return (), False
    if not isinstance(node, ast.Dict):
        return (), True
    out: list[str] = []
    partial = False
    for key, value in zip(node.keys, node.values, strict=False):
        name = literal(key) if key is not None else None
        if not isinstance(name, str) or not isinstance(value, ast.Dict):
            partial = True
            continue
        for inner_key, inner_value in zip(value.keys, value.values, strict=False):
            if inner_key is None:
                partial = True
            elif literal(inner_key) == "read_only" and literal(inner_value) is True:
                out.append(name)
    return tuple(out), partial


def build_serializer(record: ClassRecord, index: ClassIndex) -> SerializerNode:
    """Read one serializer class into a node."""
    node = record.node
    serializer = SerializerNode(
        name=record.name,
        module=record.module,
        path=record.path,
        lineno=node.lineno,
        end_lineno=node.end_lineno or node.lineno,
        is_model_serializer=index.inherits(record, MODEL_SERIALIZER_BASES),
        declared=read_declared(node),
        bases=record.bases,
    )

    meta = meta_class(node)
    if meta is None:
        # Meta is an ordinary class attribute, so a serializer without one is
        # governed by its parent's. Saying "no fields" here would be a lie.
        serializer.meta_inherited = True
        return serializer

    unreadable: list[str] = []
    model = _assigned(meta.body, "model")
    if model is not None:
        serializer.model_ref = dotted_name(model)
        if serializer.model_ref is None:
            unreadable.append("model")

    fields_node = _assigned(meta.body, "fields")
    exclude_node = _assigned(meta.body, "exclude")
    if fields_node is not None and literal(fields_node) == ALL_FIELDS:
        serializer.mode = "all"
    elif fields_node is not None:
        serializer.fields, partial = _names(fields_node)
        serializer.mode = "explicit"
        if partial:
            unreadable.append("fields")
    elif exclude_node is not None:
        serializer.exclude, partial = _names(exclude_node)
        serializer.mode = "exclude"
        if partial:
            unreadable.append("exclude")

    serializer.read_only_fields, partial = _names(_assigned(meta.body, "read_only_fields"))
    if partial:
        unreadable.append("read_only_fields")

    serializer.extra_kwargs_read_only, partial = read_extra_kwargs(
        _assigned(meta.body, "extra_kwargs")
    )
    if partial:
        unreadable.append("extra_kwargs")

    depth_node = _assigned(meta.body, "depth")
    depth = literal(depth_node) if depth_node is not None else None
    if isinstance(depth, bool) or not isinstance(depth, int):
        if depth_node is not None:
            unreadable.append("depth")
    else:
        serializer.depth = depth

    serializer.unreadable = tuple(unreadable)
    return serializer
