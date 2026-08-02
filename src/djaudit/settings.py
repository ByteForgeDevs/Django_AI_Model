"""Resolving what a Django setting is actually worth, and where it came from.

A rule that reads one assignment in one file gets split settings wrong. Real
projects put ``DEBUG = True`` in ``base.py`` and ``DEBUG = False`` in
``production.py``, and the answer to "is DEBUG on" depends on which module you
are asking about and what it inherits.

This module answers that question once, with provenance, so no rule has to
reimplement it -- and so a finding can say *which* assignment wins and which
were overridden.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from djaudit.astutils import StarImport, star_imports
from djaudit.context import ProjectContext, SettingsModule
from djaudit.discovery import dotted_path, resolve_dotted
from djaudit.evaluator import (
    Evaluator,
    Scope,
    collect_env_objects,
    collect_functions,
    collect_imports,
)
from djaudit.values import Value


class Origin(StrEnum):
    """Where a setting's effective value came from.

    The distinction is not cosmetic: a project that never mentions
    ``SECURE_SSL_REDIRECT`` and one that explicitly sets it to ``False`` have
    the same value and very different intent, and a good finding says which.
    """

    EXPLICIT = "explicit"
    """Assigned somewhere in the settings chain."""

    DJANGO_DEFAULT = "django_default"
    """Never assigned; Django's documented default applies."""

    UNRESOLVED = "unresolved"
    """Assigned, but the value could not be determined statically."""

    ABSENT = "absent"
    """Never assigned and we hold no default for it."""


@dataclass(frozen=True, slots=True)
class Definition:
    """One assignment of one setting in one module."""

    name: str
    value: Value
    module: Path
    dotted: str
    node: ast.stmt
    conditional: bool = False
    """Nested in ``if``/``try``, so it may not execute."""

    @property
    def line(self) -> int:
        return self.node.lineno

    def describe(self) -> str:
        where = f"{self.dotted or self.module.name}:{self.line}"
        suffix = " (conditional)" if self.conditional else ""
        return f"{where} -> {self.value.describe()}{suffix}"


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """A setting's effective value plus every assignment that contributed."""

    name: str
    value: Value
    origin: Origin
    definitions: tuple[Definition, ...] = ()
    """In resolution order. The last one wins."""

    @property
    def definition(self) -> Definition | None:
        """The assignment that determines the value, if there is one."""
        return self.definitions[-1] if self.definitions else None

    @property
    def overridden(self) -> tuple[Definition, ...]:
        """Assignments a later one replaced, oldest first."""
        return self.definitions[:-1]

    @property
    def is_explicit(self) -> bool:
        return self.origin is Origin.EXPLICIT

    @property
    def is_default(self) -> bool:
        return self.origin is Origin.DJANGO_DEFAULT

    @property
    def conditional(self) -> bool:
        """Whether the winning assignment might not execute."""
        winner = self.definition
        return winner.conditional if winner else False

    def is_always(self, expected: object) -> bool:
        return self.value.is_always(expected)

    def could_be(self, expected: object) -> bool:
        return self.value.could_be(expected)

    def describe(self) -> str:
        if self.origin is Origin.DJANGO_DEFAULT:
            return f"{self.name} = {self.value.describe()} (Django default, never set)"
        if self.origin is Origin.ABSENT:
            return f"{self.name} is not set"
        return f"{self.name} = {self.value.describe()}"

    def provenance(self) -> str:
        """A one-line override chain, for use as finding evidence."""
        if not self.definitions:
            return self.describe()
        return " | ".join(definition.describe() for definition in self.definitions)


@dataclass(frozen=True, slots=True)
class _Operation:
    """One module-level statement that changes a setting, in source order."""

    name: str
    node: ast.stmt
    conditional: bool
    value: ast.expr | None = None
    augmented: bool = False
    method: str = ""
    args: tuple[ast.expr, ...] = ()


def _operations(tree: ast.Module) -> list[_Operation]:
    """Assignments and in-place mutations of module-level names, in order.

    Settings modules build ``INSTALLED_APPS`` and ``MIDDLEWARE`` incrementally,
    so reading only ``Assign`` nodes sees the wrong list.
    """
    found: list[_Operation] = []

    def visit(body: list[ast.stmt], conditional: bool) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        found.append(_Operation(target.id, stmt, conditional, value=stmt.value))
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                    found.append(_Operation(stmt.target.id, stmt, conditional, value=stmt.value))
            elif isinstance(stmt, ast.AugAssign):
                if isinstance(stmt.target, ast.Name):
                    found.append(
                        _Operation(
                            stmt.target.id,
                            stmt,
                            conditional,
                            value=stmt.value,
                            augmented=True,
                        )
                    )
            elif isinstance(stmt, ast.Expr):
                mutation = _mutation(stmt, conditional)
                if mutation is not None:
                    found.append(mutation)
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


def _mutation(stmt: ast.Expr, conditional: bool) -> _Operation | None:
    call = stmt.value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return None
    receiver = call.func.value
    if not isinstance(receiver, ast.Name) or call.func.attr not in _LIST_MUTATIONS:
        return None
    return _Operation(
        receiver.id,
        stmt,
        conditional,
        method=call.func.attr,
        args=tuple(call.args),
    )


_LIST_MUTATIONS = frozenset({"append", "insert", "extend", "remove"})


