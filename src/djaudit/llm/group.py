"""Collapse a triaged run into themes, where a theme is one decision.

A run of 141 findings is not 141 questions. `DJP-004` firing nine times inside
`src/pretix/base/services/orders.py` is one fact about one file, and a reviewer
who reads it nine times learns nothing the second time.

**The grouping key was measured, not chosen.** Four candidates were tried
against the 245 recorded verdicts, scored on the only thing that matters -- how
often a group holds findings that were judged *differently*, because collapsing
those forces one decision where two were needed:

| key | groups | multi-finding | mixed-verdict | findings caught in one |
|---|---|---|---|---|
| rule | 47 | 21 | 5 | 73 |
| rule + directory (2 levels) | 66 | 27 | 4 | 65 |
| rule + directory | 92 | 40 | 3 | 31 |
| **rule + file** | **149** | **45** | **0** | **0** |

Directory grouping compresses far harder -- 92 groups against 149 -- and it is
wrong. Three of its groups mix verdicts, covering 31 findings, 12.7% of the
corpus. `rule + file` is the only key that never merged a true positive with an
accepted risk, and it still moves 141 of 245 findings into a group and removes
39% of the things to look at. Compression is not the objective; not lying about
what a reviewer already decided is.

And a theme never hides a disagreement. If the findings under one *do* carry
different verdicts, `Theme.verdict` is `None` and the renderer says so, rather
than showing a majority and quietly outvoting the exception.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from djaudit.llm.evaluate import Verdict
from djaudit.llm.triage import Judgement, Source
from djaudit.models import Severity

# Ordered worst-first, matching how the triage table already ranks.
_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


@dataclass(frozen=True, slots=True)
class Theme:
    """Every finding of one rule in one file, and what was decided about them."""

    rule_id: str
    file: str
    judgements: tuple[Judgement, ...]

    def __post_init__(self) -> None:
        if not self.judgements:
            # An empty theme would render as a file with nothing wrong in it,
            # which is worse than not rendering at all.
            raise ValueError(f"{self.rule_id} in {self.file}: a theme with no findings")

    @property
    def count(self) -> int:
        return len(self.judgements)

    @property
    def title(self) -> str:
        return self.judgements[0].finding.title

    @property
    def lines(self) -> tuple[int, ...]:
        return tuple(sorted(j.finding.location.line for j in self.judgements))

    @property
    def severity(self) -> Severity:
        """The worst in the group. A theme is as urgent as its worst member."""
        return min((j.finding.severity for j in self.judgements), key=_SEVERITY_ORDER.__getitem__)

    @property
    def verdict(self) -> Verdict | None:
        """The shared verdict, or None when the group does not agree.

        None is a real answer and must be rendered as one. Reporting the
        majority would let a theme silently overrule the one finding somebody
        judged differently -- the exact failure that ruled out directory
        grouping.
        """
        verdicts = {j.verdict for j in self.judgements}
        return verdicts.pop() if len(verdicts) == 1 else None

    @property
    def source(self) -> Source | None:
        """Where the shared verdict came from, or None if they differ."""
        sources = {j.source for j in self.judgements}
        return sources.pop() if len(sources) == 1 else None

    @property
    def rank(self) -> tuple[int, ...]:
        """Worst verdict first, then severity, then the bigger group."""
        return (
            min(j.rank[0] for j in self.judgements),
            _SEVERITY_ORDER[self.severity],
            -self.count,
        )

    def where(self) -> str:
        """`file:12` for one, `file:12, 40, 61` for a few, `file (9 places)`.

        There is deliberately no special case for a single line: joining a
        one-element list already gives `file:12`. An `if len(lines) == 1`
        branch was here and a mutation round proved it dead -- deleting it
        changed no output anywhere, because it could not.
        """
        lines = self.lines
        if len(lines) <= _LINES_SHOWN:
            return f"{self.file}:" + ", ".join(str(line) for line in lines)
        return f"{self.file} ({len(lines)} places)"


# Beyond a handful, a list of line numbers stops being information.
_LINES_SHOWN = 4


def group(judgements: Iterable[Judgement]) -> list[Theme]:
    """Every judgement, in themes, worst first.

    Nothing is dropped and nothing is merged across files. The total in equals
    the total out, which is the property the whole `llm` package rests on: this
    layer reorders and summarises findings, it never edits the set.
    """
    buckets: dict[tuple[str, str], list[Judgement]] = defaultdict(list)
    for judgement in judgements:
        finding = judgement.finding
        buckets[(finding.rule_id, finding.location.file)].append(judgement)

    themes = [
        Theme(rule_id=rule_id, file=file, judgements=tuple(found))
        for (rule_id, file), found in buckets.items()
    ]
    themes.sort(key=lambda theme: (theme.rank, theme.rule_id, theme.file))
    return themes


def collapsed(themes: Sequence[Theme]) -> int:
    """How many fewer things there are to read. Zero when nothing grouped."""
    return sum(theme.count for theme in themes) - len(themes)
