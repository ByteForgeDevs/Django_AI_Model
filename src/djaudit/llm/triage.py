"""Deciding which findings a reviewer should read first, and which are worth asking about.

Two jobs, and the second one is mostly about *not* doing it.

`6.1.5` measured that 22 of the 29 rules that fired across healthchecks, netbox
and pretix were judged the same way every time, and concluded that a model
should only be asked about the other seven. Building the runtime path made that
conclusion look thinner than it read. Most of those 22 rules were unanimous
across **one or two** findings. "Every reviewer who ever saw this agreed" and
"the single reviewer who saw it once agreed with himself" are the same sentence
when n is 1, and only one of them is evidence.

So the threshold was measured rather than chosen. Fitting the prior on two
targets and applying it to the third:

| minimum n | findings covered | agreement | downgrades | calls saved |
|---|---|---|---|---|
| 1 | 120 | 94.2% | **4** | 49.0% |
| 2 | 110 | 99.1% | **1** | 44.9% |
| 3 | 96 | 99.0% | **1** | 39.2% |
| **5** | **94** | **100.0%** | **0** | **38.4%** |
| 8 | 86 | 100.0% | 0 | 35.1% |
| 10 | 77 | 100.0% | 0 | 31.4% |

Five is the knee: the smallest threshold that makes no mistakes on a codebase
it was not fitted on. Dropping to one buys another eleven points of savings and
pays for them with four downgrades -- four real defects that this table would
have waved through on its own authority, without anyone asking anything. That
is the error this project exists to prevent, and it is not for sale at eleven
percent.

The consequence is that the shipped prior is five rules, not twenty-two, and
triage asks about 136 of the 245 corpus findings rather than 110. That is a
correction to `6.1.5`, made by the measurement that `6.1.5` made possible.

**What the prior is not.** It is a record of how reviewers judged five rules on
three open-source Django projects. It is not a claim about anyone else's
codebase, and every judgement it produces is labelled `corpus` in the output so
a reader can discount it. A model's answer is labelled `model`. Nothing here
ever presents a borrowed verdict as though this code had been examined.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from djaudit.llm.evaluate import Verdict
from djaudit.llm.prompts import build_triage_prompt, worth_asking
from djaudit.llm.provider import Answer, Declined, Provider, SchemaViolationError
from djaudit.models import Finding
from djaudit.provenance import Authorship, Provenance
from djaudit.provenance import Verdict as LabelledVerdict

# The evidence a rule needs before its corpus verdict is allowed to stand in
# for a reviewer. Measured, not chosen -- see the table above.
MINIMUM_OBSERVATIONS = 5

# Generated from `benchmarks/*.json` by `scripts/gen_triage_prior.py`, which
# also runs in CI with `--check` so this cannot drift from the verdicts it
# claims to summarise. Each entry is (verdict, findings observed).
#
# Only rules that are unanimous across at least MINIMUM_OBSERVATIONS findings
# appear. Seventeen further rules were unanimous on fewer than five and are
# deliberately absent: too little to skip a question over.
CORPUS_PRIOR: dict[str, tuple[Verdict, int]] = {
    "DJA-010": (Verdict.ACCEPTED_RISK, 15),
    "DJM-002": (Verdict.ACCEPTED_RISK, 7),
    "DJM-003": (Verdict.ACCEPTED_RISK, 5),
    "DJP-001": (Verdict.TRUE_POSITIVE, 46),
    "DJP-002": (Verdict.TRUE_POSITIVE, 10),
    "DJP-007": (Verdict.TRUE_POSITIVE, 31),
    "DJP-010": (Verdict.TRUE_POSITIVE, 7),
}


class Source(StrEnum):
    """Where a verdict came from, which the reader is entitled to know."""

    CORPUS = "corpus"
    MODEL = "model"
    # Asked, and nothing answered: offline, no credential, budget spent, or the
    # provider was unreachable. Distinct from a model that answered "unsure",
    # which is a judgement and arrives as MODEL.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Judgement:
    """One finding, one verdict, and an honest account of where it came from."""

    finding: Finding
    verdict: Verdict
    source: Source
    reason: str

    @property
    def rank(self) -> tuple[int, int, int, str]:
        """Worst first, with ties broken by something that does not move.

        Severity is the tie-breaker rather than the primary key because a
        low-severity finding a reviewer will actually act on outranks a
        critical one that three reviewers have already waved through. The
        fingerprint last makes the order stable across runs, which matters when
        the output is diffed.
        """
        buckets = {
            Verdict.TRUE_POSITIVE: 0,
            Verdict.ABSTAINED: 1,
            Verdict.ACCEPTED_RISK: 2,
        }
        return (
            buckets[self.verdict],
            -self.finding.severity.rank,
            -self.finding.confidence.rank,
            self.finding.fingerprint,
        )


@dataclass(frozen=True, slots=True)
class TriageRun:
    """Every finding judged, plus what it cost to judge them."""

    judgements: list[Judgement] = field(default_factory=list)
    asked: int = 0
    skipped: int = 0
    declined: int = 0
    #: Replies refused by the schema. A *subset* of `declined` rather than a
    #: sibling of it: no usable answer came back, and the reason was the
    #: provider's own fault. Keeping it a subset leaves the summary arithmetic
    #: correct while still telling an operator which kind of nothing they got,
    #: because "the provider said nothing" and "the provider said something it
    #: was never asked" call for very different reactions.
    misbehaved: int = 0

    @property
    def ranked(self) -> list[Judgement]:
        return sorted(self.judgements, key=lambda j: j.rank)

    def counting(self, verdict: Verdict) -> int:
        return sum(1 for j in self.judgements if j.verdict is verdict)

    @property
    def consulted_a_model(self) -> bool:
        """Whether any verdict here was actually produced by a model.

        The summary line depends on this. A run that asked twelve questions and
        had all twelve declined must not read as a triaged run.
        """
        return any(j.source is Source.MODEL for j in self.judgements)


def askable_rules(
    rule_ids: Collection[str],
    prior: dict[str, tuple[Verdict, int]] | None = None,
) -> frozenset[str]:
    """The rules whose verdict is not already settled by the corpus.

    Everything the prior does not cover, which is both the genuinely contested
    rules and the ones that never fired on any of the three targets. Those are
    different reasons for the same conclusion: nothing here knows the answer,
    so if a model is available it should be asked.
    """
    table = CORPUS_PRIOR if prior is None else prior
    return frozenset(rule_ids) - frozenset(table)


@runtime_checkable
class PerFinding(Protocol):
    """A provider that can be told which finding a question is about.

    The cache uses it to put the fingerprint in the key, so two findings that
    happen to render the same question -- the same rule on two identical lines
    in different files -- cannot share an answer. Providers that do not care
    are used unchanged, which is why this is a protocol and not a requirement.
    """

    def for_finding(self, fingerprint: str) -> Provider: ...


def triage(
    findings: Sequence[Finding],
    provider: Provider,
    *,
    prior: dict[str, tuple[Verdict, int]] | None = None,
) -> TriageRun:
    """Judge every finding, spending a call only where the answer is in doubt."""
    table = CORPUS_PRIOR if prior is None else prior
    askable = askable_rules({f.rule_id for f in findings}, table)

    judgements: list[Judgement] = []
    asked = skipped = declined = misbehaved = 0

    for finding in findings:
        if not worth_asking(finding, askable):
            verdict, observations = table[finding.rule_id]
            skipped += 1
            judgements.append(
                Judgement(
                    finding=finding,
                    verdict=verdict,
                    source=Source.CORPUS,
                    reason=(
                        f"every one of {observations} {finding.rule_id} findings reviewed "
                        f"across three Django projects was judged {verdict.value}"
                    ),
                )
            )
            continue

        asked += 1
        asker = (
            provider.for_finding(finding.fingerprint)
            if isinstance(provider, PerFinding)
            else provider
        )
        try:
            reply = asker.ask(build_triage_prompt(finding))
        except SchemaViolationError as violation:
            # A provider that answers a question nobody asked is refused --
            # that happens in `validate` and is not negotiable. What is
            # negotiable is whether one such reply destroys the run, and it
            # must not: the findings are already computed and correct, and
            # throwing them away leaves the operator with a traceback instead
            # of an audit. So the violation becomes an abstention that names
            # itself, and `misbehaved` makes it impossible to mistake the run
            # for a working one.
            misbehaved += 1
            reply = Declined(f"the provider returned an invalid reply: {violation}")
        if isinstance(reply, Answer):
            judgements.append(
                Judgement(
                    finding=finding,
                    verdict=_verdict_of(reply),
                    source=Source.MODEL,
                    reason=str(reply.content.get("reason", "")),
                )
            )
            continue

        declined += 1
        judgements.append(
            Judgement(
                finding=finding,
                verdict=Verdict.ABSTAINED,
                source=Source.UNAVAILABLE,
                reason=reply.reason,
            )
        )

    return TriageRun(
        judgements=judgements,
        asked=asked,
        skipped=skipped,
        declined=declined,
        misbehaved=misbehaved,
    )


def _verdict_of(answer: Answer) -> Verdict:
    """Map the schema's three choices onto the scored enum.

    ``unsure`` is a real answer -- a model declining to guess -- and it lands on
    ABSTAINED so that `evaluate.score` counts it as the abstention it is rather
    than crediting it to whichever class happens to be larger.
    """
    raw = str(answer.content.get("verdict", ""))
    if raw == Verdict.TRUE_POSITIVE.value:
        return Verdict.TRUE_POSITIVE
    if raw == Verdict.ACCEPTED_RISK.value:
        return Verdict.ACCEPTED_RISK
    return Verdict.ABSTAINED


#: `Source` and `Authorship` are the same distinction seen from two sides:
#: one names where a *verdict* came from, the other where any statement came
#: from. Mapping explicitly rather than relying on the string values matching
#: means renaming either enum is a type error rather than a silent mislabel.
_AUTHORSHIP = {
    Source.CORPUS: Authorship.CORPUS,
    Source.MODEL: Authorship.MODEL,
    Source.UNAVAILABLE: Authorship.UNAVAILABLE,
}


def provenance_of(judgement: Judgement, model: str = "") -> Provenance:
    """Label one verdict.

    The finding itself is always deterministic; this describes the *verdict*
    attached to it, which is the only part a model can have touched. The model
    name is carried only when a model actually answered -- naming it on a
    corpus hit would imply an involvement that did not happen.
    """
    authorship = _AUTHORSHIP[judgement.source]
    return Provenance(
        authorship=authorship,
        detail=judgement.reason,
        model=model if authorship is Authorship.MODEL else "",
    )


def verdicts_for(run: TriageRun, model: str = "") -> dict[str, LabelledVerdict]:
    """Label a whole run, keyed by fingerprint for the reporters.

    Findings with no fingerprint are skipped rather than keyed on the empty
    string, which would collapse all of them onto one label.
    """
    return {
        j.finding.fingerprint: LabelledVerdict(
            label=j.verdict.value,
            provenance=provenance_of(j, model),
        )
        for j in run.judgements
        if j.finding.fingerprint
    }
