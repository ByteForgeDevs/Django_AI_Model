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
