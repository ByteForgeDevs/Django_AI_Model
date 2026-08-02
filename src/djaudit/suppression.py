"""Inline suppression comments.

Two syntaxes are accepted::

    DEBUG = True  # noqa: DJS-001
    DEBUG = True  # djaudit: ignore[DJS-001] staging box, tracked in PROJ-412

A bare ``# noqa`` is deliberately *not* honoured. Those are usually left behind
for some other linter, and treating them as blanket djaudit suppressions would
silently hide security findings. ``# djaudit: ignore`` without codes is honoured,
because that one can only have been written for us.

``# djaudit: ignore[]`` — an *empty* bracket list — suppresses nothing. The
brackets say "I am about to name the rules I mean", so an empty list is a typo,
not an intention. Treating it as a blanket suppression would turn a slip of the
keyboard into silently hidden findings, which is the same failure we refuse
bare ``# noqa`` to avoid.
"""

from __future__ import annotations

import re

_NOQA = re.compile(
    r"#\s*noqa\s*:\s*(?P<codes>[A-Za-z]{3}-\d{3}(?:\s*,\s*[A-Za-z]{3}-\d{3})*)",
    re.IGNORECASE,
)
_IGNORE = re.compile(
    r"#\s*djaudit\s*:\s*ignore(?:\s*\[(?P<codes>[^\]]*)\])?",
    re.IGNORECASE,
)


def _codes(raw: str | None) -> set[str] | None:
    """``None`` means "every rule"; a set means only those rule ids.

    An empty set is therefore meaningfully different from ``None``: it names no
    rules at all and so suppresses nothing.
    """
    if raw is None:
        return None
    return {code.strip().upper() for code in raw.split(",") if code.strip()}


def line_suppresses(text: str, rule_id: str) -> bool:
    """Whether a single line of source suppresses ``rule_id``."""
    target = rule_id.upper()

    for match in _IGNORE.finditer(text):
        codes = _codes(match.group("codes"))
        if codes is None or target in codes:
            return True

    for match in _NOQA.finditer(text):
        codes = _codes(match.group("codes"))
        if codes and target in codes:
            return True

    return False


def is_suppressed(lines: list[str], start: int, end: int | None, rule_id: str) -> bool:
    """Whether any line in the 1-based range ``[start, end]`` suppresses the rule.

    The whole range is checked so a comment can sit on the closing line of a
    multi-line statement, which is where people naturally put it.
    """
    if not lines or start < 1:
        return False
    last = min(end or start, len(lines))
    first = min(start, len(lines))
    return any(line_suppresses(lines[i - 1], rule_id) for i in range(first, last + 1))
