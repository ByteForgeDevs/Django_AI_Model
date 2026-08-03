"""Applying what an ancestor declares to the class that inherits it.

Django's two inheritance modes look alike in source and mean opposite things
in the database. An abstract base has no table, so each heir gets its own copy
of every column — `created` on NetBox's `ChangeLoggingMixin` becomes a real
column on all ~180 models that inherit it. A concrete base *does* have a table,
so its heir stores nothing locally and reaches those columns over an implicit
one-to-one join.

Copying fields down in the second case would invent columns that do not exist,
and skipping them in the first would miss most of the schema. So the walk stops
at the first concrete ancestor and records it as a parent link instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from djaudit.graph.relations import accessor_for, interpolate

if TYPE_CHECKING:
    from collections.abc import Callable

    from djaudit.graph.inheritance import ClassRecord
    from djaudit.graph.nodes import ModelNode

MTI_SUFFIX = "_ptr"


def apply_inheritance(
    model: ModelNode,
    ancestry: tuple[ClassRecord, ...],
    node_for: Callable[[ClassRecord], ModelNode],
) -> None:
    """Fill in everything ``model`` gets from its bases.

    ``ancestry`` is nearest-first, which is also precedence order: the first
    declaration of a name wins, and the class's own body outranks all of them.
    """
    if model.inheritance_applied:
        return
    model.inheritance_applied = True

    for record in ancestry:
        ancestor = node_for(record)
        model.mro = (*model.mro, ancestor.label)

        if ancestor.is_concrete:
            if ancestor.label not in model.parents:
                model.parents = (*model.parents, ancestor.label)
                if model.is_proxy:
                    # A proxy is the same table under a second class. Its
                    # columns are all readable, and it adds no reverse
                    # relations -- those belong to the concrete model, and
                    # copying them would have two classes claiming one
                    # accessor on everything they point at.
                    model.db_table = ancestor.db_table
                    _inherit_fields(model, ancestor)
                else:
                    # Multi-table inheritance. The ancestor keeps its own
                    # table, so its columns stay there and are reached over
                    # the one-to-one Django adds rather than copied here.
                    _add_parent_link(model, ancestor)
            continue

        _inherit_fields(model, ancestor)
        _inherit_relations(model, ancestor)
        _inherit_meta(model, ancestor)
        _inherit_managers(model, ancestor)


def _inherit_fields(model: ModelNode, ancestor: ModelNode) -> None:
    for name, fld in ancestor.fields.items():
        if name in model.fields or name in model.inherited:
            continue
        model.inherited[name] = fld


def _inherit_relations(model: ModelNode, ancestor: ModelNode) -> None:
    """Copy an abstract base's relations onto the heir, renamed for it.

    This is the reason ``related_name="%(class)s_orders"`` exists. The base
    declares one relation and every heir needs a distinct reverse name, so the
    accessor is recomputed against the heir rather than carried over -- two
    models sharing a base would otherwise claim the same attribute on the
    model they both point at.
    """
    from dataclasses import replace  # noqa: PLC0415  (only needed here)

    declared = {edge.field_name for edge in model.relations}
    for edge in ancestor.relations:
        if edge.field_name in declared:
            continue
        copy = replace(
            edge,
            source=model.label,
            inherited_from=ancestor.label,
            accessor=None,
            query_name=None,
            hidden=False,
        )
        accessor_for(copy, model)
        model.relations.append(copy)


def _inherit_managers(model: ModelNode, ancestor: ModelNode) -> None:
    """Managers come down from an abstract base like anything else.

    ``Options.managers`` walks the whole MRO, so a model declaring none still
    has its base's -- which is the common case in NetBox, where the
    ``RestrictedQuerySet`` manager is attached once on a base class and reached
    by every model under it.
    """
    for name, manager in ancestor.managers.items():
        model.managers.setdefault(name, manager)


def _inherit_meta(model: ModelNode, ancestor: ModelNode) -> None:
    """Options a class did not set but its abstract base did.

    Django rebuilds ``Meta`` from the base when the heir declares none, and an
    heir writing ``class Meta(Base.Meta)`` inherits every option it does not
    override. Both end with the base's ordering and constraints in force, and a
    rule reading only the heir's body would report a model with no default
    ordering when every query it makes is sorted.
    """
    if not model.ordering and not model.ordering_unreadable:
        model.ordering = ancestor.ordering
        model.ordering_unreadable = ancestor.ordering_unreadable
    if not model.unique_together:
        model.unique_together = ancestor.unique_together
    if not model.indexes:
        model.indexes = ancestor.indexes
    if not model.constraints:
        model.constraints = ancestor.constraints
    if model.default_related_name is None:
        model.default_related_name = ancestor.default_related_name
    for edge in model.relations:
        # An inherited default_related_name changes names computed before it
        # was known, and a placeholder in one is expanded per heir.
        if edge.related_name is None and model.default_related_name:
            accessor_for(edge, model)


def _add_parent_link(model: ModelNode, parent: ModelNode) -> None:
    """The implicit one-to-one Django adds for multi-table inheritance.

    ``Restaurant(Place)`` gets a ``place_ptr`` primary key that is also a
    foreign key to ``place``. Every query on the child joins across it, so a
    graph that omits it cannot explain the join or trace ownership through it.
    """
    from djaudit.graph.nodes import FieldNode, RelationEdge  # noqa: PLC0415  (cycle)

    name = f"{parent.name.lower()}{MTI_SUFFIX}"
    if name in model.fields or name in model.inherited:
        return
    model.inherited[name] = FieldNode(
        name=name,
        kind="OneToOneField",
        dotted="django.db.models.OneToOneField",
        lineno=model.lineno,
        end_lineno=model.lineno,
        is_relation=True,
        primary_key=True,
        unique=True,
    )
    edge = RelationEdge(
        source=model.label,
        field_name=name,
        kind="OneToOneField",
        target_ref=parent.label,
        target=parent.label,
        lineno=model.lineno,
        end_lineno=model.lineno,
        on_delete="CASCADE",
        related_name=interpolate("%(class)s", model),
        inherited_from=parent.label,
        implicit=True,
    )
    accessor_for(edge, model)
    model.relations.append(edge)
