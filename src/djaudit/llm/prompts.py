"""Turning a finding into a question, without turning it into a leak.

This is the last code a finding passes through before its bytes leave the
machine, so it is where the outbound checks belong rather than where they are
convenient.

Three things it will not send.

**A secret.** `mask` in `rules/_base.py` already blanks the value a settings
rule complains about, and it is checked here again, because that first pass
only covers the rules that know they hold a secret. A DJI rule quoting a line
that happens to contain a token knows nothing about it, and "the rule that
built this finding was careful" is not a property this module can verify. The
check is cheap and the failure is a credential in somebody else's logs.

**A reviewer's note.** The 245 verdicts in `benchmarks/` each carry prose
explaining the judgement. A prompt built from one would be a model reading the
answer, so the prompt builder takes a `Finding` -- which has no note field --
and the eval path passes findings, never `ReviewedFinding`s.

**More than it needs to.** Evidence can run to kilobytes. It is truncated to a
budget, deterministically, because the prompt is a cache key: the same finding
must render byte-identically or the cache silently stops working.

The other constraint comes from `6.1.5`. Twenty-two of twenty-nine rules were
judged the same way every time they fired, so for those the rule id already is
the verdict and a model can only introduce a disagreement with a reviewer who
was right. ``worth_asking`` is how a caller declines to spend a call, and a
question that should not be asked is better than a cheap one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from djaudit.llm.provider import Field, FieldKind, Prompt, ResponseSchema
from djaudit.models import Finding

# Bumped whenever the wording below changes. It is part of the cache key, but
# the rendered text is hashed too, so forgetting to bump this is survivable --
# it is here to make a deliberate change legible in a diff, not to be the only
# thing standing between two different questions.
TRIAGE_PROMPT_VERSION = "triage/1"

# Long enough for a model to see the shape of the code, short enough that one
# enormous evidence blob cannot crowd out the finding it belongs to.
MAX_EVIDENCE_CHARACTERS = 1200
MAX_SNIPPET_CHARACTERS = 600

# Two tiers, because one broad rule could not do both jobs.
#
# The first is anchored on a vendor's own prefix. Those are exact, so they can
# admit separators and long tails without ever matching anything else.
#
# The second is for opaque tokens that announce nothing -- base64 blobs, hex
# digests, AWS-style keys. My first version wrote it as a run of forty
# characters including `/`, `-` and `_`, and measured zero false matches on the
# corpora. That measurement was wrong because it read only snippets and
# evidence: the rendered prompt also carries the file path and the message, and
# on the full text the rule fired on 99 of 245 findings -- every one a file
# path like `hc/accounts/management/commands/sendinactivitynotices` or a
# squashed migration name.
#
# Separators turn out to divide the two populations cleanly. Every false match
# held a `/` or a `_`; none of the credential shapes did. So the opaque tier
# admits no separators at all, which costs the base64url alphabet's `-` and `_`
# -- a documented gap, and one narrowed by the fact that a rule holding a value
# it knows to be a secret has already run it through `mask`.
_SECRET_SHAPES = re.compile(
    r"""
    (?: sk-[A-Za-z0-9_-]{16,}                    # OpenAI, Anthropic
      | gh[pousr]_[A-Za-z0-9]{20,}               # GitHub
      | AIza[A-Za-z0-9_-]{20,}                   # Google
      | xox[baprs]-[A-Za-z0-9-]{10,}             # Slack
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}   # JWT
      | -----BEGIN[ A-Z]*PRIVATE\ KEY-----      # PEM
      | \b[A-Za-z0-9+]{40,}={0,2}                # an opaque run, no separators
    )
    """,
    re.VERBOSE,
)


def redact(text: str) -> str:
    """Blank anything shaped like a credential, keeping the shape visible.

    Matches the convention `mask` already established, so a reader who has seen
    one form of this in a SARIF file recognises the other.
    """

    def replace(match: re.Match[str]) -> str:
        found = match.group(0)
        return f"{found[:2]}<redacted:{len(found)} chars>"

    return _SECRET_SHAPES.sub(replace, text)


def _clip(text: str, limit: int) -> str:
    """Truncate on a character count, and say so.

    Silently truncated evidence reads as complete evidence, and a model asked
    to judge half a function without being told it is half will judge it
    confidently.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated: {len(text) - limit} more characters]"


TRIAGE_SCHEMA = ResponseSchema(
    fields=(
        Field(
            "verdict",
            FieldKind.STRING,
            "true_positive if this is a real defect worth fixing; accepted_risk if "
            "the code is as described but the risk is deliberate or immaterial here; "
            "unsure if the excerpt does not settle it",
            choices=("true_positive", "accepted_risk", "unsure"),
        ),
        Field(
            "reason",
            FieldKind.STRING,
            "one or two sentences citing what in the excerpt decided it",
        ),
    )
)

_SYSTEM = """\
You are reviewing the output of djaudit, a static analyser for Django projects.
A deterministic rule has already found this code and described what it matched.
Your job is to judge whether the match matters in this codebase, not to
re-detect it.

Answer accepted_risk when the code is exactly as described but the risk is
deliberate, compensated for elsewhere, or immaterial in context -- a device
enrolment endpoint that must precede authentication, a debug flag in a settings
module that production never imports.

Answer unsure when the excerpt does not settle it. Guessing is worse than
declining: a wrong true_positive costs a reviewer an afternoon, and a wrong
accepted_risk is a real defect that a tool told someone to ignore.

You are seeing an excerpt, not the repository. Do not assume anything not shown
is absent."""


def render_finding(finding: Finding) -> str:
    """The body of the question: what the rule found, and what it saw."""
    lines = [
        f"rule: {finding.rule_id} — {finding.title}",
        f"severity: {finding.severity.value}   confidence: {finding.confidence.value}",
        f"location: {finding.location.file}:{finding.location.line}",
        "",
        f"what the rule reports: {finding.message}",
    ]
    if finding.rationale:
        lines += ["", f"why the rule cares: {finding.rationale}"]
    if finding.location.snippet:
        lines += [
            "",
            "the line it matched:",
            "```python",
            _clip(finding.location.snippet, MAX_SNIPPET_CHARACTERS),
            "```",
        ]
    for item in finding.evidence:
        label = f"evidence ({item.kind.value}{f' — {item.source}' if item.source else ''}):"
        lines += ["", label, _clip(item.content, MAX_EVIDENCE_CHARACTERS)]
    lines += ["", "Is this a real defect in this codebase?"]
    return redact("\n".join(lines))


def build_triage_prompt(finding: Finding) -> Prompt:
    """One finding, one question, rendered the same way every time."""
    return Prompt(
        version=TRIAGE_PROMPT_VERSION,
        system=_SYSTEM,
        user=render_finding(finding),
        schema=TRIAGE_SCHEMA,
    )


def worth_asking(finding: Finding, contested: Sequence[str] | frozenset[str]) -> bool:
    """Whether a model could improve on the deterministic answer here.

    `6.1.5` measured that 22 of 29 rules were judged identically every time
    they fired across three real codebases. Asking about those spends a call to
    risk contradicting a reviewer who was right, so the caller passes the
    contested set and this declines the rest.
    """
    return finding.rule_id in contested
