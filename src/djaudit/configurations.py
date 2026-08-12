"""Settings declared in a class body rather than at module level.

`django-configurations`_ is the best-known way to write them, replacing the
module-of-constants convention with a class per environment::

    class Base(Configuration):
        SECRET_KEY = values.SecretValue()

    class Prod(Base):
        DEBUG = False

Two things make this shape tractable without a parallel resolver.

The first is that class inheritance here is *the same problem* as the
``from .base import *`` chain djaudit already resolves: an ordered sequence of
bodies where later assignments beat earlier ones. Threading one scope through
the linearised bases, base first, reproduces the override semantics exactly,
with no precedence logic of its own to get wrong.

The second is that a class per environment is the same problem as a *module*
per environment. ``Dev`` and ``Prod`` are ``dev.py`` and ``prod.py``, so each
class becomes its own settings module with a role inferred from its name, and
the existing severity grading -- which already knows that ``DEBUG = True`` is
correct in development and critical in production -- applies unchanged.

The library is not the only way in, and it is not how this support is
recognised. readthedocs.org -- the project this module exists for -- hand-rolls
the same idea: a ``Settings`` class whose ``load_settings(cls, module_name)``
classmethod copies every ``member.isupper()`` attribute onto the named module,
which is what the library's metaclass does under a different name. Matching the
library by base class would have left that project exactly as unreadable as
before, so what is matched is the *shape*: a module-level call handing a class
this module's ``__name__`` is direct evidence the class becomes this settings
module, and covers every project that rolled its own.

.. _django-configurations: https://django-configurations.readthedocs.io/
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from djaudit.astutils import import_bindings
from djaudit.evaluator import Scope, collect_imports

CONFIGURATION_BASES = frozenset(
    {
        "configurations.Configuration",
        "configurations.base.Configuration",
        # Shipped subclasses users inherit from directly.
        "configurations.sphinx.SphinxConfiguration",
        "configurations.SphinxConfiguration",
    }
)
"""Import paths whose subclasses are settings classes."""

SETTINGS_MODULE_ENV = "DJANGO_SETTINGS_MODULE"
CONFIGURATION_ENV = "DJANGO_CONFIGURATION"
"""The variable that picks the class at runtime, so it cannot be read here."""


@dataclass(frozen=True, slots=True)
class ConfigurationClass:
    """One ``Configuration`` subclass found in a settings module."""

    name: str
    node: ast.ClassDef
    bases: tuple[str, ...] = ()
    """Names of bases that are themselves configuration classes, in source order."""

    inherited: bool = False
    """True when another class in the same module derives from this one."""

    defines_settings: bool = True
    """True when this class, or one it inherits from, assigns a real setting.

    A loader class like readthedocs.org's ``Settings`` is a settings class by
    inheritance and holds nothing at all. Treating it as its own settings
    module reports every security setting Django expects as missing, which is
    two false positives per empty base class.
    """


@dataclass(frozen=True, slots=True)
class ConfigurationModule:
    """Every configuration class in one file, in source order."""

    classes: dict[str, ConfigurationClass] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.classes)

    def chain(self, name: str) -> list[ast.ClassDef]:
        """The class bodies to apply for ``name``, base first.

        ``ConfigurationBase.__new__`` folds the bases in reverse order and then
        lets the class's own attributes win, which is to say the leftmost base
        beats those to its right and the subclass beats all of them. Emitting
        the reversed bases first and the class itself last reproduces that
        with a plain sequential overwrite.
        """
        order: list[ast.ClassDef] = []
        seen: set[str] = set()

        def walk(current: str) -> None:
            if current in seen or current not in self.classes:
                return
            seen.add(current)
            entry = self.classes[current]
            for base in reversed(entry.bases):
                walk(base)
            order.append(entry.node)

        walk(name)
        return order

    @property
    def leaves(self) -> list[str]:
        """Classes nothing else derives from -- the ones actually deployed."""
        return [name for name, entry in self.classes.items() if not entry.inherited]


def build_index(
    trees: dict[Path, ast.Module],
    resolve: Callable[[Path, str], Path | None],
    markers: frozenset[str] = frozenset(),
) -> dict[Path, ConfigurationModule]:
    """Find every ``Configuration`` subclass across a set of files.

    A single-file pass cannot do this. The documented split-settings layout
    puts ``class Base(Configuration)`` in ``base.py`` and
    ``class Prod(Base)`` in ``prod.py``, where ``Base`` is nothing but an
    imported name -- so whether ``Prod`` is a settings class is not decidable
    from ``prod.py`` alone. Marking what is known and re-running until nothing
    new is marked settles it in the general case, including chains several
    files deep, and terminates because the marked set only ever grows.
    """
    tables = {path: _class_table(tree) for path, tree in trees.items()}
    applied = {path: applied_classes(tree) for path, tree in trees.items()}
    bindings = {path: import_bindings(tree) for path, tree in trees.items()}
    imports = {path: collect_imports(tree) for path, tree in trees.items()}

    known: set[tuple[Path, str]] = set()
    bases: dict[tuple[Path, str], list[tuple[Path, str]]] = {}

    def locate(path: Path, name: str) -> tuple[Path, str] | None:
        """The file and class name a base refers to, following one import."""
        if name in tables.get(path, {}):
            return path, name
        origin = bindings.get(path, {}).get(name)
        if origin is None or "." not in origin:
            return None
        module_name, _, class_name = origin.rpartition(".")
        target = resolve(path, module_name)
        # Only into files that were actually parsed: a base in a module outside
        # this set has no class table here, and naming it would produce a key
        # the index cannot look up.
        if target is None or class_name not in tables.get(target, {}):
            return None
        return target, class_name

    for path, table in tables.items():
        for name, node in table.items():
            if name in applied[path]:
                known.add((path, name))
            resolved: list[tuple[Path, str]] = []
            for base in node.bases:
                if Scope(imports=imports[path]).origin(base) in CONFIGURATION_BASES:
                    known.add((path, name))
                elif isinstance(base, ast.Name):
                    found = locate(path, base.id)
                    if found is not None:
                        resolved.append(found)
            bases[path, name] = resolved

    # Two directions, because the two shapes seed opposite ends of the chain.
    # A `Configuration` subclass is known through its *base*, so knowledge
    # flows down to the leaves. A class applied by a loader is known at the
    # leaf, and its bases hold the defaults it inherits, so knowledge also has
    # to flow up -- otherwise the chain stops at the first inherited class and
    # the subclass is read against no defaults at all.
    changed = True
    while changed:
        changed = False
        for key, parents in bases.items():
            if key in known:
                for parent in parents:
                    if parent not in known:
                        known.add(parent)
                        changed = True
            elif any(parent in known for parent in parents):
                known.add(key)
                changed = True

    inherited = {parent for key, parents in bases.items() if key in known for parent in parents}

    # A class counts as defining settings if it or anything it inherits from
    # does, so `class Prod(Base): pass` still qualifies while an empty loader
    # base at the top of the chain does not.
    own = {key: _assigns(tables[key[0]][key[1]], markers) for key in known}
    defines = dict(own)
    changed = True
    while changed:
        changed = False
        for key in known:
            if not defines[key] and any(defines.get(parent) for parent in bases.get(key, ())):
                defines[key] = True
                changed = True
    index: dict[Path, ConfigurationModule] = {}
    for path, name in sorted(known, key=lambda k: (str(k[0]), k[1])):
        entry = ConfigurationClass(
            name=name,
            node=tables[path][name],
            bases=tuple(
                base for base in _local_base_names(tables[path][name]) if (path, base) in known
            ),
            inherited=(path, name) in inherited,
            defines_settings=defines[path, name],
        )
        index.setdefault(path, ConfigurationModule({})).classes[name] = entry
    return index


def _assigns(node: ast.ClassDef, markers: frozenset[str]) -> bool:
    """Whether a class body assigns any name the caller recognises as a setting."""
    if not markers:
        return True
    for stmt in node.body:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        if any(isinstance(t, ast.Name) and t.id in markers for t in targets):
            return True
    return False


def applied_classes(tree: ast.Module) -> set[str]:
    """Classes this module hands its own name to, at module level.

    ``BuildDevSettings.load_settings(__name__)`` is readthedocs.org's own
    class-settings loader, and there are others: the pattern is a classmethod
    that copies every uppercase attribute onto the named module, which is
    exactly what ``django-configurations`` does with a metaclass instead. The
    call *is* the evidence -- passing ``__name__`` is a statement that this
    class becomes this settings module, and nothing else in a settings file
    looks like it. Matching the shape rather than the method name is what lets
    one rule cover every project that rolled its own.
    """
    found: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        # Module level only, and through the branches a settings file wraps
        # around it. The same call inside a function is not evidence: the
        # function need never run, and its ``__name__`` may not be this one.
        for stmt in body:
            if isinstance(stmt, ast.If):
                visit(stmt.body)
                visit(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                visit(stmt.body)
                visit(stmt.orelse)
                visit(stmt.finalbody)
                for handler in stmt.handlers:
                    visit(handler.body)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if not isinstance(call.func, ast.Attribute):
                    continue
                receiver = call.func.value
                if not isinstance(receiver, ast.Name):
                    continue
                if any(isinstance(a, ast.Name) and a.id == "__name__" for a in call.args):
                    found.add(receiver.id)

    visit(tree.body)
    return found


def _class_table(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _local_base_names(node: ast.ClassDef) -> list[str]:
    return [base.id for base in node.bases if isinstance(base, ast.Name)]


def find_configuration_classes(tree: ast.Module, scope: Scope | None = None) -> ConfigurationModule:
    """Locate every ``Configuration`` subclass at module level.

    Inheritance is followed transitively, so ``class Dev(Base)`` is recognised
    through ``class Base(Configuration)`` without naming ``Configuration``
    itself.
    """
    resolver = scope if scope is not None else Scope(imports=collect_imports(tree))
    classes: dict[str, ConfigurationClass] = {}
    inherited: set[str] = set()

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        local_bases: list[str] = []
        rooted = False
        for base in node.bases:
            if resolver.origin(base) in CONFIGURATION_BASES:
                rooted = True
            elif isinstance(base, ast.Name) and base.id in classes:
                local_bases.append(base.id)
                inherited.add(base.id)
        if rooted or local_bases:
            classes[node.name] = ConfigurationClass(node.name, node, tuple(local_bases))

    return ConfigurationModule(
        {
            name: ConfigurationClass(name, entry.node, entry.bases, name in inherited)
            for name, entry in classes.items()
        }
    )


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def class_name_tokens(name: str) -> set[str]:
    """Split a class name into role keywords.

    ``ProductionConfig`` has to yield ``production``, and lowercasing the whole
    name would bury it in ``productionconfig``. Splitting on the camel-case
    boundary first is what lets the existing keyword table read class names at
    all.
    """
    return {
        part
        for part in re.split(r"[_\-.]", _CAMEL.sub(" ", name).lower().replace(" ", "_"))
        if part
    }
