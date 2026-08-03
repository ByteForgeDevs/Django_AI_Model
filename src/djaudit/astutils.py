"""Small AST helpers shared by rules.

Deliberately conservative: when a value cannot be determined statically these
helpers return :data:`UNKNOWN` rather than guessing. Rules turn ``UNKNOWN`` into
a lower confidence or into silence, which is how we keep the false positive rate
low enough for people to leave the tool switched on.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Final


class _Unknown:
    """Sentinel for a value that cannot be resolved by static analysis."""

    _instance: _Unknown | None = None

    def __new__(cls) -> _Unknown:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        return False


UNKNOWN: Final = _Unknown()


@dataclass(frozen=True, slots=True)
class Assignment:
    """A module-level name binding.

    ``conditional`` marks assignments nested inside ``if``/``try`` blocks. Those
    may never execute, so rules should generally report them with reduced
    confidence rather than as certainties.
    """

    name: str
    value: ast.expr
    node: ast.stmt
    conditional: bool = False


def module_assignments(tree: ast.Module) -> list[Assignment]:
    """Collect module-level assignments, descending into conditional blocks.

    Django settings modules routinely wrap assignments in ``if DEBUG:`` or
    ``try/except ImportError``, so ignoring nested blocks would miss most real
    configuration.
    """
    found: list[Assignment] = []

    def visit(body: list[ast.stmt], conditional: bool) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        found.append(Assignment(target.id, stmt.value, stmt, conditional))
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                    found.append(Assignment(stmt.target.id, stmt.value, stmt, conditional))
            elif isinstance(stmt, ast.If):
                visit(stmt.body, True)
                visit(stmt.orelse, True)
            elif isinstance(stmt, ast.Try):
                visit(stmt.body, True)
                for handler in stmt.handlers:
                    visit(handler.body, True)
                visit(stmt.orelse, True)
                visit(stmt.finalbody, conditional)
            elif isinstance(stmt, ast.With):
                visit(stmt.body, conditional)

    visit(tree.body, False)
    return found


def literal(node: ast.expr | None) -> Any:
    """Statically evaluate a literal expression, or return :data:`UNKNOWN`."""
    if node is None:
        return UNKNOWN
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return UNKNOWN


def dotted_name(node: ast.expr) -> str | None:
    """Render ``a.b.c`` from a Name/Attribute chain, or ``None`` if it is neither."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


@dataclass(frozen=True, slots=True)
class StarImport:
    """A ``from x import *``, keeping the relative level.

    ``from .base import *`` and ``from base import *`` name different modules,
    and dropping the level makes a split-settings layout unresolvable.
    """

    module: str
    level: int = 0

    @property
    def is_relative(self) -> bool:
        return self.level > 0


def star_imports(tree: ast.Module) -> list[StarImport]:
    """Every ``from x import *`` in source order, with relative levels intact."""
    return [
        StarImport(module=node.module or "", level=node.level)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
    ]


def star_import_targets(tree: ast.Module) -> list[str]:
    """Modules pulled in via ``from x import *``.

    Split settings layouts rely on this, so resolving the import graph matters
    for deciding which module a setting actually takes effect in.
    """
    return [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and any(alias.name == "*" for alias in node.names)
    ]


def import_bindings(tree: ast.Module) -> dict[str, str]:
    """Map every name a module imports to the dotted path it came from.

    Recognising a Django model means recognising ``models.Model``, but the name
    ``models`` is only Django's because of an import at the top of the file, and
    plenty of projects write ``from django.db import models as db_models`` or
    ``from django.db.models import Model``. Matching on the spelling alone gives
    both false positives and false negatives; matching on what the name is bound
    to gives neither.

    A relative import keeps its dots (``from .base import Card`` binds ``Card``
    to ``.base.Card``) so a later pass can resolve it against the package it was
    written in. Star imports are not bindings and are handled by
    :func:`star_imports`.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # ``import a.b.c`` binds ``a``; ``import a.b.c as x`` binds ``x``
                # to the full path. Only the aliased form is unambiguous.
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    bindings[alias.name.partition(".")[0]] = alias.name.partition(".")[0]
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    continue
                bindings[alias.asname or alias.name] = f"{prefix}.{alias.name}"
    return bindings


def resolve_dotted(bindings: dict[str, str], name: str) -> str:
    """Expand a dotted name through a module's imports.

    ``models.Model`` with ``models`` bound to ``django.db.models`` resolves to
    ``django.db.models.Model``. A name with no binding is returned unchanged --
    it may be defined in this module, which is a different question and one the
    caller is better placed to answer.
    """
    head, dot, rest = name.partition(".")
    origin = bindings.get(head)
    if origin is None:
        return name
    return f"{origin}{dot}{rest}" if rest else origin
