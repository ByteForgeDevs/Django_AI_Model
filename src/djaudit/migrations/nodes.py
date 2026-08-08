"""What a migration file says, as data.

Read from source and never imported. That is not the usual static-analysis
preference here, it is a hard requirement: a migration module runs at import
time, and the ones most worth auditing are exactly the ones that would reach
for a database connection while doing it.

The vocabulary is Django's. ``model_name`` is lowercased because that is how
``makemigrations`` writes it, and normalising it to the model's real casing
here would mean every comparison against a migration's own text needed
un-normalising again.
"""

from __future__ import annotations

import ast
import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from djaudit.graph.nodes import FieldNode


class OperationKind(StrEnum):
    """What an operation does to the database, as opposed to what it is called.

    The split that matters is not Django's class hierarchy but the answer to
    "what lock does this take and for how long", because that is the only
    question a deployment cares about. ``AlterModelOptions`` and ``AddField``
    are both ``ModelOperation`` subclasses and one of them emits no SQL at all.
    """

    SCHEMA = "schema"
    """Changes a table's columns: ``AddField``, ``AlterField``, ``CreateModel``."""

    INDEX = "index"
    """Creates or drops an index. Locks differ sharply from schema changes."""

    CONSTRAINT = "constraint"
    """Adds or drops a constraint, which may or may not scan the table."""

    RUN_PYTHON = "run_python"
    """Arbitrary Python against the database. Unbounded, and often unreversible."""

    RUN_SQL = "run_sql"
    """Arbitrary SQL. We can read it as text and nothing more."""

    SEPARATE = "separate"
    """``SeparateDatabaseAndState``: state and database halves stated apart."""

    STATE = "state"
    """Recorded in migration state and emits no DDL: ``AlterModelOptions``."""

    UNKNOWN = "unknown"
    """A third-party or project-defined operation. Never assumed harmless."""


DATA_KINDS = frozenset({OperationKind.RUN_PYTHON, OperationKind.RUN_SQL})
"""The plan's "data" category, which Django spells as two separate classes.

Kept as a set rather than a single ``DATA`` member because the two differ in
what can be said about them: a ``RunSQL`` body is text we can read, and a
``RunPython`` body is a function we can only follow by name.
"""


_KIND_BY_OPERATION: dict[str, OperationKind] = {
    # Model-level schema
    "CreateModel": OperationKind.SCHEMA,
    "DeleteModel": OperationKind.SCHEMA,
    "RenameModel": OperationKind.SCHEMA,
    "AlterModelTable": OperationKind.SCHEMA,
    "AlterModelTableComment": OperationKind.SCHEMA,
    # Field-level schema
    "AddField": OperationKind.SCHEMA,
    "RemoveField": OperationKind.SCHEMA,
    "AlterField": OperationKind.SCHEMA,
    "RenameField": OperationKind.SCHEMA,
    # Indexes
    "AddIndex": OperationKind.INDEX,
    "RemoveIndex": OperationKind.INDEX,
    "RenameIndex": OperationKind.INDEX,
    "AddIndexConcurrently": OperationKind.INDEX,
    "RemoveIndexConcurrently": OperationKind.INDEX,
    "AlterIndexTogether": OperationKind.INDEX,
    # Constraints
    "AddConstraint": OperationKind.CONSTRAINT,
    "RemoveConstraint": OperationKind.CONSTRAINT,
    "AddConstraintNotValid": OperationKind.CONSTRAINT,
    "ValidateConstraint": OperationKind.CONSTRAINT,
    "AlterUniqueTogether": OperationKind.CONSTRAINT,
    "AlterConstraint": OperationKind.CONSTRAINT,
    "AlterOrderWithRespectTo": OperationKind.SCHEMA,
    # Data
    "RunPython": OperationKind.RUN_PYTHON,
    "RunSQL": OperationKind.RUN_SQL,
    # State-only
    "SeparateDatabaseAndState": OperationKind.SEPARATE,
    "AlterModelOptions": OperationKind.STATE,
    "AlterModelManagers": OperationKind.STATE,
    # django.contrib.postgres
    "CreateCollation": OperationKind.SCHEMA,
    "RemoveCollation": OperationKind.SCHEMA,
    "CreateExtension": OperationKind.SCHEMA,
    "BtreeGinExtension": OperationKind.SCHEMA,
    "BtreeGistExtension": OperationKind.SCHEMA,
    "CITextExtension": OperationKind.SCHEMA,
    "CryptoExtension": OperationKind.SCHEMA,
    "HStoreExtension": OperationKind.SCHEMA,
    "TrigramExtension": OperationKind.SCHEMA,
    "UnaccentExtension": OperationKind.SCHEMA,
}
"""Django's built-in operations, by what they do rather than what they extend.

``AlterOrderWithRespectTo`` is schema and not constraint: it adds an ``_order``
integer column, which is a table rewrite in exactly the way ``AddField`` is.
``AlterIndexTogether`` and ``AlterUniqueTogether`` are the deprecated spellings
and still appear throughout the corpus, so both are classified rather than
falling through to ``UNKNOWN`` and being reported as unclassifiable.
"""


