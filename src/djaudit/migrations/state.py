"""What the database looks like at each point in the migration history.

A migration is a delta. `AlterField` states what a column becomes and says
nothing at all about what it was, so "did this change the type" and "did this
make a nullable column NOT NULL" are unanswerable from the operation alone --
and those two questions are most of `DJM-002`.

So the operations are replayed in dependency order, and each one is handed the
state that preceded it. This is a deliberately partial reimplementation of
Django's `ProjectState`: it tracks tables and their columns and nothing else,
because that is all the lock rules ask about. Where Django's version resolves
field classes and builds real model objects, this one carries the parsed
declaration and stops.

Partial is stated rather than implied. `unknown` records the models a replay
could not follow, and a rule that would speak about one is expected to decline.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from djaudit.astutils import literal
from djaudit.graph.fields import field_from_call
from djaudit.migrations.nodes import MigrationNode, Operation, OperationKind

if TYPE_CHECKING:
    from djaudit.graph.nodes import FieldNode
    from djaudit.migrations.graph import MigrationGraph

_SETS_A_COLUMN = frozenset({"AddField", "AlterField"})
"""Operations that leave a column holding exactly the declaration they carry."""

ModelKey = tuple[str, str]
"""``(app_label, lowercased model name)`` -- how a migration names a model."""


@dataclass(slots=True)
class ModelState:
    """One model as the migrations have built it up so far."""

    app: str
    name: str
    fields: dict[str, FieldNode] = field(default_factory=dict)
    created_by: MigrationNode | None = None
    deleted: bool = False

    @property
    def key(self) -> ModelKey:
        return (self.app, self.name)


@dataclass(slots=True)
class MigrationState:
    """Every model known at one point in the migration history."""

    models: dict[ModelKey, ModelState] = field(default_factory=dict)
    unknown: set[ModelKey] = field(default_factory=set)
    """Models whose column set is no longer trustworthy.

    Entered when an operation names a model no ``CreateModel`` was seen for, or
    when a state-only operation moved columns around in a way the replay does
    not model. A rule that reads a field out of an unknown model is reading a
    guess, so it should ask first.
    """

    def get(self, key: ModelKey) -> ModelState | None:
        return self.models.get(key)

    def field_of(self, key: ModelKey, name: str) -> FieldNode | None:
        """One column as it stands, or ``None`` if the replay cannot say.

        ``None`` covers both "no such column" and "this model is not tracked
        well enough to answer", which are different facts. Callers that need to
        tell them apart check :attr:`unknown` -- but callers that only want to
        know whether it is safe to speak get the same answer either way, which
        is the common case and the one worth making hard to get wrong.
        """
        if key in self.unknown:
            return None
        model = self.models.get(key)
        return model.fields.get(name) if model else None

    def copy(self) -> MigrationState:
        """A state that shares nothing mutable with this one.

        Only :func:`final_state` and tests need this; the replay itself
        deliberately does not copy per operation.
        """
        return MigrationState(
            models={
                key: ModelState(
                    app=model.app,
                    name=model.name,
                    fields=dict(model.fields),
                    created_by=model.created_by,
                    deleted=model.deleted,
                )
                for key, model in self.models.items()
            },
            unknown=set(self.unknown),
        )


@dataclass(frozen=True, slots=True)
class Applied:
    """One operation, with the facts about the state that preceded it.

    An earlier draft carried the whole preceding :class:`MigrationState`. That
    is the obvious shape and the wrong one: it copies every model's every column
    once per operation, which measured at 1.07s of NetBox's replay, and no rule
    reads more than three facts out of it. So the three facts are captured as
    the operation is applied and the copy is not made.

    The state *after* is not stored either. It is the ``existing_field`` of
    whatever acts on the column next, and storing it twice would leave one of
    the two as the copy nobody updated.
    """

    migration: MigrationNode
    operation: Operation
    existing_field: FieldNode | None
    """The column this operation acts on, as it stood before it ran.

    ``None`` covers "no such column yet" and "this model is not tracked well
    enough to say", which :attr:`model_tracked` tells apart.
    """

    model_tracked: bool
    """False when the replay lost track of this model's columns.

    A rule reading :attr:`existing_field` off an untracked model is reading a
    guess and should decline instead.
    """

    created_by: MigrationNode | None
    """The migration whose ``CreateModel`` built this table, when known.

    This is the difference between an expensive operation and a free one. A
    non-nullable ``AddField`` against a table created by an *earlier* migration
    rewrites however many rows production has; against a table created by *this*
    migration it rewrites an empty table. Same operation, different incident.
    """

    @property
    def model_key(self) -> ModelKey | None:
        if self.operation.model_name is None:
            return None
        return (self.migration.app, self.operation.model_name)

    @property
    def creates_its_own_table(self) -> bool:
        """Whether the table this touches is created by the same migration.

        Such a table cannot have rows yet, so no operation against it can be
        blocking however long it would take on a populated one.
        """
        return self.created_by is not None and self.created_by.key == self.migration.key


def _create_model(state: MigrationState, app: str, migration: MigrationNode, op: Operation) -> None:
    """Apply a ``CreateModel``, reading its ``fields=[('name', Field()), ...]``."""
    if op.model_name is None:
        return
    model = ModelState(app=app, name=op.model_name, created_by=migration)
    declared = op.argument("fields")
    if declared is None or not _read_declared_fields(model, declared):
        # A model created from a field list we could not read end to end is a
        # model whose columns we do not know. Recording what we did read would
        # have every later AlterField look like it invented the column it
        # alters, and every later AddField look like the first of its name.
        state.unknown.add(model.key)
    state.models[model.key] = model


def _read_declared_fields(model: ModelState, declared: ast.expr) -> bool:
    """``[('name', models.CharField(...)), ...]`` into a model's columns.

    Returns whether every entry was readable. A partial read is the dangerous
    answer: it looks exactly like a complete one to a caller that only checks
    whether any columns came back.
    """
    if not isinstance(declared, (ast.List, ast.Tuple)):
        return False
    complete = True
    for entry in declared.elts:
        if not isinstance(entry, (ast.Tuple, ast.List)) or len(entry.elts) != 2:
            complete = False
            continue
        name = literal(entry.elts[0])
        call = entry.elts[1]
        if not isinstance(name, str) or not isinstance(call, ast.Call):
            complete = False
            continue
        parsed = field_from_call(name, call, {}, assume_field=True)
        if parsed is None:
            complete = False
        else:
            model.fields[name] = parsed
    return complete


def _apply(state: MigrationState, migration: MigrationNode, operation: Operation) -> None:
    """Fold one operation into the running state."""
    app = migration.app
    name = operation.model_name

    if operation.name == "CreateModel":
        _create_model(state, app, migration, operation)
        return

    if name is None:
        return
    key: ModelKey = (app, name)

    model = state.models.get(key)
    if model is None and operation.name not in {"DeleteModel", "RenameModel"}:
        # An operation against a model no CreateModel was seen for. Usually a
        # model from an app whose migrations live in site-packages; sometimes a
        # squash that dropped the create. Either way the column set is a guess.
        state.unknown.add(key)
        model = ModelState(app=app, name=name)
        state.models[key] = model

    if model is not None and operation.field_name and operation.name in _SETS_A_COLUMN:
        # AddField and AlterField are the same operation against state: both
        # leave the column holding exactly the declaration they carry. They
        # differ only in what they mean for the *database*, which is a lock
        # question and not a state one.
        if operation.field is not None:
            model.fields[operation.field_name] = operation.field
        else:
            state.unknown.add(key)
    elif operation.name == "RemoveField" and model is not None and operation.field_name:
        model.fields.pop(operation.field_name, None)
    elif operation.name == "RenameField" and model is not None:
        old, new = operation.old_name, operation.new_name
        if old and new and old in model.fields:
            model.fields[new] = model.fields.pop(old)
        else:
            state.unknown.add(key)
    elif operation.name == "DeleteModel":
        state.models.pop(key, None)
    elif operation.name == "RenameModel":
        _rename_model(state, app, operation)
    elif operation.kind is OperationKind.UNKNOWN:
        # A third-party operation may do anything to the table. Trusting the
        # column set afterwards would be trusting something nobody read.
        state.unknown.add(key)


def _rename_model(state: MigrationState, app: str, operation: Operation) -> None:
    """Move a model's columns to its new name."""
    old, new = operation.old_name, operation.new_name
    if not old or not new:
        return
    source = state.models.pop((app, old.lower()), None)
    target = ModelState(
        app=app,
        name=new.lower(),
        fields=dict(source.fields) if source else {},
        created_by=source.created_by if source else None,
    )
    state.models[target.key] = target
    if source is None:
        state.unknown.add(target.key)
    elif (app, old.lower()) in state.unknown:
        state.unknown.discard((app, old.lower()))
        state.unknown.add(target.key)


