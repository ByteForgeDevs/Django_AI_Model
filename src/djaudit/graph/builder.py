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

from djaudit.astutils import dotted_name, import_bindings, resolve_dotted
from djaudit.graph.fields import extract_fields
from djaudit.graph.meta import class_attr_literal, meta_class, read_meta
from djaudit.graph.nodes import ModelGraph, ModelNode
from djaudit.graph.relations import DEFAULT_USER_MODEL, build_edges, resolve_edges
from djaudit.settings import resolve_all

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
        label = class_attr_literal(node, "label")
        if isinstance(label, str) and label:
            return label
        name = class_attr_literal(node, "name")
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
            fields=extract_fields(node, self.bindings),
        )
        read_meta(model, meta_class(node))
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


def resolve_user_model(ctx: ProjectContext) -> str:
    """``AUTH_USER_MODEL`` for the project, or Django's default.

    A project that swaps its user model does so in exactly one place, and every
    ownership question in Phase 2 turns on getting it right -- assume
    ``auth.User`` on a project with a custom user and every IDOR rule goes
    quiet on the one relation that mattered.

    Where several settings modules disagree, the production-reachable ones are
    read first: that is the deployment the rules are reasoning about. A value
    we cannot resolve falls back to the default rather than to nothing, because
    Django's default is what an unset setting actually means.
    """
    views = resolve_all(ctx)
    ordered = sorted(
        ctx.settings_modules,
        key=lambda m: (not m.is_entrypoint, not m.role.reaches_production, m.dotted),
    )
    for module in ordered:
        view = views.get(module.dotted)
        if view is None:
            continue
        resolved = view.get("AUTH_USER_MODEL")
        if not resolved.is_assigned:
            continue
        label = resolved.value.literal
        if isinstance(label, str) and "." in label:
            return label
    return DEFAULT_USER_MODEL


def build_model_graph(ctx: ProjectContext) -> ModelGraph:
    """Reconstruct the project's models from source."""
    graph = ModelGraph(user_model=resolve_user_model(ctx))
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
            model.relations = build_edges(model, scanner.bindings)
            graph.add(model)
        graph.unresolved_bases.update(scanner.unresolved)

    # Deferred until every model is known: a bare "Order" may name a model in
    # a module read after the one referring to it.
    resolve_edges(graph.models, graph.user_model, graph.incoming)
    return graph