@dataclass(frozen=True, slots=True)
class SettingsView:
    """Every setting visible from one settings module, with inheritance applied."""

    module: SettingsModule
    chain: tuple[str, ...]
    """Modules in resolution order, ending with ``module`` itself."""

    settings: dict[str, ResolvedSetting]

    def get(self, name: str) -> ResolvedSetting:
        """The setting's effective value, or ABSENT when it is never assigned."""
        found = self.settings.get(name)
        if found is not None:
            return found
        return ResolvedSetting(name, Value.unknown("setting is not assigned"), Origin.ABSENT)

    def __contains__(self, name: str) -> bool:
        return name in self.settings


def resolve_settings(ctx: ProjectContext, module: SettingsModule) -> SettingsView:
    """Resolve one settings module, following ``import *`` inheritance.

    A star import copies names into the importing module's namespace, so the
    chain really does share one namespace at runtime. Threading a single scope
    through it in source order reproduces that, which is what makes
    ``production.py`` overriding ``base.py`` come out right without any
    special-case override logic.
    """
    chain = _inheritance_chain(ctx, module)
    scope = Scope()
    definitions: dict[str, list[Definition]] = {}

    for path in chain:
        tree = ctx.parse(path)
        if tree is None:
            continue

        # A star import copies the imported module's imports, helpers and env
        # objects too, so merging them is faithful rather than approximate.
        scope.imports.update(collect_imports(tree))
        scope.functions.update(collect_functions(tree))
        scope.env_objects.update(collect_env_objects(tree, scope.imports))

        dotted = dotted_path(ctx.root, path)
        for operation in _operations(tree):
            value = _apply(operation, scope)
            if value is None:
                continue
            scope.names[operation.name] = value
            if operation.name.isupper():
                definitions.setdefault(operation.name, []).append(
                    Definition(
                        name=operation.name,
                        value=value,
                        module=path,
                        dotted=dotted,
                        node=operation.node,
                        conditional=operation.conditional,
                    )
                )

    return SettingsView(
        module=module,
        chain=tuple(dotted_path(ctx.root, path) for path in chain),
        settings={name: _resolved(name, found) for name, found in definitions.items()},
    )


def resolve_all(ctx: ProjectContext) -> dict[str, SettingsView]:
    """Resolve every discovered settings module, keyed by dotted path."""
    return {module.dotted: resolve_settings(ctx, module) for module in ctx.settings_modules}


def _resolved(name: str, definitions: list[Definition]) -> ResolvedSetting:
    winner = definitions[-1]
    origin = Origin.EXPLICIT if not winner.value.is_unknown else Origin.UNRESOLVED
    return ResolvedSetting(
        name=name,
        value=winner.value,
        origin=origin,
        definitions=tuple(definitions),
    )


def _apply(operation: _Operation, scope: Scope) -> Value | None:
    """Compute the value a single operation leaves behind."""
    evaluator = Evaluator(scope)

    if operation.method:
        return _mutate(operation, scope, evaluator)

    if operation.value is None:
        return None
    value = evaluator.evaluate(operation.value)

    if not operation.augmented:
        return value

    # MIDDLEWARE += [...] -- the previous value matters.
    current = scope.names.get(operation.name)
    if current is None or not current.is_literal or not value.is_literal:
        return Value.unknown(f"unresolvable augmented assignment to {operation.name}")
    try:
        combined = current.literal + value.literal
    except TypeError:
        return Value.unknown(f"incompatible augmented assignment to {operation.name}")
    return Value.of(combined, env_dependent=current.env_dependent or value.env_dependent)


def _mutate(operation: _Operation, scope: Scope, evaluator: Evaluator) -> Value | None:
    current = scope.names.get(operation.name)
    if current is None:
        return None
    if not current.is_literal or not isinstance(current.literal, list):
        return Value.unknown(f"mutation of an unresolved {operation.name}")

    arguments = [evaluator.evaluate(argument) for argument in operation.args]
    if not all(argument.is_literal for argument in arguments):
        return Value.unknown(f"unresolvable argument to {operation.name}.{operation.method}()")

    updated = list(current.literal)
    try:
        getattr(updated, operation.method)(*[argument.literal for argument in arguments])
    except (TypeError, ValueError, IndexError):
        return Value.unknown(f"{operation.name}.{operation.method}() failed")

    tainted = current.env_dependent or any(argument.env_dependent for argument in arguments)
    return Value.of(updated, env_dependent=tainted)


def _inheritance_chain(ctx: ProjectContext, module: SettingsModule) -> list[Path]:
    """Star-imported modules first, in source order, then the module itself."""
    ordered: list[Path] = []
    seen: set[Path] = set()

    def walk(path: Path) -> None:
        if path in seen:
            # A cycle is not valid Python at runtime either, but a half-written
            # settings package should not hang the analyser.
            return
        seen.add(path)
        tree = ctx.parse(path)
        if tree is not None:
            for star in star_imports(tree):
                target = _resolve_star(ctx, path, star)
                if target is not None:
                    walk(target)
        ordered.append(path)

    walk(module.path)
    return ordered


def _resolve_star(ctx: ProjectContext, importer: Path, star: StarImport) -> Path | None:
    if not star.is_relative:
        return resolve_dotted(ctx.root, star.module)

    # `from .base import *` is relative to the importing module's package;
    # each extra dot climbs one more level.
    package = importer.parent
    for _ in range(star.level - 1):
        package = package.parent
    if not star.module:
        return None
    relative = Path(*star.module.split("."))
    for candidate in (
        package / relative.with_suffix(".py"),
        package / relative / "__init__.py",
    ):
        if candidate.is_file():
            return candidate
    return None
