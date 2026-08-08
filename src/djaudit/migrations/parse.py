"""Read migration files without importing them.

A migration module is executable Python whose top level frequently imports the
project's models, its settings, and occasionally a database driver. Importing
one to find out whether it is safe would be the same mistake as running an
untrusted `manage.py` to find out whether it is trustworthy, so everything here
goes through `ast`.

The parser is deliberately unfussy about what it cannot read. A migration that
builds its `operations` list in a loop is rare and real; recording it as
unreadable lets a rule decline to speak rather than report on half a list.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from djaudit.astutils import dotted_name, import_bindings, literal, resolve_dotted
from djaudit.graph.fields import field_from_call
from djaudit.migrations.nodes import (
    SIGNATURES,
    Dependency,
    MigrationNode,
    Operation,
    argument,
    classify,
)

if TYPE_CHECKING:
    from djaudit.context import ProjectContext

MIGRATION_BASES = frozenset(
    {"django.db.migrations.Migration", "django.db.migrations.migration.Migration"}
)
"""How the base class resolves once import bindings are applied."""

_SWAPPABLE = frozenset(
    {
        "django.db.migrations.swappable_dependency",
        "django.db.migrations.migration.swappable_dependency",
    }
)

_MODEL_PARAMS = ("model_name", "name")
"""Where an operation puts the model it acts on, in the order Django prefers.

