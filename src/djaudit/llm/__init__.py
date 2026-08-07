"""The language model layer: a consumer of findings, never a producer of them.

Everything under this package reads findings that a deterministic rule already
produced and adds opinion to them -- an ordering, an explanation, a proposed
patch. Nothing here can add a finding to a run or take one away. That is not a
convention this package follows; it is a shape it has. See ``provider`` for the
mechanism and ``docs/architecture/llm-layer.md`` for the argument.
"""

from djaudit.llm.budget import Budget, Metered, RateLimit
from djaudit.llm.cache import Cache, Cached, key_for
from djaudit.llm.config import Credential, LLMConfig
from djaudit.llm.evaluate import (
    ReviewedFinding,
    Score,
    Triager,
    Verdict,
    baselines,
    contested_rules,
    headroom,
    load_ground_truth,
    score,
)
from djaudit.llm.prompts import (
    TRIAGE_SCHEMA,
    build_triage_prompt,
    redact,
    render_finding,
    worth_asking,
)
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
from djaudit.llm.triage import (
    CORPUS_PRIOR,
    MINIMUM_OBSERVATIONS,
    Judgement,
    Source,
    TriageRun,
    askable_rules,
    triage,
)

__all__ = [
    "CORPUS_PRIOR",
    "MINIMUM_OBSERVATIONS",
    "TRIAGE_SCHEMA",
    "Answer",
    "Budget",
    "Cache",
    "Cached",
    "Credential",
    "Declined",
    "Field",
    "FieldKind",
    "Judgement",
    "LLMConfig",
    "Metered",
    "NullProvider",
    "Prompt",
    "Provider",
    "RateLimit",
    "Reply",
    "ResponseSchema",
    "ReviewedFinding",
    "SchemaViolationError",
    "Score",
    "Source",
    "TriageRun",
    "Triager",
    "Usage",
    "Verdict",
    "askable_rules",
    "baselines",
    "build_triage_prompt",
    "contested_rules",
    "headroom",
    "key_for",
    "load_ground_truth",
    "redact",
    "render_finding",
    "score",
    "triage",
    "worth_asking",
]
