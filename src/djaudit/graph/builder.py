"""Read models out of the source, without importing the project.

The reason this is static rather than a `django.setup()` and a walk over
``apps.get_models()`` is that the live approach needs the target's
dependencies installed, its settings valid, and its code executed. We audit
repositories we did not write, so all three are unacceptable — and the great
majority of what an authorization rule needs is right there in the class body.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from djaudit.astutils import UNKNOWN, dotted_name, import_bindings, literal, resolve_dotted
from djaudit.graph.nodes import ModelGraph, ModelNode

if TYPE_CHECKING:
    from djaudit.context import ProjectContext

DJANGO_MODEL = "django.db.models.Model"
DJANGO_MODEL_ALIASES = frozenset({DJANGO_MODEL, "django.db.models.base.Model"})

APPCONFIG_BASES = frozenset(
    {
        "django.apps.AppConfig",
        "django.apps.config.AppConfig",
    }
)

_META_FLAGS = {
    "abstract": "is_abstract",
    "proxy": "is_proxy",
    "managed": "managed",
}


def app_dir_for(path: Path) -> Path:
    """The application package a model file belongs to.

    Django puts models in ``<app>/models.py`` or, once a project grows, in a
    ``<app>/models/`` package -- NetBox splits every app that way. Both mean the
    same app, so both have to lead to the same directory. Anything else is a
    model somewhere unusual, and the containing directory is the best available
    answer.
    """
    parent = path.parent
    if parent.name == "models" and (parent / "__init__.py").exists():
        return parent.parent
    return parent


def app_label_for(path: Path, ctx: ProjectContext) -> str:
    """Django's app label for the application containing ``path``.

    The default is the last component of the application's module path, which
    is the directory name. An ``AppConfig`` may override it, and projects with
    a name collision between two installed apps have to -- so ``apps.py`` is
    read before falling back.
    """
    app_dir = app_dir_for(path)
    declared = _appconfig_label(app_dir / "apps.py", ctx)
    return declared or app_dir.name


def _appconfig_label(apps_py: Path, ctx: ProjectContext) -> str | None:
    """``label``, or the tail of ``name``, from the first AppConfig in a file."""
    if not apps_py.exists():
        return None
    tree = ctx.parse(apps_py)
    if tree is None:
        return None

    bindings = import_bindings(tree)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = _base_names(node)
        # A project's own shared AppConfig subclass is common enough that
        # requiring a direct Django base would miss it, so a base whose name
        # ends in AppConfig counts too.
        if not any(
            resolve_dotted(bindings, base) in APPCONFIG_BASES or base.endswith("AppConfig")
            for base in bases
        ):
            continue
        label = _class_attr_literal(node, "label")
        if isinstance(label, str) and label:
            return label
        name = _class_attr_literal(node, "name")
        if isinstance(name, str) and name:
            return name.rpartition(".")[2]
    return None


def _base_names(node: ast.ClassDef) -> list[str]:
    """Base classes as written, skipping anything that is not a dotted name."""
    names = []
    for base in node.bases:
        rendered = dotted_name(base)
        if rendered is not None:
            names.append(rendered)
    return names


def _class_attr_literal(node: ast.ClassDef, attr: str) -> object:
    """A literal class attribute, or ``None`` if absent or not a literal.

    ``None`` conflates "not there" with "there but computed", which is right
    for every caller here: both mean we cannot claim a value, and the flags
    this reads all default to False in Django anyway.
    """
    for stmt in node.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = [stmt.target], stmt.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id == attr and value is not None:
                resolved = literal(value)
                return None if resolved is UNKNOWN else resolved
    return None


def meta_class(node: ast.ClassDef) -> ast.ClassDef | None:
    """The inner ``class Meta`` of a model, if it has one."""
    for stmt in node.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
            return stmt
    return None


class _ModuleScanner:
    """Finds the model classes in one module.

    Kept as a class because recognising a model is iterative: a class that
    inherits from another class *in the same module* is a model exactly when
    that one is, and a base can be defined below its heir only in the sense
    that Python would reject it -- so a single ordered pass over the module,
    accumulating what it has learned, is both correct and enough.

    Ancestry that crosses modules is deliberately not resolved here. It needs
    the whole project's import graph, which is substep 2.1.6; until then such a
    class is recorded in :attr:`unresolved` rather than silently dropped.
    """

    def __init__(self, path: Path, tree: ast.Module, app_label: str) -> None:
        self.path = path
        self.tree = tree
        self.app_label = app_label
        self.bindings = import_bindings(tree)
        self.local_models: set[str] = set()
        self.found: list[ModelNode] = []
        self.unresolved: dict[str, tuple[str, ...]] = {}

    def scan(self) -> None:
        for node in self._class_defs(self.tree.body):
            bases = tuple(_base_names(node))
            verdict, unknown = self._classify(bases)
            if not verdict:
                continue
            self.local_models.add(node.name)
            model = self._build(node, bases)
            self.found.append(model)
            if unknown:
                self.unresolved[model.label] = unknown

    def _class_defs(self, body: list[ast.stmt]) -> list[ast.ClassDef]:
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
                out.extend(self._class_defs(stmt.body))
                out.extend(self._class_defs(stmt.orelse))
            elif isinstance(stmt, ast.Try):
                out.extend(self._class_defs(stmt.body))
                for handler in stmt.handlers:
                    out.extend(self._class_defs(handler.body))
                out.extend(self._class_defs(stmt.orelse))
        return out

    def _classify(self, bases: tuple[str, ...]) -> tuple[bool, tuple[str, ...]]:
        """Is this a model, and which of its bases could we not account for?"""
        is_model = False
        unknown: list[str] = []
        for base in bases:
            if resolve_dotted(self.bindings, base) in DJANGO_MODEL_ALIASES:
                is_model = True
            elif base in self.local_models:
                # A base defined above it in this same module: a model exactly
                # when that one is. Ancestry across modules is substep 2.1.6.
                is_model = True
            elif base.split(".")[-1] in {"object", "Enum", "TextChoices", "IntegerChoices"}:
                continue
            else:
                unknown.append(base)
        return is_model, tuple(unknown)

    def _build(self, node: ast.ClassDef, bases: tuple[str, ...]) -> ModelNode:
        model = ModelNode(
            name=node.name,
            app_label=self.app_label,
            path=self.path,
            lineno=node.lineno,
            end_lineno=node.end_lineno or node.lineno,
            bases=bases,
            node=node,
        )
        meta = meta_class(node)
        if meta is not None:
            for attr, field_name in _META_FLAGS.items():
                value = _class_attr_literal(meta, attr)
                if isinstance(value, bool):
                    setattr(model, field_name, value)
            swappable = _class_attr_literal(meta, "swappable")
            if isinstance(swappable, str) and swappable:
                model.swappable = swappable
        return model


def is_model_module(path: Path) -> bool:
    """Whether a file is somewhere Django would look for models.

    Django imports each app's ``models`` module and nothing else, so a class
    defined outside one is not installed no matter what it inherits from. This
    also keeps us out of ``migrations/``, where every historical version of
    every model is written out in full and none of them is the current schema.
    """
    if "migrations" in path.parts:
        return False
    if path.name == "models.py":
        return True
    parent = path.parent
    return parent.name == "models" and (parent / "__init__.py").exists()


def build_model_graph(ctx: ProjectContext) -> ModelGraph:
    """Reconstruct the project's models from source."""
    graph = ModelGraph()
    labels: dict[Path, str] = {}

    for path in ctx.python_files:
        if not is_model_module(path):
            continue
        tree = ctx.parse(path)
        if tree is None:
            continue
        app_dir = app_dir_for(path)
        if app_dir not in labels:
            labels[app_dir] = app_label_for(path, ctx)
        scanner = _ModuleScanner(path, tree, labels[app_dir])
        scanner.scan()
        for model in scanner.found:
            graph.add(model)
        graph.unresolved_bases.update(scanner.unresolved)

    return graph
