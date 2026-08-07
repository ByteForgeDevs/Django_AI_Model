"""Who authored each thing djaudit says.

A finding, an explanation and a triage verdict can sit next to each other in
one report and look equally authoritative, and they are not. Findings come out
of deterministic AST rules and are reproducible byte for byte; a triage verdict
may have come from a recorded corpus, or from a language model, or from nothing
at all because no model was reachable. Somebody acting on the report is
entitled to know which, and a machine consuming the report needs it more than a
human does, because a machine cannot pick the difference up from tone.

The dangerous case is not a wrong label -- it is no label. An unlabelled SARIF
result is indistinguishable from a labelled one to any tool that does not know
to look, so "no model was involved" and "nobody said" collapse into the same
silence. Everything therefore carries provenance, including the deterministic
findings that make up the overwhelming majority and would otherwise seem not to
need it. `DETERMINISTIC` exists precisely so that absence of a label can never
be read as a claim.

This module deliberately does not import the LLM layer. Reporters need the
vocabulary, not the machinery; keeping the dependency pointing this way means
`djaudit report --format sarif` stamps provenance without the report path being
able to reach a provider at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Authorship(StrEnum):
    """What produced a statement.

    Deliberately not a boolean. "A model wrote this" and "a model was asked and
    said nothing" are different facts with different consequences, and folding
    them together loses the one a reader most needs.
    """

    #: An AST rule. Reproducible, offline, identical between runs.
    DETERMINISTIC = "deterministic"
    #: A recorded corpus of human triage decisions. Human judgement, but from
    #: a different codebase than this one, which is worth knowing.
    CORPUS = "corpus"
    #: A language model answered. May differ between runs. Never authored a
    #: finding -- only ever a judgement about one, or prose alongside it.
    MODEL = "model"
    #: A model was asked and nothing came back: offline, no credential, budget
    #: spent, provider unreachable. Distinct from a model that answered
    #: "unsure", which is a judgement and is MODEL.
    UNAVAILABLE = "unavailable"


#: Labels that mean a language model influenced the text. Used by consumers
#: that want to hold model-authored material to a different standard.
FROM_A_MODEL = frozenset({Authorship.MODEL})


@dataclass(frozen=True, slots=True)
class Provenance:
    """One statement's origin, in a form a machine can filter on."""

    authorship: Authorship
    detail: str = ""
    model: str = ""

    def __post_init__(self) -> None:
        if self.model and self.authorship is not Authorship.MODEL:
            raise ValueError(
                f"{self.authorship.value} provenance cannot name a model "
                f"({self.model!r}); only MODEL may."
            )

    @property
    def deterministic(self) -> bool:
        return self.authorship is Authorship.DETERMINISTIC

    @property
    def reproducible(self) -> bool:
        """Whether re-running is expected to produce the same answer.

        A corpus lookup is reproducible without being deterministic in the AST
        sense: the answer is fixed, but it was a human's, not a rule's.
        """
        return self.authorship in {Authorship.DETERMINISTIC, Authorship.CORPUS}

    def as_properties(self) -> dict[str, Any]:
        """The SARIF/JSON property bag form.

        `model` is omitted rather than emitted empty, so a consumer testing for
        its presence gets the right answer without also having to test for the
        empty string.
        """
        payload: dict[str, Any] = {
            "authorship": self.authorship.value,
            "reproducible": self.reproducible,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.model:
            payload["model"] = self.model
        return payload


#: The provenance of every finding djaudit emits. Findings are never authored
#: by a model; see `tests/llm/test_fixes_need_no_model.py` and the hostile
#: provider check, both of which enforce that rather than assume it.
DETERMINISTIC = Provenance(
    authorship=Authorship.DETERMINISTIC,
    detail="emitted by an AST rule; no model was consulted",
)


@dataclass(frozen=True, slots=True)
class Verdict:
    """A judgement *about* a finding, and where the judgement came from.

    Kept separate from the finding's own provenance on purpose, and the reason
    is a mistake this design already made once: stamping a single label onto
    each result meant an offline run marked all 24 of its findings
    `unavailable` and `reproducible: false`. Every one of those findings came
    from a deterministic rule and would reproduce byte for byte. What was
    unavailable was the *opinion* about them, and a consumer told otherwise
    would discard perfectly solid results.

    So a result carries two statements. The finding is always deterministic.
    The verdict is whatever it is.
    """

    label: str
    provenance: Provenance

    def as_properties(self) -> dict[str, Any]:
        return {"verdict": self.label, "provenance": self.provenance.as_properties()}


def describe(provenance: Provenance) -> str:
    """A one-line human rendering, for terminal output and diffs."""
    if provenance.authorship is Authorship.DETERMINISTIC:
        return "deterministic rule"
    if provenance.authorship is Authorship.CORPUS:
        return "recorded human triage"
    if provenance.authorship is Authorship.UNAVAILABLE:
        return "no model answered"
    return f"model {provenance.model}" if provenance.model else "model"
