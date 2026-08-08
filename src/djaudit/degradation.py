"""What the audit could not check, and what it checked instead.

The live tier is off by default and will be unavailable on most runs: no
virtualenv found, no consent given, a target whose Django will not start. That
is the *normal* case, not the exceptional one, and it has a failure mode worth
naming.

**A security tool that quietly checks less is worse than one that fails.** A
run with the live tier absent emits fewer findings, and fewer findings look
exactly like a cleaner codebase. The reader has no way to tell "we checked and
found nothing" from "we could not check". So every live rule that does not run
is counted, named, and reported alongside the findings, and every live rule
declares in `RuleMeta.fallback` what still covers its ground statically.

The declaration is enforced rather than encouraged: `tests/live/` fails if a
`Tier.LIVE` rule ships without a written fallback sentence, and if the static
rule ids it names do not exist. The gate is in place before the first live rule
so that `DJM-010` cannot land without one.

This module lives outside `djaudit.live` on purpose. Everything in that package
is written as though the target were hostile because everything in it executes
target code; this module only reads rule metadata, and putting it there made
every audit run import the subprocess machinery to print a sentence.

**A fallback is not a substitute.** `DJM-002` reading an `AlterField` from
source and `DJM-010` reading the `ALTER TABLE` that Django actually emits are
not the same check at different confidences -- the second can see a rewrite the
first can only suspect. The sentence exists to say what is *lost*, so a reader
deciding whether to install a virtualenv can price it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from djaudit.models import Tier
from djaudit.registry import all_rules

if TYPE_CHECKING:
    from djaudit.registry import Rule


class Skipped(NamedTuple):
    """One live rule that did not run, and what covers it in the meantime."""

    rule_id: str
    title: str
    fallback: str
    covered_by: tuple[str, ...] = ()

    def explain(self) -> str:
        covered = f" Still checked by {', '.join(self.covered_by)}." if self.covered_by else ""
        return f"{self.rule_id} {self.title}: {self.fallback}{covered}"


class Degradation(NamedTuple):
    """The live rules this run did not reach, and why.

    Falsy when nothing was skipped, so a caller can write `if result.degraded`
    without having to distinguish "live ran" from "live had nothing to do".
    """

    reason: str
    skipped: tuple[Skipped, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.skipped)

    def explain(self) -> str:
        if not self.skipped:
            return f"no live-tier rules were skipped ({self.reason})"
        count = len(self.skipped)
        rules = "rule" if count == 1 else "rules"
        return f"{count} live-tier {rules} did not run ({self.reason})"

    def report(self) -> tuple[str, ...]:
        """The heading and one line per skipped rule, for a terminal or a log."""
        return (self.explain(), *(item.explain() for item in self.skipped))


def _covering(rule: type[Rule], ran: frozenset[str]) -> tuple[str, ...]:
    """The declared fallbacks that this run actually executed.

    Filtered by what ran rather than reported verbatim, because a fallback the
    reader also excluded covers nothing and saying otherwise is a false
    reassurance -- the exact failure this module exists to prevent.
    """
    return tuple(rule_id for rule_id in rule.meta.fallback_rules if rule_id in ran)


def assess(reason: str, *, ran: frozenset[str] | set[str]) -> Degradation:
    """Everything registered as live that `ran` does not contain.

    Derived from the registry rather than from the engine's own filtering, so a
    live rule is reported as skipped however it came to be skipped -- tier
    filter, family filter, or an `--exclude` the reader forgot they set.
    """
    ran = frozenset(ran)
    skipped = tuple(
        Skipped(
            rule_id=rule.meta.id,
            title=rule.meta.title,
            fallback=rule.meta.fallback,
            covered_by=_covering(rule, ran),
        )
        for rule in sorted(all_rules(), key=lambda r: r.meta.id)
        if rule.meta.tier is Tier.LIVE and rule.meta.id not in ran
    )
    return Degradation(reason=reason, skipped=skipped)
