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