@dataclasses.dataclass(frozen=True, slots=True)
class Signature:
    """One operation's positional parameters, and which of them names a model."""

    params: tuple[str, ...]
    model_param: str | None = None


def _sig(*params: str, model: str | None = None) -> Signature:
    return Signature(params=params, model_param=model)


SIGNATURES: dict[str, Signature] = {
    # django.db.migrations.operations.models
    "CreateModel": _sig("name", "fields", "options", "bases", "managers", model="name"),
    "DeleteModel": _sig("name", model="name"),
    "RenameModel": _sig("old_name", "new_name", model="old_name"),
    "AlterModelTable": _sig("name", "table", model="name"),
    "AlterModelTableComment": _sig("name", "table_comment", model="name"),
    "AlterUniqueTogether": _sig("name", "unique_together", model="name"),
    "AlterIndexTogether": _sig("name", "index_together", model="name"),
    "AlterOrderWithRespectTo": _sig("name", "order_with_respect_to", model="name"),
    "AlterModelOptions": _sig("name", "options", model="name"),
    "AlterModelManagers": _sig("name", "managers", model="name"),
    "AddIndex": _sig("model_name", "index", model="model_name"),
    "RemoveIndex": _sig("model_name", "name", model="model_name"),
    "RenameIndex": _sig("model_name", "new_name", "old_name", "old_fields", model="model_name"),
    "AddConstraint": _sig("model_name", "constraint", model="model_name"),
    "RemoveConstraint": _sig("model_name", "name", model="model_name"),
    "AlterConstraint": _sig("model_name", "name", "constraint", model="model_name"),
    # django.db.migrations.operations.fields
    "AddField": _sig("model_name", "name", "field", "preserve_default", model="model_name"),
    "RemoveField": _sig("model_name", "name", model="model_name"),
    "AlterField": _sig("model_name", "name", "field", "preserve_default", model="model_name"),
    "RenameField": _sig("model_name", "old_name", "new_name", model="model_name"),
    # django.db.migrations.operations.special -- none of these names a model
    "RunSQL": _sig("sql", "reverse_sql", "state_operations", "hints", "elidable"),
    "RunPython": _sig("code", "reverse_code", "atomic", "hints", "elidable"),
    "SeparateDatabaseAndState": _sig("database_operations", "state_operations"),
    # django.contrib.postgres.operations
    "AddIndexConcurrently": _sig("model_name", "index", model="model_name"),
    "RemoveIndexConcurrently": _sig("model_name", "index_name", model="model_name"),
    "AddConstraintNotValid": _sig("model_name", "constraint", model="model_name"),
    "ValidateConstraint": _sig("model_name", "name", model="model_name"),
    "CreateExtension": _sig("name"),
    "CreateCollation": _sig("name", "locale", "provider", "deterministic"),
    "RemoveCollation": _sig("name", "locale", "provider", "deterministic"),
}
"""Each operation's positional parameters and its model, in Django's own order.

``makemigrations`` writes every argument by keyword, so the corpus never
exercises position and a single guessed offset looks correct across all 875 of
its migrations. Hand-written migrations are the ones that use position -- and
hand-written migrations are what the lock rules exist to catch.

No single offset serves them all. ``RenameModel(old, new)`` names the old model
first; ``RenameField(model, old, new)`` names the model first and the old column
second, so reading position 0 as ``old_name`` for both makes every positional
``RenameField`` rename its own table. ``field`` is position 2 of ``AddField``
and nothing at all of ``RunSQL``, whose position 0 is a SQL statement that a
shared offset reports as a model name.

``model_param`` is recorded rather than guessed for the same reason. It is
``model_name`` for field operations, ``name`` for model operations and
``old_name`` for ``RenameModel`` -- and for ``CreateCollation`` the ``name``
parameter is a collation, which a rule scanning for ``model_name`` then ``name``
would report as a table.

An operation missing from this table is a third-party one, read by the looser
rules in the parser: it may still name a model, and an operation whose model
went unrecognised could not be marked unreadable against that model.
"""


