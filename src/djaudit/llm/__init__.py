"""The language model layer: a consumer of findings, never a producer of them.

Everything under this package reads findings that a deterministic rule already
produced and adds opinion to them -- an ordering, an explanation, a proposed
patch. Nothing here can add a finding to a run or take one away. That is not a
convention this package follows; it is a shape it has. See ``provider`` for the
mechanism and ``docs/architecture/llm-layer.md`` for the argument.
"""

from djaudit.llm.provider import (
    Answer,
    Declined,
    Field,
    FieldKind,
    NullProvider,
    Prompt,
    Provider,
    Reply,
    ResponseSchema,
    SchemaViolationError,
    Usage,
)

__all__ = [
    "Answer",
    "Declined",
    "Field",
    "FieldKind",
    "NullProvider",
    "Prompt",
    "Provider",
    "Reply",
    "ResponseSchema",
    "SchemaViolationError",
    "Usage",
]
