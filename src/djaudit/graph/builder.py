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
from djaudit.graph.inherit import apply_inheritance
from djaudit.graph.inheritance import (
    ClassIndex,
    ClassRecord,
    class_defs,
    package_dotted,
)
from djaudit.graph.managers import extract_managers, implicit_manager
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
    return app_label_of_dir(app_dir_for(path), ctx)


def app_label_of_dir(app_dir: Path, ctx: ProjectContext) -> str:
    """Django's app label for an application directory.

    Split from :func:`app_label_for` because a migration lives two levels down
    (``<app>/migrations/0001_initial.py``) and ``app_dir_for`` only knows how to
    climb out of a ``models/`` package. The label has to be the same one the
    model graph uses or a migration's ``dependencies`` would point at an app
    nothing else in the run has heard of.
    """
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
        bases = base_names(node)
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


def base_names(node: ast.ClassDef) -> list[str]:
    """Base classes as written, skipping anything that is not a dotted name.

    ``Manager.from_queryset(SomeQuerySet)`` is the exception. It is a call, but
    it is also how Django composes a manager class, and NetBox subclasses the
    result four times. Reading it as no base at all leaves those classes with
    an empty ancestry and nothing to identify them by.
    """
    names = []
    for base in node.bases:
        rendered = dotted_name(base)
        if rendered is not None:
            names.append(rendered)
            continue
        composed = _composed_base(base)
        if composed is not None:
            names.append(composed)
    return names


def _composed_base(base: ast.expr) -> str | None:
    """The class ``Manager.from_queryset(...)`` produces, named by its owner."""
    if not isinstance(base, ast.Call):
        return None
    callee = dotted_name(base.func)
    if callee is None or "." not in callee:
        return None
    owner, _, tail = callee.rpartition(".")
    return owner if tail == "from_queryset" else None


def build_node(record: ClassRecord, app_label: str, index: ClassIndex) -> ModelNode:
    """A graph node for one class, from what its own body says."""
    node = record.node
    model = ModelNode(
        name=record.name,
        app_label=app_label,
        path=record.path,
        lineno=node.lineno,
        end_lineno=node.end_lineno or node.lineno,
        bases=record.bases,
        node=node,
        fields=extract_fields(node, record.bindings),
    )
    read_meta(model, meta_class(node), record.bindings)
    model.managers = extract_managers(node, record, index)
    model.relations = build_edges(model, record.bindings)
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
    index = ClassIndex(ctx)
    labels: dict[Path, str] = {}
    nodes: dict[str, ModelNode] = {}

    def label_for(path: Path) -> str:
        app_dir = app_dir_for(path)
        if app_dir not in labels:
            labels[app_dir] = app_label_for(path, ctx)
        return labels[app_dir]

    def node_for(record: ClassRecord) -> ModelNode:
        existing = nodes.get(record.dotted)
        if existing is None:
            existing = build_node(record, label_for(record.path), index)
            nodes[record.dotted] = existing
        return existing

    for path in ctx.python_files:
        if not is_model_module(path):
            continue
        tree = ctx.parse(path)
        if tree is None:
            continue
        module = package_dotted(path)
        for node in class_defs(tree.body):
            record = index.lookup(f"{module}.{node.name}")
            if record is None or record.path != path or not index.is_model(record):
                continue
            model = node_for(record)
            graph.add(model)
            missing = index.unresolved_bases(record)
            if missing:
                graph.unresolved_bases[model.label] = missing

    # Inheritance before edges are resolved: a field arriving from an abstract
    # base is a relation like any other, and it has to be in place before
    # anything asks what points at what.
    for dotted, model in list(nodes.items()):
        record = index.lookup(dotted)
        if record is None:
            continue
        # Only ancestors that are themselves models take part. A plain mixin
        # is not one: Django never contributes its attributes as fields, and
        # counting one as a concrete parent invents a table and a join.
        ancestors = tuple(a for a in index.ancestry(record) if index.is_model(a))
        apply_inheritance(model, ancestors, node_for)
        if not model.managers and not model.is_abstract:
            # ModelBase._prepare adds `objects` only when nothing was declared
            # anywhere in the MRO, so this waits until inheritance is done.
            model.managers["objects"] = implicit_manager(model.lineno)

    # Deferred until every model is known: a bare "Order" may name a model in
    # a module read after the one referring to it.
    resolve_edges(graph.models, graph.user_model, graph.incoming)
    return graph
