"""Partially evaluated expression results.

A static analyser reading Django settings meets three situations, and conflating
any two of them produces a bad rule:

* the value is knowable -- ``DEBUG = True``
* the value is not knowable -- ``SECRET_KEY = vault.fetch("prod")``
* the value depends on something we can enumerate -- ``DEBUG = env.bool(...)``,
  which is one thing when the variable is set and another when it is not

Returning ``None`` for the second case is the trap: ``None`` is also a perfectly
good settings value, so "we could not tell" and "it is None" become the same
answer and every rule downstream inherits the ambiguity. ``Value`` makes the
distinction explicit and forces rules to handle it.

The two questions a rule actually asks are :meth:`Value.is_always` ("must this be
X?") and :meth:`Value.could_be` ("might this be X?"). They map directly onto
confidence: something that must be wrong is reportable with certainty, something
that might be wrong is `tentative`. Nothing else about the shape of a value
should reach a rule.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class ValueKind(StrEnum):
    LITERAL = "literal"
    """Resolved to a concrete Python object."""

    UNKNOWN = "unknown"
    """Not resolvable statically. Carries a reason, for evidence and debugging."""

    CONDITIONAL = "conditional"
    """Depends on a branch we cannot take, but the possibilities are enumerable."""


@dataclass(frozen=True, slots=True)
class Value:
    """One partially evaluated expression.

    ``env_dependent`` is tracked separately from ``kind`` because it is not a
    third state but a taint: a value can be a perfectly concrete literal and
    still be a literal only because nobody set the environment variable. Rules
    use it to lower confidence, never to change a verdict.
    """

    kind: ValueKind
    literal: Any = None
    branches: tuple[Value, ...] = ()
    env_dependent: bool = False
    reason: str = ""
    source: str = field(default="", compare=False)

    @classmethod
    def of(cls, literal: Any, *, env_dependent: bool = False, source: str = "") -> Value:
        return cls(ValueKind.LITERAL, literal=literal, env_dependent=env_dependent, source=source)

    @classmethod
    def unknown(cls, reason: str, *, env_dependent: bool = False, source: str = "") -> Value:
        return cls(ValueKind.UNKNOWN, reason=reason, env_dependent=env_dependent, source=source)

    @classmethod
    def conditional(cls, branches: Iterable[Value], *, source: str = "") -> Value:
        """Collapse a set of possible values into one.

        Flattens nested conditionals and de-duplicates, so ``A or (B or C)``
        does not produce a tree that every rule has to walk. A single distinct
        branch is not conditional at all -- both arms of ``True if x else True``
        agree, and pretending otherwise would needlessly lower confidence.
        """
        flat: list[Value] = []
        for branch in branches:
            flat.extend(branch.branches if branch.kind is ValueKind.CONDITIONAL else [branch])

        # De-duplicated on the observable value only. Two branches that both
        # yield True are one outcome even if only one came from the
        # environment; the taint is aggregated onto the parent instead. Keeping
        # them distinct would leave a conditional whose arms agree, and every
        # rule downstream would lower its confidence for no reason.
        unique: list[Value] = []
        for value in flat:
            bare = replace(value, env_dependent=False)
            if bare not in unique:
                unique.append(bare)

        if not unique:
            return cls.unknown("no branches", source=source)
        tainted = any(v.env_dependent for v in flat)
        if len(unique) == 1:
            return replace(unique[0], env_dependent=tainted, source=source)
        return cls(
            ValueKind.CONDITIONAL,
            branches=tuple(unique),
            env_dependent=tainted,
            source=source,
        )

    @property
    def is_literal(self) -> bool:
        return self.kind is ValueKind.LITERAL

    @property
    def is_unknown(self) -> bool:
        return self.kind is ValueKind.UNKNOWN

    @property
    def is_conditional(self) -> bool:
        return self.kind is ValueKind.CONDITIONAL

    def possible(self) -> tuple[Any, ...]:
        """Every literal this could be. Empty when nothing is knowable."""
        if self.is_literal:
            return (self.literal,)
        if self.is_conditional:
            return tuple(literal for branch in self.branches for literal in branch.possible())
        return ()

    @property
    def fully_resolved(self) -> bool:
        """True when no branch is unknown, so :meth:`possible` is exhaustive."""
        if self.is_literal:
            return True
        if self.is_conditional:
            return all(branch.fully_resolved for branch in self.branches)
        return False

    def is_always(self, expected: Any) -> bool:
        """Must the value be ``expected``, on every path?

        False whenever any branch is unknown: an unenumerable branch could be
        anything, so no universal claim survives it.
        """
        if not self.fully_resolved:
            return False
        possible = self.possible()
        return bool(possible) and all(_same(actual, expected) for actual in possible)

    def could_be(self, expected: Any) -> bool:
        """Might the value be ``expected``?

        True for unknowns, deliberately. A rule asking "could this be unsafe"
        about a value it cannot see should get "yes" and report tentatively,
        rather than get "no" and stay silent.
        """
        if self.is_unknown:
            return True
        if self.is_conditional and not self.fully_resolved:
            return True
        return any(_same(actual, expected) for actual in self.possible())

    def describe(self) -> str:
        """One line for a finding's evidence."""
        if self.is_literal:
            text = repr(self.literal)
        elif self.is_conditional:
            text = " or ".join(branch.describe() for branch in self.branches)
        else:
            text = f"unknown ({self.reason})" if self.reason else "unknown"
        return f"{text} [environment-dependent]" if self.env_dependent else text


def _same(actual: Any, expected: Any) -> bool:
    """Equality that does not conflate booleans with the integers 0 and 1.

    ``DEBUG = 1`` and ``DEBUG = True`` are both truthy to Django, but a rule
    checking ``is_always(True)`` is asking a precise question and ``1 == True``
    in Python would answer it wrongly for ``ALLOWED_HOSTS = [1]``-style data.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(actual) is type(expected) and actual == expected
    return bool(actual == expected)


UNKNOWN = Value.unknown("not evaluated")
