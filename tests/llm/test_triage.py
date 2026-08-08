"""Whether the triage layer earns the calls it makes, and admits what it borrows.

Two things are easy to get wrong here and neither shows up in a green test that
only checks the happy path.

The first is the prior. A table that says "this rule is always a true positive"
on the strength of one observation will suppress a question and be believed.
The threshold that governs it is asserted against the held-out corpus here, not
just documented, because the argument for five rather than one *is* a
measurement and a measurement nobody re-runs is a comment.

The second is provenance. A verdict borrowed from three other codebases and a
verdict produced by reading this one must never render the same, and the run
must say plainly when nothing was consulted at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from djaudit.llm.evaluate import Verdict, load_ground_truth
from djaudit.llm.prompts import TRIAGE_SCHEMA
from djaudit.llm.provider import Answer, Declined, Prompt, Provider, Reply, Usage
from djaudit.llm.triage import (
    CORPUS_PRIOR,
    MINIMUM_OBSERVATIONS,
    Judgement,
    Source,
    TriageRun,
    askable_rules,
    triage,
)
from djaudit.models import (
    Confidence,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)

BENCHMARKS = Path(__file__).resolve().parents[2] / "benchmarks"
GENERATOR = Path(__file__).resolve().parents[2] / "scripts" / "gen_triage_prior.py"


def make_finding(
    *,
    rule_id: str = "DJP-004",
    severity: Severity = Severity.HIGH,
    confidence: Confidence = Confidence.FIRM,
    fingerprint: str = "f0",
    line: int = 10,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        title="A finding",
        family=Family.DJP,
        severity=severity,
        confidence=confidence,
        tier=Tier.STATIC,
        location=Location(file="app/views.py", line=line, snippet="qs = Thing.objects.all()"),
        message="Something is wrong here.",
        rationale="Because it is.",
        fingerprint=fingerprint,
    )


class Says:
    """A provider that answers every question the same way."""

    def __init__(self, verdict: str, reason: str = "because") -> None:
        self.verdict = verdict
        self.reason = reason
        self.asked: list[Prompt] = []

    @property
    def name(self) -> str:
        return "says"

    def ask(self, prompt: Prompt) -> Reply:
        self.asked.append(prompt)
        return Answer(
            content={"verdict": self.verdict, "reason": self.reason},
            model="says",
            usage=Usage(input_tokens=10, output_tokens=5),
        )


class Refuses:
    def __init__(self, reason: str = "offline") -> None:
        self.reason = reason
        self.calls = 0

    @property
    def name(self) -> str:
        return "refuses"

    def ask(self, prompt: Prompt) -> Reply:
        self.calls += 1
        return Declined(self.reason)


class TestTheShippedPrior:
    """The table is a claim about evidence, so the evidence is checked."""

    def test_every_entry_is_unanimous_in_the_corpus(self) -> None:
        tallies: dict[str, Counter[Verdict]] = {}
        for finding in load_ground_truth(BENCHMARKS):
            tallies.setdefault(finding.rule_id, Counter())[finding.verdict] += 1

        for rule, (verdict, observations) in CORPUS_PRIOR.items():
            seen = tallies[rule]
            assert len(seen) == 1, f"{rule} is contested but ships a verdict"
            assert next(iter(seen)) is verdict
            assert sum(seen.values()) == observations

    def test_every_entry_clears_the_evidence_floor(self) -> None:
        for rule, (_, observations) in CORPUS_PRIOR.items():
            assert observations >= MINIMUM_OBSERVATIONS, f"{rule} rests on too little"

    def test_unanimous_rules_below_the_floor_are_left_out(self) -> None:
        """The contrast, not just the absence.

        Twenty-two rules are unanimous; only five are here. If the floor
        stopped being applied this would be the test that noticed, because the
        seventeen excluded ones would arrive.
        """
        tallies: dict[str, Counter[Verdict]] = {}
        for finding in load_ground_truth(BENCHMARKS):
            tallies.setdefault(finding.rule_id, Counter())[finding.verdict] += 1

        unanimous = {r for r, seen in tallies.items() if len(seen) == 1}
        thin = {r for r in unanimous if sum(tallies[r].values()) < MINIMUM_OBSERVATIONS}

        assert thin, "the corpus no longer has thin unanimous rules; this test is now vacuous"
        assert not (thin & set(CORPUS_PRIOR)), f"shipped on too little evidence: {thin}"

    def test_the_floor_is_where_held_out_errors_stop(self) -> None:
        """Why five, re-derived rather than remembered.

        Fit on two targets, apply to the third. At the shipped floor the prior
        makes no mistakes on a codebase it never saw; one step below it does.
        This is the whole argument for the parameter, so it runs.
        """
        findings = load_ground_truth(BENCHMARKS)
        targets = sorted({f.target for f in findings})

        def wrong_at(threshold: int) -> tuple[int, int]:
            wrong = downgrades = 0
            for held in targets:
                tallies: dict[str, Counter[Verdict]] = {}
                for f in (f for f in findings if f.target != held):
                    tallies.setdefault(f.rule_id, Counter())[f.verdict] += 1
                prior = {
                    r: next(iter(seen))
                    for r, seen in tallies.items()
                    if len(seen) == 1 and sum(seen.values()) >= threshold
                }
                for f in (f for f in findings if f.target == held):
                    if f.rule_id in prior and prior[f.rule_id] is not f.verdict:
                        wrong += 1
                        downgrades += int(f.is_true_positive)
            return wrong, downgrades

        assert wrong_at(MINIMUM_OBSERVATIONS) == (0, 0)
        # And the floor is doing work: trusting a single observation costs real
        # defects, which is the error this whole layer exists to avoid.
        below_wrong, below_downgrades = wrong_at(1)
        assert below_wrong > 0
        assert below_downgrades > 0


class TestWhatGetsAsked:
    def test_a_settled_rule_is_never_put_to_a_model(self) -> None:
        model = Says("accepted_risk")
        run = triage([make_finding(rule_id="DJP-001")], model)

        assert model.asked == []
        assert run.asked == 0
        assert run.skipped == 1
        assert run.judgements[0].source is Source.CORPUS
        assert run.judgements[0].verdict is Verdict.TRUE_POSITIVE

    def test_a_contested_rule_is_put_to_a_model(self) -> None:
        model = Says("accepted_risk")
        run = triage([make_finding(rule_id="DJP-004")], model)

        assert len(model.asked) == 1
        assert run.asked == 1
        assert run.judgements[0].source is Source.MODEL
        assert run.judgements[0].verdict is Verdict.ACCEPTED_RISK

    def test_a_rule_the_corpus_never_saw_is_put_to_a_model(self) -> None:
        """Unseen and contested are different reasons for the same conclusion.

        Thirty-eight implemented rules never fired on the three targets. The
        prior knows nothing about them, and silence is not agreement.
        """
        model = Says("true_positive")
        run = triage([make_finding(rule_id="DJI-004")], model)

        assert run.asked == 1
        assert run.judgements[0].source is Source.MODEL

    def test_the_model_overrides_nothing_it_was_not_asked_about(self) -> None:
        model = Says("accepted_risk")
        findings = [make_finding(rule_id="DJP-001", fingerprint="a"), make_finding(fingerprint="b")]
        run = triage(findings, model)

        by_source = {j.finding.fingerprint: j for j in run.judgements}
        assert by_source["a"].verdict is Verdict.TRUE_POSITIVE
        assert by_source["b"].verdict is Verdict.ACCEPTED_RISK

    def test_askable_covers_everything_the_prior_does_not(self) -> None:
        assert askable_rules({"DJP-001", "DJP-004", "DJX-999"}) == frozenset({"DJP-004", "DJX-999"})

    def test_a_caller_can_supply_its_own_prior(self) -> None:
        model = Says("accepted_risk")
        run = triage(
            [make_finding(rule_id="DJP-004")], model, prior={"DJP-004": (Verdict.TRUE_POSITIVE, 9)}
        )

        assert model.asked == []
        assert run.judgements[0].source is Source.CORPUS


class TestWhenNothingAnswers:
    def test_a_declined_call_is_undecided_not_acceptable(self) -> None:
        """The dangerous default.

        A refusal that fell through to "accepted risk" would turn every
        offline run into a tool telling people to ignore real defects.
        """
        run = triage([make_finding()], Refuses("no key"))

        assert run.judgements[0].verdict is Verdict.ABSTAINED
        assert run.judgements[0].source is Source.UNAVAILABLE
        assert run.judgements[0].reason == "no key"
        assert run.declined == 1

    def test_a_run_that_asked_and_got_nothing_did_not_consult_a_model(self) -> None:
        run = triage([make_finding()], Refuses())
        assert run.asked == 1
        assert not run.consulted_a_model

    def test_a_run_with_one_real_answer_did_consult_a_model(self) -> None:
        assert triage([make_finding()], Says("true_positive")).consulted_a_model

    def test_corpus_verdicts_alone_are_not_a_consultation(self) -> None:
        run = triage([make_finding(rule_id="DJP-001")], Refuses())
        assert run.judgements[0].verdict is Verdict.TRUE_POSITIVE
        assert not run.consulted_a_model

    def test_an_unsure_model_abstains_rather_than_guessing(self) -> None:
        run = triage([make_finding()], Says("unsure"))
        assert run.judgements[0].verdict is Verdict.ABSTAINED
        assert run.judgements[0].source is Source.MODEL

    def test_a_verdict_outside_the_schema_abstains(self) -> None:
        """Belt and braces: the schema already rejects this upstream."""
        run = triage([make_finding()], Says("definitely_exploitable"))
        assert run.judgements[0].verdict is Verdict.ABSTAINED


class TestRanking:
    def test_real_defects_come_before_accepted_risks_of_any_severity(self) -> None:
        low_real = Judgement(
            make_finding(severity=Severity.LOW, fingerprint="low"),
            Verdict.TRUE_POSITIVE,
            Source.MODEL,
            "",
        )
        critical_accepted = Judgement(
            make_finding(severity=Severity.CRITICAL, fingerprint="crit"),
            Verdict.ACCEPTED_RISK,
            Source.CORPUS,
            "",
        )
        run = TriageRun(judgements=[critical_accepted, low_real])

        assert [j.finding.fingerprint for j in run.ranked] == ["low", "crit"]

    def test_undecided_sits_between_them(self) -> None:
        order = [
            Judgement(make_finding(fingerprint="c"), Verdict.ACCEPTED_RISK, Source.CORPUS, ""),
            Judgement(make_finding(fingerprint="b"), Verdict.ABSTAINED, Source.UNAVAILABLE, ""),
            Judgement(make_finding(fingerprint="a"), Verdict.TRUE_POSITIVE, Source.MODEL, ""),
        ]
        assert [j.finding.fingerprint for j in TriageRun(judgements=order).ranked] == [
            "a",
            "b",
            "c",
        ]

    def test_severity_orders_within_a_verdict(self) -> None:
        # Fingerprints chosen so alphabetical order is the *opposite* of the
        # expected order. The first version named them "c" and "m", which sorted
        # into the right answer on their own, so zeroing severity entirely still
        # passed -- the tie-break was doing the work the test claimed to check.
        rows = [
            Judgement(
                make_finding(severity=Severity.MEDIUM, fingerprint="aaa"),
                Verdict.TRUE_POSITIVE,
                Source.MODEL,
                "",
            ),
            Judgement(
                make_finding(severity=Severity.CRITICAL, fingerprint="zzz"),
                Verdict.TRUE_POSITIVE,
                Source.MODEL,
                "",
            ),
        ]
        assert [j.finding.fingerprint for j in TriageRun(judgements=rows).ranked] == ["zzz", "aaa"]

    def test_confidence_breaks_a_severity_tie(self) -> None:
        rows = [
            Judgement(
                make_finding(confidence=Confidence.TENTATIVE, fingerprint="aaa"),
                Verdict.TRUE_POSITIVE,
                Source.MODEL,
                "",
            ),
            Judgement(
                make_finding(confidence=Confidence.CERTAIN, fingerprint="zzz"),
                Verdict.TRUE_POSITIVE,
                Source.MODEL,
                "",
            ),
        ]
        assert [j.finding.fingerprint for j in TriageRun(judgements=rows).ranked] == ["zzz", "aaa"]

    def test_the_order_does_not_move_between_runs(self) -> None:
        rows = [
            Judgement(make_finding(fingerprint=name), Verdict.TRUE_POSITIVE, Source.MODEL, "")
            for name in ("zz", "aa", "mm")
        ]
        first = [j.finding.fingerprint for j in TriageRun(judgements=rows).ranked]
        shuffled = TriageRun(judgements=list(reversed(rows)))
        assert [j.finding.fingerprint for j in shuffled.ranked] == first


class TestProvenance:
    def test_a_borrowed_verdict_says_how_much_evidence_it_rests_on(self) -> None:
        run = triage([make_finding(rule_id="DJP-001")], Refuses())
        reason = run.judgements[0].reason

        assert "46" in reason
        assert "DJP-001" in reason
        assert "true_positive" in reason

    def test_a_model_verdict_carries_the_model_s_own_reason(self) -> None:
        run = triage([make_finding()], Says("true_positive", reason="the loop refetches"))
        assert run.judgements[0].reason == "the loop refetches"

    def test_counting_is_by_verdict_not_by_source(self) -> None:
        run = triage(
            [
                make_finding(rule_id="DJP-001", fingerprint="a"),
                make_finding(rule_id="DJP-004", fingerprint="b"),
            ],
            Says("true_positive"),
        )
        assert run.counting(Verdict.TRUE_POSITIVE) == 2
        assert run.counting(Verdict.ACCEPTED_RISK) == 0


class TestTheFingerprintReachesTheCache:
    def test_a_cache_aware_provider_is_told_which_finding_it_is_answering(self) -> None:
        """Two identical questions about different findings must not share an answer."""
        seen: list[str] = []

        class Recording:
            def __init__(self, fingerprint: str = "") -> None:
                self.fingerprint = fingerprint

            @property
            def name(self) -> str:
                return "recording"

            def for_finding(self, fingerprint: str) -> Provider:
                return Recording(fingerprint)

            def ask(self, prompt: Prompt) -> Reply:
                seen.append(self.fingerprint)
                return Declined("no")

        triage(
            [make_finding(fingerprint="one"), make_finding(fingerprint="two")],
            Recording(),
        )
        assert seen == ["one", "two"]

    def test_a_provider_without_the_hook_is_used_unchanged(self) -> None:
        model = Says("true_positive")
        assert triage([make_finding()], model).asked == 1


class TestTheSchemaMatchesTheVerdicts:
    def test_every_choice_the_schema_offers_is_handled(self) -> None:
        """A choice the prompt allows but triage cannot map is a silent abstention."""
        choices = next(f.choices for f in TRIAGE_SCHEMA.fields if f.name == "verdict")

        mapped = {}
        for choice in choices:
            run = triage([make_finding()], Says(choice))
            mapped[choice] = run.judgements[0].verdict

        assert mapped == {
            "true_positive": Verdict.TRUE_POSITIVE,
            "accepted_risk": Verdict.ACCEPTED_RISK,
            "unsure": Verdict.ABSTAINED,
        }


@pytest.mark.parametrize("rule_id", sorted(CORPUS_PRIOR))
def test_the_prior_only_names_rules_that_exist(rule_id: str) -> None:
    from djaudit.registry import all_rules

    assert rule_id in {rule.meta.id for rule in all_rules()}


class TestTheGeneratorGate:
    """The gate is only a gate if it has been seen to fail."""

    def test_it_passes_on_the_shipped_constant(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "triage prior current" in result.stdout

    def test_it_fails_when_the_constant_no_longer_matches_the_corpus(self, tmp_path: Path) -> None:
        """Retriaging a finding must not leave a stale table suppressing questions."""
        corpus = tmp_path / "benchmarks"
        corpus.mkdir()
        # DJP-001 ships as unanimously true_positive over 46 findings. Here it
        # is contested, so the checker must refuse the shipped entry.
        corpus.joinpath("fake.json").write_text(
            json.dumps(
                {
                    "target": "fake",
                    "findings": [
                        {
                            "fingerprint": f"f{n}",
                            "rule_id": "DJP-001",
                            "verdict": "true_positive" if n else "accepted_risk",
                            "file": "a.py",
                            "line": n + 1,
                        }
                        for n in range(6)
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check", "--benchmarks", str(corpus)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "stale" in result.stderr
        assert "DJP-001" in result.stderr
