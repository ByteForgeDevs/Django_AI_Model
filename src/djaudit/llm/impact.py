"""Frame a finding for somebody who does not write Django.

A reviewer who is not a Django specialist reads "N+1 query on a
`SerializerMethodField`" and cannot tell whether to schedule it, escalate it or
close it. What they need is who it affects, what it costs, how widespread it is,
and -- the part almost every security tool omits -- **what would have to be true
for it not to matter to them**.

**Nothing here is invented, and that is the whole design.** Business impact is
where a tool starts producing sentences nobody can check: "could lead to a
breach", "may cost you thousands", "affects 40% of users". Those read as
authority and are unfalsifiable, and one of them being wrong discredits the 244
findings next to it. So the framing is assembled from three sources that are all
already facts:

1. **Who and what it costs** -- a fixed sentence per family. No numbers.
2. **How widespread** -- counted from the run. Occurrences and files, nothing else.
3. **When it does not apply** -- the rule's own ``limitations``, authored next
   to the rule. Measured: **all 67 rules carry them**, so this is available for
   every finding rather than most.

`no_invented_numbers` enforces (1) and (2) mechanically: every integer in the
rendered text must be one the run actually counted. A template that grew a
statistic would fail it.

No model is consulted. A model could rewrite this prose more fluently, and that
is all it could do -- there is no fact here it could supply.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from djaudit import registry
from djaudit.models import Family, Finding, Severity

# Who carries the cost, and what the cost is. One sentence per family, holding
# no numbers, no probabilities and no money -- see `no_invented_numbers`.
_AUDIENCE: dict[Family, tuple[str, str]] = {
    Family.DJS: (
        "everyone using the deployed site",
        "a misconfigured deployment leaks internal detail or weakens a "
        "protection the framework would otherwise provide",
    ),
    Family.DJI: (
        "anyone who can reach the affected endpoint",
        "untrusted input reaches an interpreter, so a caller can make the "
        "application do something it was not asked to do",
    ),
    Family.DJA: (
        "the people whose records the API serves",
        "the API returns or accepts more than the caller should be able to see or set",
    ),
    Family.DJP: (
        "every user of the affected page, and whoever pays for the database",
        "work grows with the number of rows, so it is fine in development and "
        "degrades as real data arrives",
    ),
    Family.DJD: (
        "whoever has to migrate or query this data later",
        "the schema does not say what the application assumes, so the "
        "database cannot enforce it and every reader must remember to",
    ),
    Family.DJM: (
        "everyone using the site during the deploy",
        "the migration takes a lock that blocks reads or writes while it runs",
    ),
    Family.DJX: (
        "whoever is on the database this was not developed against",
        "the two databases disagree, so behaviour that passes locally can differ in production",
    ),
}

# How much of a reviewer's week this is worth, in words rather than a score.
_URGENCY: dict[Severity, str] = {
    Severity.CRITICAL: "Treat as an incident: this does not wait for the next planning round.",
    Severity.HIGH: "Worth interrupting planned work for.",
    Severity.MEDIUM: "Schedule it; it is unlikely to be the thing that breaks today.",
    Severity.LOW: "Fix it when the file is open for another reason.",
    Severity.INFO: "Informational. Act only if it matches something you already care about.",
}

_INTEGER = re.compile(r"\d+")


@dataclass(frozen=True, slots=True)
class Impact:
    """Who is affected, what it costs, how widespread, and when it does not apply."""

    finding: Finding
    who: str
    cost: str
    urgency: str
    occurrences: int
    files: int
    caveats: tuple[str, ...]

    @property
    def counted(self) -> set[int]:
        """Every number this is allowed to print. See `no_invented_numbers`."""
        return {self.occurrences, self.files}


def blast_radius(finding: Finding, among: Sequence[Finding]) -> tuple[int, int]:
    """How many times this rule fired in the run, and across how many files.

    Counted, not estimated. One occurrence in one file and forty across twelve
    are different conversations, and it is the only scale figure available
    without running the application.
    """
    same_rule = [f for f in among if f.rule_id == finding.rule_id]
    if finding.fingerprint not in {f.fingerprint for f in same_rule}:
        same_rule.append(finding)
    return len(same_rule), len({f.location.file for f in same_rule})


def caveats_for(rule_id: str) -> tuple[str, ...]:
    """The rule's own account of what it cannot see.

    Taken from the rule rather than restated here, so it goes stale in the
    commit that makes it wrong. A rule that is not registered -- a finding
    loaded from an older report, say -- yields nothing rather than a guess.
    """
    try:
        return registry.get(rule_id).meta.limitations
    except (KeyError, registry.RuleError):
        return ()


def impact(finding: Finding, among: Sequence[Finding] = ()) -> Impact:
    who, cost = _AUDIENCE[finding.family]
    occurrences, files = blast_radius(finding, among)
    return Impact(
        finding=finding,
        who=who,
        cost=cost,
        urgency=_URGENCY[finding.severity],
        occurrences=occurrences,
        files=files,
        caveats=caveats_for(finding.rule_id),
    )


def framing(assessment: Impact) -> str:
    """The prose this module generates, with no rule-authored text in it.

    Separated from `render` so `no_invented_numbers` has something exact to
    check. The caveats below are the rule's own words and legitimately contain
    digits -- `DJA-008` cites `DJA-010` by name -- and running the digit check
    over them would either fail on honest text or have to be loosened until it
    stopped catching anything.
    """
    lines = [
        "Who this affects",
        f"  {assessment.who}",
        "",
        "What it costs",
        f"  {assessment.cost}",
        "",
        "How widespread",
        f"  {_spread(assessment)}",
        "",
        "How urgent",
        f"  {assessment.urgency}",
    ]
    return "\n".join(lines)


def render(assessment: Impact) -> str:
    """The framing, plus the rule's own account of when it does not apply.

    The caveats are reproduced **verbatim**. Paraphrasing them here would put
    this module in the business of restating a rule's limits in its own words,
    which is exactly how a limit turns into a reassurance.
    """
    text = framing(assessment)
    if assessment.caveats:
        text += "\n\n" + "\n".join(
            ["When this does not apply to you", *[f"  - {c}" for c in assessment.caveats]]
        )
    return text


def _spread(assessment: Impact) -> str:
    if assessment.occurrences == 1:
        return "one place in this codebase."
    if assessment.files == 1:
        return f"{assessment.occurrences} places, all in one file."
    return f"{assessment.occurrences} places across {assessment.files} files."


def no_invented_numbers(assessment: Impact, text: str) -> bool:
    """Whether every integer in `text` is one the run actually counted.

    The mechanism that keeps this module honest. Impact prose is exactly where
    a tool starts emitting figures nobody can check, and a made-up number reads
    identically to a measured one. So the templates hold no digits and this
    asserts it of the artefact that ships, rather than of the intention.

    Applied to `framing`, not to `render`: the caveats `render` appends are the
    rule's own text and are checked differently, by requiring them to appear
    verbatim.
    """
    return all(int(match) in assessment.counted for match in _INTEGER.findall(text))
