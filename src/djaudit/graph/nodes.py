"""What a model is, once we have read it out of the source.

These are deliberately plain records. The builder fills them in, the graph
queries read them, and rules consume both. Nothing here parses anything; the
separation is what keeps the extraction testable one concern at a time.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DJANGO_MODEL_BASES = frozenset(
    {
        "models.Model",
        "django.db.models.Model",
        "db.models.Model",
        "Model",
    }
)
"""How ``django.db.models.Model`` gets written in practice.

Bare ``Model`` is in the list because ``from django.db.models import Model`` is
legal and occasionally used. It is also the name of plenty of things that are
not Django models, so the builder only accepts it when the module imports it
from Django — the string alone is not evidence.
"""


@dataclass(slots=True)
class RelationEdge:
    """One relation, from the model that declares it to the model it names.

    Kept separate from the field because a relation has two ends and the
    reverse one is a property of the *target*. Keeping edges as their own
    objects is what lets the graph be walked in either direction.
    """

    source: str
    """Label of the model declaring the field."""

    field_name: str
    kind: str
    """``ForeignKey``, ``OneToOneField``, ``ManyToManyField``…"""

    target_ref: str | None
    """The other model exactly as written -- ``"auth.User"``, ``"Order"``,
    ``"self"`` -- or ``None`` when it came from ``settings.AUTH_USER_MODEL``
    or from an expression we could not read."""

    lineno: int
    end_lineno: int
    target: str | None = None
    """The resolved label, or ``None`` for a model we do not have. Django's own
    ``auth.User`` is the common case: real, referenced constantly, and not in
    the repository."""

    is_self: bool = False
    via_user_setting: bool = False
    """Written as ``settings.AUTH_USER_MODEL``, which is the correct way."""

    points_at_user: bool = False
    """Whether this relation reaches the project's user model, however it was
    spelled. This is the single fact every authorization rule is built on."""

    on_delete: str | None = None
    through: str | None = None

    @property
    def is_multi(self) -> bool:
        """Whether one row on this side can have many on the other."""
        return self.kind in ("ManyToManyField", "GenericRelation")

    @property
    def resolved(self) -> bool:
        return self.target is not None


@dataclass(slots=True)
class FieldNode:
    """One field declared on a model.

    The three-way distinction between *absent*, *present but unreadable* and
    *present and false* is what :attr:`unreadable` exists for. A boolean cannot
    hold it, and collapsing it is how a tool ends up reporting that a field has
    no default when what it really has is ``default=timezone.now``.
    """

    name: str
    kind: str
    """The field class, e.g. ``CharField`` — the tail of the resolved name."""

    dotted: str
    """The field class resolved through the module's imports, as far as we can."""

    lineno: int
    end_lineno: int
    is_django: bool = True
    """False for a field this project or a third-party package defines. Such a
    field still behaves like one, but we cannot know what its own arguments
    mean, so a rule may want to say less about it."""

    is_relation: bool = False

    null: bool = False
    blank: bool = False
    unique: bool = False
    db_index: bool = False
    primary_key: bool = False
    editable: bool = True
    auto_now: bool = False
    auto_now_add: bool = False

    max_length: int | None = None
    has_default: bool = False
    default: Any = None
    has_choices: bool = False
    choices: Any = None

    unreadable: tuple[str, ...] = ()
    """Keywords that were given but could not be evaluated statically.

    A rule that cares about one of these should either lower its confidence or
    stay quiet. The value stored alongside is Django's own default, so the
    field remains usable for every question that does not turn on that keyword.
    """

    args: tuple[ast.expr, ...] = field(default_factory=tuple, repr=False, compare=False)
    kwargs: dict[str, ast.expr] = field(default_factory=dict, repr=False, compare=False)
    """Raw arguments, kept so relation resolution does not re-walk the tree."""

    node: ast.Call | None = field(default=None, repr=False, compare=False)

    def knows(self, keyword: str) -> bool:
        """Whether this field's value for ``keyword`` is something we read.

        The honest guard for any rule about to make a claim that turns on one
        argument.
        """
        return keyword not in self.unreadable