def argument(op_name: str, call: ast.Call, param: str) -> ast.expr | None:
    """One argument of an operation call, given by keyword or by Django's position.

    The single place that knows where an argument lives. Every hand-rolled
    ``call.args[1]`` is a guess that happens to be right for the operation its
    author had in mind and wrong for the next one.
    """
    for keyword in call.keywords:
        if keyword.arg == param:
            return keyword.value
    signature = SIGNATURES.get(op_name)
    if signature is not None and param in signature.params:
        position = signature.params.index(param)
        if len(call.args) > position:
            return call.args[position]
    return None


CONCURRENT_OPERATIONS = frozenset({"AddIndexConcurrently", "RemoveIndexConcurrently"})
"""Postgres-only operations that build an index without an exclusive lock."""


@dataclass(frozen=True, slots=True)
class Dependency:
    """One entry of a migration's ``dependencies``.

    ``swappable`` marks ``migrations.swappable_dependency(settings.AUTH_USER_MODEL)``,
    whose app is not knowable from the migration file alone -- it is whatever
    ``AUTH_USER_MODEL`` resolves to, which the settings resolver can answer and
    this parser deliberately does not guess at.
    """

    app: str
    name: str
    swappable: bool = False
    unreadable: bool = False
    """The entry was not two string literals, so it names nothing we can follow."""

    def __str__(self) -> str:
        return f"{self.app}.{self.name}"


@dataclass(frozen=True, slots=True)
class Operation:
    """One entry of a migration's ``operations`` list."""

    name: str
    """The operation class as written, e.g. ``AddField``."""

    kind: OperationKind
    lineno: int
    end_lineno: int
    model_name: str | None = None
    """Lowercased, as ``makemigrations`` writes it. ``None`` when not model-scoped."""

    field_name: str | None = None
    old_name: str | None = None
    new_name: str | None = None
    field: FieldNode | None = None
    """The declared field, for ``AddField`` and ``AlterField``.

    Parsed by the model graph's own field reader, so ``null`` here means the
    same thing it means on a model.
    """

    reverse: bool | None = None
    """Whether a reverse was supplied. ``None`` when the question does not apply."""

    sql: str | None = None
    """The forward SQL of a ``RunSQL``, when it was a readable string."""

    callable_name: str | None = None
    """The forward function of a ``RunPython``, by name."""

    inner: tuple[Operation, ...] = ()
    """The database half of a ``SeparateDatabaseAndState``."""

    kwargs: dict[str, ast.expr] = dataclasses.field(default_factory=dict, repr=False, compare=False)
    node: ast.Call | None = dataclasses.field(default=None, repr=False, compare=False)

    def argument(self, param: str) -> ast.expr | None:
        """One of this operation's arguments, by Django's name for it."""
        if self.node is None:
            return None
        return argument(self.name, self.node, param)

    @property
    def touches_data(self) -> bool:
        return self.kind in DATA_KINDS

    @property
    def concurrent(self) -> bool:
        return self.name in CONCURRENT_OPERATIONS


@dataclass(frozen=True, slots=True)
class MigrationNode:
    """One migration file.

    ``app`` is the resolved app *label*, not the directory name, because that is
    what ``dependencies`` entries are written against and a project with an
    ``AppConfig.label`` override would otherwise produce a graph whose edges
    point at nothing.
    """

    app: str
    name: str
    path: Path
    dependencies: tuple[Dependency, ...] = ()
    operations: tuple[Operation, ...] = ()
    run_before: tuple[Dependency, ...] = ()
    replaces: tuple[Dependency, ...] = ()
    atomic: bool = True
    """Django's default. ``False`` only when the class says so."""

    initial: bool | None = None
    unreadable: tuple[str, ...] = ()
    """Attributes present but not readable statically, e.g. a computed list."""

    @property
    def key(self) -> tuple[str, str]:
        return (self.app, self.name)

    @property
    def is_squash(self) -> bool:
        return bool(self.replaces)

    @property
    def number(self) -> int | None:
        """The leading migration number, when the file follows the convention."""
        head = self.name.split("_", 1)[0]
        return int(head) if head.isdigit() else None

    def __str__(self) -> str:
        return f"{self.app}.{self.name}"


def classify(name: str) -> OperationKind:
    """The kind of one operation class name.

    Unknown names are ``UNKNOWN`` rather than a guess. Third-party operations
    are common -- ``django_postgres_extra``, ``psqlextra`` and NetBox's own all
    ship some -- and calling an unrecognised one ``STATE`` would let a rule
    conclude a migration emits no DDL when nobody checked.
    """
    return _KIND_BY_OPERATION.get(name, OperationKind.UNKNOWN)
