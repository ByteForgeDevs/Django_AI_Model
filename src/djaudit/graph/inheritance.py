"""Inheritance that crosses module boundaries.

Almost no real Django project declares its models against `models.Model`
directly. NetBox has a `NetBoxModel`/`PrimaryModel` hierarchy layered over a
dozen feature mixins, and reading only what one file says leaves two thirds of
its models invisible and the rest missing the `created` and `last_updated`
columns every one of them has. A graph that cannot see those is not wrong at
the edges — it is wrong about which models exist.

Resolving it means doing by hand the part of an import Python would do for us:
turn a base class name into a dotted path through the module's own imports,
find the file that defines it, and repeat. Two things make that survivable —
modules are parsed only when something actually refers to them, and a class
that still cannot be found is recorded rather than assumed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from djaudit.astutils import import_bindings, resolve_dotted, star_imports

if TYPE_CHECKING:
    from djaudit.context import ProjectContext

DJANGO_MODEL_PATHS = frozenset(
    {
        "django.db.models.Model",
        "django.db.models.base.Model",
    }
)

MAX_DEPTH = 24
"""Guard against a base chain that never terminates. Real hierarchies are a
handful deep; anything longer is a cycle we failed to detect or a project
doing something we should decline to reason about rather than hang on."""


def package_dotted(path: Path) -> str:
    """The dotted module name Python would import this file as.

    Derived by walking up while ``__init__.py`` keeps existing, which is how
    the interpreter decides the same thing. The project root is the wrong
    anchor: NetBox's code sits in ``<root>/netbox/netbox/models/``, which the
    root makes ``netbox.netbox.models`` while every import in the project says
    ``netbox.models``.
    """
    parts = [path.parent.name if path.stem == "__init__" else path.stem]
    directory = path.parent
    if path.stem == "__init__":
        directory = directory.parent
    while (directory / "__init__.py").exists():
        parts.append(directory.name)
        directory = directory.parent
    return ".".join(reversed(parts))


@dataclass(slots=True)
class ClassRecord:
    """One class definition anywhere in the project."""

    name: str
    module: str
    path: Path
    node: ast.ClassDef
    bindings: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.name}"

    @property
    def bases(self) -> tuple[str, ...]:
        from djaudit.graph.builder import base_names  # noqa: PLC0415  (cycle)

        return tuple(base_names(self.node))


class ClassIndex:
    """Every class in the project, found by dotted path and parsed on demand.

    Parsing is lazy because the alternative is reading all 1213 of NetBox's
    files to answer questions about the handful that define base classes. A
    module is read the first time a name points into it and never again.
    """

    def __init__(self, ctx: ProjectContext) -> None:
        self.ctx = ctx
        self._modules: dict[str, Path] = {}
        self._classes: dict[str, dict[str, ClassRecord]] = {}
        self._bindings: dict[str, dict[str, str]] = {}
        self._stars: dict[str, tuple[str, ...]] = {}
        self._packages: set[str] = set()
        self._inherit_cache: dict[tuple[str, int], bool] = {}
        for path in ctx.python_files:
            dotted = package_dotted(path)
            if dotted not in self._modules:
                self._modules[dotted] = path
                if path.stem == "__init__":
                    self._packages.add(dotted)

    def _load(self, module: str) -> dict[str, ClassRecord]:
        cached = self._classes.get(module)
        if cached is not None:
            return cached
        found: dict[str, ClassRecord] = {}
        path = self._modules.get(module)
        tree = self.ctx.parse(path) if path is not None else None
        if path is not None and tree is not None:
            self._bindings[module] = import_bindings(tree)
            self._stars[module] = tuple(
                self._absolute("." * star.level + star.module, module)
                if star.level
                else star.module
                for star in star_imports(tree)
            )
            for node in class_defs(tree.body):
                found[node.name] = ClassRecord(
                    name=node.name,
                    module=module,
                    path=path,
                    node=node,
                    bindings=self._bindings[module],
                )
        self._classes[module] = found
        return found

    def _absolute(self, dotted: str, module: str) -> str:
        return absolute(dotted, module, is_package=module in self._packages)

    def bindings_for(self, module: str) -> dict[str, str]:
        self._load(module)
        return self._bindings.get(module, {})

    def lookup(self, dotted: str, *, depth: int = 0) -> ClassRecord | None:
        """The class a dotted path names, following re-exports.

        ``from netbox.models import PrimaryModel`` resolves to
        ``netbox.models.PrimaryModel``, and that name may be defined in the
        package's ``__init__`` or merely imported into it from a submodule.
        Both are how a project presents a stable public name, so both have to
        lead to the definition.
        """
        module, _, name = dotted.rpartition(".")
        if not module or depth > MAX_DEPTH:
            return None
        found = self._load(module).get(name)
        if found is not None:
            return found
        forwarded = self.bindings_for(module).get(name)
        if forwarded is not None and forwarded.lstrip(".") != dotted.lstrip("."):
            return self.lookup(self._absolute(forwarded, module), depth=depth + 1)
        # NetBox's netbox/models/__init__.py is nothing but re-exports, half of
        # them "from netbox.models.features import *". A star import binds no
        # name we can see, so the only way to know whether it supplies this one
        # is to look in the module it names.
        for starred in self._stars.get(module, ()):
            through = self.lookup(f"{starred}.{name}", depth=depth + 1)
            if through is not None:
                return through
        return None

    def base_target(self, record: ClassRecord, base: str) -> str:
        """The dotted path a base class name refers to, from where it is written."""
        resolved = resolve_dotted(record.bindings, base)
        if "." not in resolved or resolved.startswith("."):
            resolved = self._absolute(resolved, record.module)
        if resolved == base and base in self._load(record.module):
            return f"{record.module}.{base}"
        return resolved

    def is_model(self, record: ClassRecord) -> bool:
        """Whether this class ends up inheriting from ``django.db.models.Model``.

        The question a mixin makes interesting: ``TrackingModelMixin`` is a
        plain object and contributes nothing, while ``ChangeLoggingMixin`` two
        names along in the same base list is abstract and brings two columns
        with it. Only following the chain tells them apart.
        """
        return self.inherits(record, DJANGO_MODEL_PATHS)

    def inherits(self, record: ClassRecord, targets: frozenset[str], *, depth: int = 0) -> bool:
        """Whether any ancestor of ``record`` is one of ``targets``."""
        key = (record.dotted, id(targets))
        cached = self._inherit_cache.get(key)
        if cached is not None:
            return cached
        if depth > MAX_DEPTH:
            return False
        # Provisional False stops a cycle from recursing; a real answer
        # overwrites it below.
        self._inherit_cache[key] = False
        verdict = False
        for base in record.bases:
            target = self.base_target(record, base)
            if target in targets:
                verdict = True
                break
            parent = self.lookup(target)
            if parent is not None and self.inherits(parent, targets, depth=depth + 1):
                verdict = True
                break
        self._inherit_cache[key] = verdict
        return verdict

    def ancestry(self, record: ClassRecord) -> tuple[ClassRecord, ...]:
        """Ancestors in Python's resolution order, nearest first, self excluded.

        A depth-first left-to-right walk with duplicates removed, which is what
        C3 linearisation reduces to for the single-inheritance-plus-mixins
        shape Django models actually use. Order decides which declaration wins
        when two bases define the same field, so it is not incidental.
        """
        seen: set[str] = {record.dotted}
        out: list[ClassRecord] = []

        def walk(node: ClassRecord, depth: int) -> None:
            if depth > MAX_DEPTH:
                return
            for base in node.bases:
                parent = self.lookup(self.base_target(node, base))
                if parent is None or parent.dotted in seen:
                    continue
                seen.add(parent.dotted)
                out.append(parent)
                walk(parent, depth + 1)

        walk(record, 0)
        return tuple(out)

    def unresolved_bases(self, record: ClassRecord) -> tuple[str, ...]:
        """Base names that lead neither to Django's ``Model`` nor to a class we found."""
        missing = []
        for base in record.bases:
            target = self.base_target(record, base)
            if target in DJANGO_MODEL_PATHS or self.lookup(target) is not None:
                continue
            missing.append(base)
        return tuple(missing)


