"""Proposing a suppression, and never writing one.

A suppression is a permanent instruction to stop reporting something. It
outlives the person who added it, the reason they added it, and usually the
code it was about. So this module proposes and stops, and the proposal is a
patch the reviewer applies themselves -- not because applying it would be hard,
but because the moment a tool can silence its own findings, "djaudit is clean"
stops meaning anything.

**A borrowed verdict is not grounds for one.** `triage` will happily rank a
finding low on the strength of the corpus prior: fifteen reviewers on three
other Django projects waved this rule through, so it goes to the bottom of the
list. That is a reasonable way to order a reviewer's afternoon and a terrible
basis for writing a permanent comment into somebody's source. Ranking is
reversible and suppression is not, so only a `model` verdict -- something that
read *this* code and said why -- can justify a proposal here.

The consequence is that an offline djaudit proposes nothing at all, which is
correct rather than unfortunate. A tool with no model available has no basis on
which to silence anything, and `--write-baseline` already exists for the case
where the *person* has decided.

**Every proposal must actually work.** A generated comment the shipped parser
does not recognise would be worse than none: the reviewer commits it, the
finding keeps firing, and they conclude the suppression syntax is broken. Each
proposal is therefore round-tripped through `suppression.line_suppresses`
before it is offered, and the tests apply one to real source and re-run the
engine to watch the finding disappear.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from djaudit.llm.evaluate import Verdict
from djaudit.llm.triage import Judgement, Source, TriageRun
from djaudit.suppression import line_suppresses

# A justification is the whole point of the comment, so an empty one is not a
# proposal worth making. Short enough to admit "deliberate, see PROJ-412";
# long enough to exclude "ok".
MINIMUM_JUSTIFICATION = 12

# Suppression comments live at the end of a line that already exists, and a
# line that ruff or black will immediately reflow is a proposal that creates
# work. Anything that would overflow is offered as a baseline entry instead.
MAXIMUM_LINE = 120

# What `mask()` in `rules/_base.py` leaves behind. A snippet carrying this has
# had a real value cut out of it and cannot be patched back onto disk.
REDACTION_MARKER = "<redacted:"


class SuppressionError(Exception):
    """A proposal that could not be made honestly."""


@dataclass(frozen=True, slots=True)
class Proposal:
    """One suppression somebody might choose to apply, and the case for it."""

    judgement: Judgement
    original: str
    line: str
    justification: str

    @property
    def rule_id(self) -> str:
        return self.judgement.finding.rule_id

    @property
    def file(self) -> str:
        return self.judgement.finding.location.file

    @property
    def line_number(self) -> int:
        return self.judgement.finding.location.line

    def diff(self) -> str:
        """A unified diff of exactly one line, ready for `git apply`.

        Rendered rather than applied. The reviewer reads the justification, the
        line it lands on and the rule it silences, all in one hunk, and decides.
        """
        return "\n".join(
            difflib.unified_diff(
                [self.original],
                [self.line],
                fromfile=f"a/{self.file}",
                tofile=f"b/{self.file}",
                lineterm="",
                n=0,
            )
        )


def comment_for(rule_id: str, justification: str) -> str:
    """The suppression comment itself, in the form the parser recognises.

    Rule-scoped, never bare. `# djaudit: ignore` with no brackets is honoured by
    the parser and silences every rule on the line, which is not what anyone
    means when they have judged one specific finding.
    """
    return f"  # djaudit: ignore[{rule_id.upper()}] {justification}"


def check_grounds(judgement: Judgement) -> str:
    """The case for suppressing, or a refusal. Returns the justification.

    Separated from `propose` so `suggest` can refuse on the grounds before it
    reads anything from disk. Checking the file first meant a borrowed verdict
    on a moved line was reported as "that line is gone", which is true and not
    the reason it would have been refused anyway.
    """
    finding = judgement.finding
    if judgement.verdict is not Verdict.ACCEPTED_RISK:
        raise SuppressionError(
            f"{finding.rule_id}: only an accepted risk can be suppressed, "
            f"not {judgement.verdict.value}"
        )
    if judgement.source is not Source.MODEL:
        raise SuppressionError(
            f"{finding.rule_id}: a {judgement.source.value} verdict cannot justify "
            "suppressing a finding in this codebase"
        )

    justification = " ".join(judgement.reason.split())
    if len(justification) < MINIMUM_JUSTIFICATION:
        raise SuppressionError(
            f"{finding.rule_id}: no justification given, and a suppression without "
            "one is a finding that vanished"
        )
    return justification


def propose(judgement: Judgement, source_line: str) -> Proposal:
    """Build one proposal against the real source line, refusing without grounds.

    **The source line is passed in, and must come from disk.** The first version
    of this built the patch from `finding.location.snippet`, which reads
    correctly and is catastrophic: a settings rule masks the secret it found, so
    the snippet for `SECRET_KEY = "3t(5n^s..."` is `SECRET_KEY =
    "*x<redacted:50 chars>"`. Applying that patch would have replaced a live
    credential with the redaction marker. Nothing in the diff would have looked
    wrong. Every cheap test passed; applying one to a real file is what caught
    it, which is why that test exists.

    Raises rather than returning None, because every refusal here is a
    programming error at the call site rather than an ordinary outcome --
    `suggest` filters first.
    """
    finding = judgement.finding
    justification = check_grounds(judgement)

    if not source_line.strip():
        raise SuppressionError(f"{finding.rule_id}: no source line to attach a comment to")
    # Defence in depth. The masked form should never reach here now that the
    # line comes from disk, and if it ever does the proposal is refused rather
    # than written into somebody's settings module.
    if REDACTION_MARKER in source_line:
        raise SuppressionError(
            f"{finding.rule_id}: the source line is masked, and patching it would "
            "overwrite the real value"
        )
    if line_suppresses(source_line, finding.rule_id):
        raise SuppressionError(f"{finding.rule_id}: this line is already suppressed")

    line = source_line.rstrip() + comment_for(finding.rule_id, justification)
    if len(line) > MAXIMUM_LINE:
        raise SuppressionError(
            f"{finding.rule_id}: the comment would make a {len(line)}-character line; "
            "record this one in a baseline instead"
        )
    # The proposal is checked against the parser that will read it, so a
    # suggestion that does not actually suppress cannot be offered.
    if not line_suppresses(line, finding.rule_id):
        raise SuppressionError(
            f"{finding.rule_id}: generated a comment djaudit itself does not honour"
        )
    return Proposal(
        judgement=judgement,
        original=source_line.rstrip("\n"),
        line=line,
        justification=justification,
    )


def source_line_of(root: Path, judgement: Judgement) -> str:
    """The line the finding actually sits on, read from disk.

    Read fresh rather than taken from the finding, because the finding's copy
    may be masked and because a patch is only meaningful against what is there
    now. A file that has moved on since the run yields a line that will not
    match, and the proposal is refused rather than applied to the wrong place.
    """
    location = judgement.finding.location
    path = root / location.file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SuppressionError(
            f"{judgement.finding.rule_id}: cannot read {location.file}: {exc}"
        ) from exc
    if not 1 <= location.line <= len(lines):
        raise SuppressionError(
            f"{judgement.finding.rule_id}: {location.file} has no line {location.line} "
            "any more; re-run djaudit before suppressing"
        )
    return lines[location.line - 1]


def suggest(run: TriageRun, root: Path) -> tuple[list[Proposal], list[str]]:
    """Every suppression worth offering, and why the rest were not offered.

    Both halves are returned. A caller shown three proposals and told nothing
    about the eleven findings that did not qualify would reasonably conclude
    there were only three accepted risks.
    """
    proposals: list[Proposal] = []
    refused: list[str] = []
    for judgement in run.ranked:
        if judgement.verdict is not Verdict.ACCEPTED_RISK:
            continue
        try:
            # Grounds before disk: a verdict that could never justify a
            # suppression should not send this reading somebody's source.
            check_grounds(judgement)
            proposals.append(propose(judgement, source_line_of(root, judgement)))
        except SuppressionError as exc:
            refused.append(str(exc))
    return proposals, refused


def render(proposals: Sequence[Proposal]) -> str:
    """The whole patch, or an empty string if there is nothing to propose."""
    return "\n".join(proposal.diff() for proposal in proposals)
