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
from djaudit.llm.edit import (
    DIFF_CONTEXT,
    Edit,
    EditError,
    apply,
    diff,
    line_starts,
    offset_of,
    replace_value,
    span,
    touched_lines,
    verified_span,
)
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
from djaudit.llm.explain import Explanation, FingerprintError, explain, find
from djaudit.llm.group import Theme, collapsed, group
from djaudit.llm.impact import Impact, blast_radius, framing, impact, no_invented_numbers
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
    "DIFF_CONTEXT",
    "MINIMUM_OBSERVATIONS",
    "TRIAGE_SCHEMA",
    "Answer",
    "Budget",
    "Cache",
    "Cached",
    "Credential",
    "Declined",
    "Edit",
    "EditError",
    "Explanation",
    "Field",
    "FieldKind",
    "FingerprintError",
    "Impact",
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
    "Theme",
    "TriageRun",
    "Triager",
    "Usage",
    "Verdict",
    "apply",
    "askable_rules",
    "baselines",
    "blast_radius",
    "build_triage_prompt",
    "check_grounds",
    "collapsed",
    "comment_for",
    "contested_rules",
    "diff",
    "explain",
    "find",
    "framing",
    "group",
    "headroom",
    "impact",
    "key_for",
    "line_starts",
    "load_ground_truth",
    "no_invented_numbers",
    "offset_of",
    "propose",
    "redact",
    "render_finding",
    "replace_value",
    "score",
    "source_line_of",
    "span",
    "suggest",
    "touched_lines",
    "triage",
    "verified_span",
    "worth_asking",
]
