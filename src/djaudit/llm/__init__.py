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
from djaudit.llm.suggest import (
    Proposal,
    SuppressionError,
    check_grounds,
    comment_for,
    propose,
    source_line_of,
    suggest,
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
    "Proposal",
    "Provider",
    "RateLimit",
    "Reply",
    "ResponseSchema",
    "ReviewedFinding",
    "SchemaViolationError",
    "Score",
    "Source",
    "SuppressionError",
    "TriageRun",
    "Triager",
    "Usage",
    "Verdict",
    "askable_rules",
    "baselines",
    "build_triage_prompt",
    "check_grounds",
    "comment_for",
    "contested_rules",
    "headroom",
    "key_for",
    "load_ground_truth",
    "propose",
    "redact",
    "render_finding",
    "score",
    "source_line_of",
    "suggest",
    "triage",
    "worth_asking",
]
