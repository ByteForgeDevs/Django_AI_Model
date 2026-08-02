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