def absolute(dotted: str, module: str, *, is_package: bool = False) -> str:
    """Resolve a relative import against the module it was written in.

    One leading dot means the package the writing module lives *in*, and each
    further dot one level above that. Whether the writer is itself a package
    changes the answer by a level: ``from .device_components import X`` inside
    ``dcim.models.power`` means ``dcim.models.device_components``, while the
    same line inside ``dcim/models/__init__.py`` -- which *is* ``dcim.models``
    -- means the same thing while starting one component shorter.
    """
    if not dotted.startswith("."):
        return dotted if "." in dotted else f"{module}.{dotted}"
    level = len(dotted) - len(dotted.lstrip("."))
    drop = level - 1 if is_package else level
    parts = module.split(".")
    base = parts[: max(len(parts) - drop, 0)]
    return ".".join([*base, dotted.lstrip(".")])


def class_defs(body: list[ast.stmt]) -> list[ast.ClassDef]:
    """Top-level classes, plus those inside ``if``/``try`` blocks.

    Conditionally defined models are rare but real -- a model guarded by a
    feature flag or an optional dependency still creates a table when the
    branch is taken.
    """
    out: list[ast.ClassDef] = []
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            out.append(stmt)
        elif isinstance(stmt, ast.If):
            out.extend(class_defs(stmt.body))
            out.extend(class_defs(stmt.orelse))
        elif isinstance(stmt, ast.Try):
            out.extend(class_defs(stmt.body))
            for handler in stmt.handlers:
                out.extend(class_defs(handler.body))
            out.extend(class_defs(stmt.orelse))
    return out