def _acted_on(operation: Operation) -> str | None:
    """The name of the column this operation reads, as it stands before it runs.

    Every operation but one names that column in ``field_name``. ``RenameField``
    names it in ``old_name``, because from its own point of view ``field_name``
    is the name it is producing rather than the name it is acting on. Reading
    only ``field_name`` left every rename in all three corpora -- 49 of them --
    with no prior column, which is the operation that most needs one: whether a
    rename is safe depends entirely on what is being renamed.
    """
    if operation.name == "RenameField":
        return operation.old_name
    return operation.field_name


def _capture(state: MigrationState, migration: MigrationNode, operation: Operation) -> Applied:
    """Read the three prior facts out of the state, before the operation edits it."""
    key: ModelKey | None = None
    if operation.model_name is not None:
        key = (migration.app, operation.model_name)

    existing: FieldNode | None = None
    tracked = True
    created: MigrationNode | None = None
    if key is not None:
        tracked = key not in state.unknown
        model = state.models.get(key)
        if model is not None:
            created = model.created_by
            column = _acted_on(operation)
            if tracked and column is not None:
                existing = model.fields.get(column)

    return Applied(
        migration=migration,
        operation=operation,
        existing_field=existing,
        model_tracked=tracked,
        created_by=created,
    )


def replay(graph: MigrationGraph) -> list[Applied]:
    """Every operation in apply order, each with the state that preceded it.

    Returns a list rather than a generator because rules ask about it more than
    once per run and rebuilding the whole history per question would make the
    migration family the most expensive thing in the tool.

    ``SeparateDatabaseAndState`` contributes its database half only. Its state
    half is by construction a no-op against the database, and applying both
    would double every rename it exists to express.
    """
    state = MigrationState()
    applied: list[Applied] = []

    for migration in graph.plan():
        for operation in migration.operations:
            for effective in _effective(operation):
                applied.append(_capture(state, migration, effective))
                _apply(state, migration, effective)

    return applied


def _effective(operation: Operation) -> tuple[Operation, ...]:
    """The operations that actually reach the database."""
    if operation.kind is OperationKind.SEPARATE:
        return operation.inner
    return (operation,)


def final_state(graph: MigrationGraph) -> MigrationState:
    """The state after every migration has been applied."""
    state = MigrationState()
    for migration in graph.plan():
        for operation in migration.operations:
            for effective in _effective(operation):
                _apply(state, migration, effective)
    return state