``AddField`` uses ``model_name``; ``CreateModel`` and ``DeleteModel`` use
``name``. Checking ``model_name`` first matters for ``RenameModel``, which has
``old_name``/``new_name`` and no plain ``name`` at all.
"""


def is_migration_file(path: Path) -> bool:
    """Whether a path is where Django would put a migration.

    Directory-based rather than content-based because the answer decides whether
    to parse at all, and ``__init__.py`` inside ``migrations/`` is a package
    marker rather than a migration.
    """
    return (
        path.suffix == ".py"
        and path.parent.name == "migrations"
        and path.name != "__init__.py"
        and not path.name.startswith("~")
    )


def migration_files(ctx: ProjectContext) -> list[Path]:
    """Every migration file in the project, in a stable order.

    Sorted so a run over the same tree produces the same graph twice: the file
    walk's order depends on the filesystem, and a migration graph that renumbers
    itself between runs would give findings that move for no reason.
    """
    return sorted(path for path in ctx.python_files if is_migration_file(path))


def _migration_class(tree: ast.Module, bindings: dict[str, str]) -> ast.ClassDef | None:
    """The ``Migration`` class in a migration module.

    Django's loader reads the module attribute called ``Migration`` and nothing
    else, so the name decides. Matching on the resolved base first looks more
    principled and is wrong: a file that defines a shared ``ProjectMigration``
    base above its real ``Migration`` would hand back the empty base class, and
    the migration would read as having no operations at all.

    The base check remains as a fallback for the reverse case -- a project whose
    ``Migration`` subclasses its own base, where the name is still ``Migration``
    but a stricter reader might not expect it to be.
    """
    fallback: ast.ClassDef | None = None
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name == "Migration":
            return node
        for base in node.bases:
            name = dotted_name(base)
            if name is not None and resolve_dotted(bindings, name) in MIGRATION_BASES:
                fallback = fallback or node
    return fallback


def _class_attr(class_node: ast.ClassDef, name: str) -> ast.expr | None:
    """The value assigned to a class attribute, or ``None``."""
    for stmt in class_node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            if isinstance(target, ast.Name) and target.id == name and stmt.value is not None:
                return stmt.value
    return None


def _dependency(node: ast.expr, bindings: dict[str, str]) -> Dependency:
    """One ``dependencies`` entry.

    Three shapes occur in the wild: a two-string tuple, a
    ``swappable_dependency`` call, and -- in projects that generate migrations
    programmatically -- something else entirely.
    """
    if isinstance(node, ast.Call):
        func = dotted_name(node.func)
        if func is not None and resolve_dotted(bindings, func) in _SWAPPABLE:
            return Dependency(app="", name="", swappable=True)
        return Dependency(app="", name="", unreadable=True)

    value = literal(node)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        app, name = value
        if isinstance(app, str) and isinstance(name, str):
            return Dependency(app=app, name=name)
    return Dependency(app="", name="", unreadable=True)


def _dependencies(
    class_node: ast.ClassDef, attr: str, bindings: dict[str, str]
) -> tuple[tuple[Dependency, ...], bool]:
    """The entries of one dependency-shaped attribute, and whether it was readable."""
    value = _class_attr(class_node, attr)
    if value is None:
        return (), True
    if not isinstance(value, (ast.List, ast.Tuple)):
        return (), False
    return tuple(_dependency(item, bindings) for item in value.elts), True


def _string_arg(op: str, call: ast.Call, param: str) -> str | None:
    """A string argument of an operation, or ``None`` if it is not readable."""
    node = argument(op, call, param)
    if node is None:
        return None
    resolved = literal(node)
    return resolved if isinstance(resolved, str) else None


def _model_name(op: str, call: ast.Call) -> str | None:
    """The model an operation acts on, lowercased as Django writes it.

    Django's own operations are read from the parameter their signature says
    holds the model, so the ones that hold none -- ``RunSQL``, ``RunPython``,
    the extension and collation operations -- yield nothing rather than their
    first argument. Reading position 0 of a ``RunSQL`` turned 78 NetBox SQL
    statements into models, each then marked untrustworthy by a replay that had
    nothing to distrust.

    A third-party operation has no signature here, and for those the looser
    reading stands: it may well act on a model, and one whose model went
    unrecognised could not be marked unreadable against that model.
    """
    signature = SIGNATURES.get(op)
    if signature is not None:
        if signature.model_param is None:
            return None
        found = _string_arg(op, call, signature.model_param)
        return found.lower() if found is not None else None

    for param in _MODEL_PARAMS:
        found = _string_arg(op, call, param)
        if found is not None:
            return found.lower()
    if call.args:
        resolved = literal(call.args[0])
        if isinstance(resolved, str):
            return resolved.lower()
    return None


def _run_python_details(call: ast.Call, keywords: dict[str, ast.expr]) -> tuple[bool, str | None]:
    """Whether a ``RunPython`` has a reverse, and the name of its forward half.

    ``RunPython.noop`` counts as a reverse: it declares that undoing the
    migration requires no work, which is a decision somebody made, unlike an
    absent argument which is a decision nobody made.
    """
    reverse = "reverse_code" in keywords or len(call.args) > 1
    forward = argument("RunPython", call, "code")
    return reverse, dotted_name(forward) if forward is not None else None


def _run_sql_details(call: ast.Call, keywords: dict[str, ast.expr]) -> tuple[bool, str | None]:
    """Whether a ``RunSQL`` has a reverse, and its forward SQL if readable."""
    reverse = "reverse_sql" in keywords or len(call.args) > 1
    forward = argument("RunSQL", call, "sql")
    if forward is None:
        return reverse, None
    resolved = literal(forward)
    if isinstance(resolved, str):
        return reverse, resolved
    if isinstance(resolved, (list, tuple)):
        parts = [item for item in resolved if isinstance(item, str)]
        return reverse, "\n".join(parts) if parts else None
    return reverse, None


def _operation(node: ast.expr, bindings: dict[str, str]) -> Operation | None:
    """One ``operations`` entry, or ``None`` if it is not a call at all."""
    if not isinstance(node, ast.Call):
        return None
    dotted = dotted_name(node.func)
    if dotted is None:
        return None
    name = dotted.rpartition(".")[2] or dotted
    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}

    field_node = None
    declared = argument(name, node, "field")
    if isinstance(declared, ast.Call):
        # Positional trust: Django accepts only a field instance under
        # ``field``, so the ``*Field`` naming convention is not needed here
        # and would only drop the third-party ones.
        field_node = field_from_call(
            _string_arg(name, node, "name") or "",
            declared,
            bindings,
            assume_field=True,
        )

    reverse: bool | None = None
    sql: str | None = None
    callable_name: str | None = None
    if name == "RunPython":
        reverse, callable_name = _run_python_details(node, keywords)
    elif name == "RunSQL":
        reverse, sql = _run_sql_details(node, keywords)

    inner: tuple[Operation, ...] = ()
    if name == "SeparateDatabaseAndState":
        inner = _inner_operations(node, keywords, bindings)

    field_name = None
    if name in {"AddField", "RemoveField", "AlterField"}:
        field_name = _string_arg(name, node, "name")

    return Operation(
        name=name,
        kind=classify(name),
        lineno=node.lineno,
        end_lineno=node.end_lineno or node.lineno,
        model_name=_model_name(name, node),
        field_name=field_name,
        old_name=_string_arg(name, node, "old_name"),
        new_name=_string_arg(name, node, "new_name"),
        field=field_node,
        reverse=reverse,
        sql=sql,
        callable_name=callable_name,
        inner=inner,
        kwargs=keywords,
        node=node,
    )


def _inner_operations(
    call: ast.Call, keywords: dict[str, ast.expr], bindings: dict[str, str]
) -> tuple[Operation, ...]:
    """The ``database_operations`` half of a ``SeparateDatabaseAndState``.

    Only the database half: the state half is by construction free of DDL, and
    including it would have every lock rule report on operations that never
    reach the database.
    """
    inner = keywords.get("database_operations")
    if inner is None and call.args:
        inner = call.args[0]
    if not isinstance(inner, (ast.List, ast.Tuple)):
        return ()
    parsed = (_operation(item, bindings) for item in inner.elts)
    return tuple(op for op in parsed if op is not None)


def parse_migration(path: Path, app: str, ctx: ProjectContext) -> MigrationNode | None:
    """One migration file as a :class:`MigrationNode`, or ``None`` if unparseable."""
    tree = ctx.parse(path)
    if tree is None:
        return None
    bindings = import_bindings(tree)
    class_node = _migration_class(tree, bindings)
    if class_node is None:
        return None

    unreadable: list[str] = []
    dependencies, ok = _dependencies(class_node, "dependencies", bindings)
    if not ok:
        unreadable.append("dependencies")
    run_before, ok = _dependencies(class_node, "run_before", bindings)
    if not ok:
        unreadable.append("run_before")
    replaces, ok = _dependencies(class_node, "replaces", bindings)
    if not ok:
        unreadable.append("replaces")

    operations: tuple[Operation, ...] = ()
    ops_node = _class_attr(class_node, "operations")
    if isinstance(ops_node, (ast.List, ast.Tuple)):
        parsed = (_operation(item, bindings) for item in ops_node.elts)
        operations = tuple(op for op in parsed if op is not None)
        if len(operations) != len(ops_node.elts):
            unreadable.append("operations")
    elif ops_node is not None:
        unreadable.append("operations")

    atomic_node = _class_attr(class_node, "atomic")
    atomic_value = literal(atomic_node) if atomic_node is not None else None
    if atomic_node is not None and not isinstance(atomic_value, bool):
        unreadable.append("atomic")

    initial_node = _class_attr(class_node, "initial")
    initial_value = literal(initial_node) if initial_node is not None else None

    return MigrationNode(
        app=app,
        name=path.stem,
        path=path,
        dependencies=dependencies,
        operations=operations,
        run_before=run_before,
        replaces=replaces,
        atomic=atomic_value if isinstance(atomic_value, bool) else True,
        initial=initial_value if isinstance(initial_value, bool) else None,
        unreadable=tuple(unreadable),
    )