@dataclass(slots=True)
class ModelNode:
    """One model class found in the source.

    ``app_label`` follows Django's own rule -- the last component of the
    application package -- unless an ``AppConfig`` overrides it, which the
    builder resolves. Together with :attr:`name` it forms the label Django uses
    in string references (``"dcim.Device"``), which is how relations are
    resolved.
    """

    name: str
    app_label: str
    path: Path
    """Absolute path of the file the class is written in."""

    lineno: int
    end_lineno: int
    bases: tuple[str, ...] = ()
    """Base classes exactly as written, so ``models.Model`` stays distinct from
    a project's own ``BaseModel``. Resolution happens later."""

    is_abstract: bool = False
    """``Meta.abstract``. An abstract model has no table and cannot be queried,
    so most rules should skip it -- but its fields are inherited, so the graph
    still has to carry it."""

    is_proxy: bool = False
    """``Meta.proxy``. Shares its parent's table; a different Python class over
    the same rows."""

    swappable: str | None = None
    """The setting name from ``Meta.swappable``, normally ``AUTH_USER_MODEL``.
    This is how Django's own ``User`` declares that a project may replace it,
    and how we recognise a custom user model without importing anything."""

    managed: bool = True
    """``Meta.managed``. ``False`` means Django does not own the table, which
    changes what a migration rule may say about it."""

    node: ast.ClassDef | None = field(default=None, repr=False, compare=False)
    """The class body, kept so later passes can re-read it without re-parsing."""

    relations: list[RelationEdge] = field(default_factory=list)
    """Relations declared on this class, in declaration order."""

    fields: dict[str, FieldNode] = field(default_factory=dict)
    """Fields declared on this class, in declaration order.

    Inherited fields are not included: they belong to the class that declared
    them, and flattening an inheritance chain needs the whole project, which is
    substep 2.1.6.
    """

    @property
    def label(self) -> str:
        """``app_label.ModelName`` — the identifier Django uses everywhere."""
        return f"{self.app_label}.{self.name}"

    @property
    def is_concrete(self) -> bool:
        """Whether this model has a table of its own.

        Abstract models have no table. Proxies share their parent's. Both are
        real classes a rule may need to reason about, and neither is somewhere
        rows live.
        """
        return not self.is_abstract and not self.is_proxy


@dataclass(slots=True)
class ModelGraph:
    """Every model in the project, indexed the way lookups actually happen.

    Django refers to models three ways -- by class, by ``"app.Model"`` and by
    bare ``"Model"`` -- and a graph that only supports one of them forces every
    caller to reimplement the other two.
    """

    models: dict[str, ModelNode] = field(default_factory=dict)
    """Keyed by ``app_label.ModelName``."""

    user_model: str = "auth.User"
    """``AUTH_USER_MODEL`` as the project configures it, or Django's default.

    Every authorization rule in this phase is a question about ownership, and
    ownership means a path to this model. Reading it from the settings rather
    than assuming ``auth.User`` is what makes the rules work on the many
    projects that swap it.
    """

    unresolved_bases: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    """Base classes we could not tie to anything, per model label.

    Usually a base imported from a third-party package. Kept rather than
    dropped because a model whose ancestry we cannot see is a model whose
    fields we may be missing, and a rule may want to say less about it.
    """

    def __len__(self) -> int:
        return len(self.models)

    def __iter__(self) -> Iterator[ModelNode]:
        return iter(self.models.values())

    def __contains__(self, label: str) -> bool:
        return label in self.models

    def get(self, ref: str, *, app_label: str | None = None) -> ModelNode | None:
        """Resolve a model reference the way Django does.

        ``"app.Model"`` is exact. A bare ``"Model"`` is looked up in
        ``app_label`` first -- which is what Django does inside an application
        -- and only then across the project, and an ambiguous bare name
        resolves to nothing rather than to a guess. Two apps with a ``Comment``
        each is a normal thing for a project to have, and picking one at random
        would put every downstream finding on the wrong model.
        """
        if "." in ref:
            return self.models.get(ref)
        if app_label is not None:
            local = self.models.get(f"{app_label}.{ref}")
            if local is not None:
                return local
        matches = [m for m in self.models.values() if m.name == ref]
        return matches[0] if len(matches) == 1 else None

    def by_app(self, app_label: str) -> list[ModelNode]:
        return sorted(
            (m for m in self.models.values() if m.app_label == app_label),
            key=lambda m: m.name,
        )

    @property
    def apps(self) -> list[str]:
        return sorted({m.app_label for m in self.models.values()})

    @property
    def concrete(self) -> list[ModelNode]:
        """Models that own a table, in label order."""
        return sorted((m for m in self.models.values() if m.is_concrete), key=lambda m: m.label)

    def add(self, model: ModelNode) -> None:
        """Insert a model, keeping the first definition of a duplicated label.

        A repository can legitimately contain two classes with the same label --
        a vendored copy, a migration test app -- and the alternative to keeping
        one is a crash on a project we were asked to audit.
        """
        self.models.setdefault(model.label, model)
