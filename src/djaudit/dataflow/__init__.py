"""Local dataflow analysis.

Built for `DJP` (performance) and `DJI` (injection), whose rules are only
worth shipping if they can tell whether a value was already handled -- a
queryset that was prefetched, a string that was parameterised. Flagging every
attribute access in a loop would "detect" every N+1 and bury the real ones.

Deliberately local. Analysis stops at the module boundary and, within a module,
at one call hop with an explicit budget. Unbounded interprocedural analysis on a
large repository is slow and produces confident nonsense, and neither is worth
the recall it buys.
"""

from __future__ import annotations

from djaudit.dataflow.scopes import (
    ELEMENT_KINDS,
    Binding,
    BindingKind,
    Scope,
    ScopeKind,
    build_scopes,
)

__all__ = [
    "ELEMENT_KINDS",
    "Binding",
    "BindingKind",
    "Scope",
    "ScopeKind",
    "build_scopes",
]
