"""What the harness must refuse to tell you.

The distribution is the whole reason this module exists in the shape it does:
194 of the 252 verdicts are true positives, so a classifier that reads nothing
and says "true positive" scores 77.0%. Most of these tests are therefore about
what cannot be claimed -- that there is no combined number, that recall will not
answer without being told which class, that abstaining is not free, and that a
model which trades one error for the other does not read as an improvement.

The ground truth is loaded from `benchmarks/` rather than mocked, because a
harness tested only against a fixture is a harness that has never met the data
it exists to score.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from djaudit.llm.evaluate import (
    AlwaysSays,
    ByRule,
    ReviewedFinding,
    Score,
    Triager,
    Verdict,
    baselines,
    contested_rules,
    headroom,
    held_out_by_rule,
    load_ground_truth,
    score,
)

BENCHMARKS = Path(__file__).resolve().parents[2] / "benchmarks"


@pytest.fixture(scope="module")
def truth() -> list[ReviewedFinding]:
    return load_ground_truth(BENCHMARKS)


def finding(verdict: Verdict, *, rule: str = "DJS-001", fp: str = "a") -> ReviewedFinding:
    return ReviewedFinding(
        fingerprint=fp,
        rule_id=rule,
        verdict=verdict,
        file="settings.py",
        line=1,
        note="",
        target="t",
    )


@dataclass(frozen=True)
class Perfect:
    """Cheats by reading the answer. Used to prove the harness can see a
    perfect score at all -- a scorer that reported 0.0 for everything would
    pass every test about what it refuses to say."""

    def classify(self, finding: ReviewedFinding) -> Verdict:
        return finding.verdict


@dataclass(frozen=True)
class Inverted:
    def classify(self, finding: ReviewedFinding) -> Verdict:
        return Verdict.ACCEPTED_RISK if finding.is_true_positive else Verdict.TRUE_POSITIVE


class TestTheGroundTruth:
    def test_it_loads_every_reviewed_finding(self, truth: list[ReviewedFinding]) -> None:
        """Cross-checked against the raw files, not against a recorded total.

        The loader's failure mode is dropping or duplicating entries, and
        counting the JSON directly reaches the same number by a different path,
        so the check stays load-bearing while the corpus grows. A literal here
        only ever meant "the corpus was this size the day someone looked".
        """
        on_disk = sum(
            len(json.loads(path.read_text())["findings"]) for path in BENCHMARKS.glob("*.json")
        )

        assert on_disk
        assert len(truth) == on_disk

    def test_the_distribution_is_the_one_the_design_assumes(
        self, truth: list[ReviewedFinding]
    ) -> None:
        """If this changes, the majority baseline moves and the argument for
        splitting the directions has to be re-made rather than inherited."""
        true_positives = sum(1 for f in truth if f.is_true_positive)
        accepted = sum(1 for f in truth if not f.is_true_positive)

        assert true_positives == 196
        assert accepted == 63

    def test_every_finding_carries_its_target(self, truth: list[ReviewedFinding]) -> None:
        assert {f.target for f in truth} == {"healthchecks", "netbox", "pretix"}

    def test_fingerprints_are_unique(self, truth: list[ReviewedFinding]) -> None:
        """Scoring the same finding twice would weight it twice."""
        pairs = {(f.target, f.fingerprint) for f in truth}

        assert len(pairs) == len(truth)

    def test_an_empty_directory_is_an_error_not_an_empty_score(self, tmp_path: Path) -> None:
        """A harness that silently scores nothing reports 0.0 and looks like a
        failing model rather than a missing corpus."""
        with pytest.raises(ValueError, match="no ground truth"):
            load_ground_truth(tmp_path)


class TestTheMajorityBaseline:
    """The number every later claim in this phase has to clear."""

    def test_saying_true_positive_to_everything_scores_seventy_nine_percent(
        self, truth: list[ReviewedFinding]
    ) -> None:
        result = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="majority")

        assert result.recall(Verdict.TRUE_POSITIVE) == 1.0
        assert result.recall(Verdict.ACCEPTED_RISK) == 0.0
        assert result.matrix[Verdict.TRUE_POSITIVE][Verdict.TRUE_POSITIVE] == 196

    def test_the_majority_baseline_makes_every_possible_upgrade_error(
        self, truth: list[ReviewedFinding]
    ) -> None:
        """Every accepted risk is upgraded, which is the claim in the name.

        Expressed against the accepted-risk population rather than a literal,
        because "every possible" is a relationship to that population and a
        pinned count silently stops asserting it as the corpus grows.
        """
        accepted = sum(1 for f in truth if not f.is_true_positive)
        result = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="majority")

        assert accepted
        assert result.upgrades == accepted
        assert result.downgrades == 0

    def test_saying_accepted_risk_to_everything_is_the_mirror(
        self, truth: list[ReviewedFinding]
    ) -> None:
        """And it is the dangerous mirror: 194 real defects dismissed."""
        result = score(AlwaysSays(Verdict.ACCEPTED_RISK), truth, name="lenient")

        assert result.downgrades == 196
        assert result.upgrades == 0

    def test_the_by_rule_baseline_beats_the_majority_one(
        self, truth: list[ReviewedFinding]
    ) -> None:
        """A real bar, not a straw one. Rule identity alone carries signal, so
        a model that only reaches the majority baseline has added nothing."""
        majority = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="majority")
        by_rule = score(ByRule.learned_from(truth), truth, name="by-rule")

        assert by_rule.recall(Verdict.ACCEPTED_RISK) > majority.recall(Verdict.ACCEPTED_RISK)


class TestThereIsNoHeadlineNumber:
    def test_the_score_exposes_no_combined_accuracy(self) -> None:
        """Structural, not documentary. There is nothing to reach for."""
        result = Score.empty("x")

        assert not hasattr(result, "accuracy")
        assert not hasattr(result, "f1")

    def test_recall_will_not_answer_without_a_class(self) -> None:
        result = Score.empty("x")

        with pytest.raises(TypeError):
            result.recall()  # type: ignore[call-arg]

    def test_the_rendered_report_carries_both_directions(
        self, truth: list[ReviewedFinding]
    ) -> None:
        rendered = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="m").as_dict()

        assert "true_positives" in rendered
        assert "accepted_risks" in rendered
        assert "downgraded_to_accepted_risk" in rendered["true_positives"]  # type: ignore[operator]
        assert "upgraded_to_true_positive" in rendered["accepted_risks"]  # type: ignore[operator]


class TestTradingOneErrorForTheOtherIsNotProgress:
    def test_a_model_that_wins_both_directions_beats_the_baseline(
        self, truth: list[ReviewedFinding]
    ) -> None:
        majority = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="majority")
        perfect = score(Perfect(), truth, name="perfect")

        assert perfect.beats(majority)

    def test_a_model_that_only_trades_does_not(self, truth: list[ReviewedFinding]) -> None:
        """Better on accepted risks, worse on true positives. Moved, not improved."""
        majority = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="majority")
        lenient = score(AlwaysSays(Verdict.ACCEPTED_RISK), truth, name="lenient")

        assert lenient.recall(Verdict.ACCEPTED_RISK) > majority.recall(Verdict.ACCEPTED_RISK)
        assert not lenient.beats(majority)

    def test_matching_the_baseline_exactly_does_not_beat_it(
        self, truth: list[ReviewedFinding]
    ) -> None:
        majority = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="majority")
        same = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="copy")

        assert not same.beats(majority)

    def test_the_inverted_model_beats_nothing(self, truth: list[ReviewedFinding]) -> None:
        majority = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="majority")
        inverted = score(Inverted(), truth, name="inverted")

        assert not inverted.beats(majority)

    def test_a_perfect_score_is_reachable(self, truth: list[ReviewedFinding]) -> None:
        """The contrast. A scorer that returned 0.0 for everything would satisfy
        every refusal test above and measure nothing."""
        perfect = score(Perfect(), truth, name="perfect")

        assert perfect.recall(Verdict.TRUE_POSITIVE) == 1.0
        assert perfect.recall(Verdict.ACCEPTED_RISK) == 1.0
        assert perfect.downgrades == 0
        assert perfect.upgrades == 0


class TestAbstainingIsNotFree:
    def test_abstaining_on_everything_scores_zero_on_both(
        self, truth: list[ReviewedFinding]
    ) -> None:
        """Not "no data". A model that will not answer has not done the job."""
        result = score(AlwaysSays(Verdict.ABSTAINED), truth, name="silent")

        assert result.recall(Verdict.TRUE_POSITIVE) == 0.0
        assert result.recall(Verdict.ACCEPTED_RISK) == 0.0

    def test_abstaining_on_everything_beats_nothing(self, truth: list[ReviewedFinding]) -> None:
        majority = score(AlwaysSays(Verdict.TRUE_POSITIVE), truth, name="majority")
        silent = score(AlwaysSays(Verdict.ABSTAINED), truth, name="silent")

        assert not silent.beats(majority)

    def test_abstentions_are_visible(self, truth: list[ReviewedFinding]) -> None:
        result = score(AlwaysSays(Verdict.ABSTAINED), truth, name="silent")

        assert result.abstentions() == len(truth)
        assert result.coverage() == 0.0

    def test_selective_abstention_cannot_inflate_recall(self) -> None:
        """The failure this guards.

        A model that answers only the easy half and abstains on the rest would
        post a perfect score on what it answered, if recall were computed over
        answers. It is computed over what a human reviewed, so declining to
        answer costs exactly as much as answering wrong.
        """

        @dataclass(frozen=True)
        class OnlyEasyOnes:
            def classify(self, f: ReviewedFinding) -> Verdict:
                return f.verdict if f.rule_id == "DJS-001" else Verdict.ABSTAINED

        findings = [
            finding(Verdict.TRUE_POSITIVE, rule="DJS-001", fp="a"),
            finding(Verdict.TRUE_POSITIVE, rule="DJP-004", fp="b"),
            finding(Verdict.TRUE_POSITIVE, rule="DJP-004", fp="c"),
        ]

        result = score(OnlyEasyOnes(), findings, name="picky")

        assert result.recall(Verdict.TRUE_POSITIVE) == pytest.approx(1 / 3)
        assert result.coverage() == pytest.approx(1 / 3)


class TestTheHarnessDoesNotLeakTheAnswer:
    def test_the_triager_protocol_is_satisfied_without_reading_the_note(self) -> None:
        """The notes explain each verdict in prose. Anything given the note is
        not being tested on judgement."""

        @dataclass(frozen=True)
        class Blind:
            seen: list[str]

            def classify(self, f: ReviewedFinding) -> Verdict:
                self.seen.append(f.note)
                return Verdict.TRUE_POSITIVE

        seen: list[str] = []
        blind: Triager = Blind(seen)
        score(blind, [finding(Verdict.TRUE_POSITIVE)], name="blind")

        # The field exists on the record -- a prompt builder must simply never
        # put it in a prompt, which is asserted where prompts are built.
        assert seen == [""]


class TestHeldOutIsTheHonestNumber:
    """The gap between the two by-rule baselines is the point.

    Fitted on its own test set the rule lookup reaches 87.3% on accepted risks.
    Held out it reaches 52.4%. Reporting the first as "a lookup table already
    does this well" would have been a measurement of memorisation.
    """

    def test_held_out_scores_every_finding(self, truth: list[ReviewedFinding]) -> None:
        result = held_out_by_rule(truth)

        assert truth
        assert sum(result.total(actual) for actual in result.matrix) == len(truth)

    def test_held_out_is_worse_than_fitted_on_accepted_risks(
        self, truth: list[ReviewedFinding]
    ) -> None:
        fitted = score(ByRule.learned_from(truth), truth, name="fitted")
        honest = held_out_by_rule(truth)

        assert honest.recall(Verdict.ACCEPTED_RISK) < fitted.recall(Verdict.ACCEPTED_RISK)

    def test_the_honest_number_is_about_half(self, truth: list[ReviewedFinding]) -> None:
        """Pinned, because this is the figure the phase's argument rests on.

        It has now moved twice, in opposite directions, and both movements are
        understood rather than drift -- which is the only kind of movement a
        pinned figure should be allowed.

        `DJM-002` took it from 26/51 to 33/58: seven findings, all reviewed
        `accepted_risk`, spread over two targets. A rule that appears in more
        than one target is in the training table for every fold that scores it,
        and a rule whose verdicts are unanimous is one the lookup gets right.

        `DJM-003` then took it from 33/58 to 33/63 -- five more accepted risks
        and not one of them predicted. Its findings are all netbox, so it is
        trained on for the two folds that have none of it to score and missing
        from the one that scores all five. See
        `test_a_single_target_rule_is_confidently_wrong_in_its_own_fold`,
        which holds the mechanism rather than leaving it as a comment here.
        """
        honest = held_out_by_rule(truth)

        assert honest.recall(Verdict.ACCEPTED_RISK) == pytest.approx(33 / 63, abs=0.005)
        assert honest.recall(Verdict.TRUE_POSITIVE) == pytest.approx(190 / 194, abs=0.005)

    def test_a_single_target_rule_is_confidently_wrong_in_its_own_fold(
        self, truth: list[ReviewedFinding]
    ) -> None:
        """Why a lenient rule can *lower* the held-out number by landing.

        Leave-one-target-out cannot see a rule that fires on one target: the
        fold that scores its findings is the one fold trained without it. What
        makes that cost real rather than merely absent is `ByRule`'s fallback --
        an unseen rule id is answered `TRUE_POSITIVE`, not abstained on -- so
        every finding of a single-target *lenient* rule is scored as a wrong
        verdict.

        Asserting the fallback is the point, though it is worth being exact
        about how much this adds: a baseline that abstained on unseen rules
        would score an *identical* accepted-risk recall, the very figure the
        test above pins as the honest number, and would be caught only by the
        true-positive recall alongside it (190/194 becomes 168/194). So the
        headline number cannot see the difference. This test can, and it names
        the rule responsible instead of moving a ratio by an unexplained
        amount.
        """
        targets: dict[str, set[str]] = {}
        for finding in truth:
            targets.setdefault(finding.rule_id, set()).add(finding.target)
        lenient = {
            rule
            for rule, seen in targets.items()
            if len(seen) == 1 and all(not f.is_true_positive for f in truth if f.rule_id == rule)
        }

        assert lenient, "no single-target lenient rule left; this test now proves nothing"

        for rule in lenient:
            (target,) = targets[rule]
            train = [f for f in truth if f.target != target]
            assert rule not in {f.rule_id for f in train}, rule
            predictions = {
                ByRule.learned_from(train).classify(f) for f in truth if f.rule_id == rule
            }
            assert predictions == {Verdict.TRUE_POSITIVE}, rule

    def test_folds_are_pooled_not_averaged(self, truth: list[ReviewedFinding]) -> None:
        """Pretix has 141 findings and healthchecks 33. Averaging the three
        fold scores would give them equal say."""
        honest = held_out_by_rule(truth)
        per_fold = []
        for held in sorted({f.target for f in truth}):
            train = [f for f in truth if f.target != held]
            test = [f for f in truth if f.target == held]
            per_fold.append(score(ByRule.learned_from(train), test, name=held))
        mean = sum(f.recall(Verdict.ACCEPTED_RISK) for f in per_fold) / len(per_fold)

        assert honest.recall(Verdict.ACCEPTED_RISK) != pytest.approx(mean, abs=0.001)


class TestTheContestedSet:
    """Where a model can help, and where it can only do harm."""

    def test_most_rules_are_unanimous(self, truth: list[ReviewedFinding]) -> None:
        contested = contested_rules(truth)
        rules = {f.rule_id for f in truth}

        assert len(contested) == 7
        assert len(rules) == 32

    def test_the_contested_rules_are_the_ones_with_both_verdicts(
        self, truth: list[ReviewedFinding]
    ) -> None:
        for rule in contested_rules(truth):
            verdicts = {f.verdict for f in truth if f.rule_id == rule}
            assert verdicts == {Verdict.TRUE_POSITIVE, Verdict.ACCEPTED_RISK}, rule

    def test_an_unanimous_rule_is_not_contested(self, truth: list[ReviewedFinding]) -> None:
        """The contrast: a function returning every rule would pass the test
        above vacuously."""
        contested = contested_rules(truth)
        uncontested = {f.rule_id for f in truth} - contested

        assert uncontested
        for rule in uncontested:
            assert len({f.verdict for f in truth if f.rule_id == rule}) == 1, rule

    def test_headroom_counts_findings_not_rules(self, truth: list[ReviewedFinding]) -> None:
        affected, total = headroom(truth)

        assert affected == 110
        assert total == len(truth)

    def test_a_single_verdict_corpus_has_no_contested_rules(self) -> None:
        findings = [
            finding(Verdict.TRUE_POSITIVE, rule="DJS-001", fp="a"),
            finding(Verdict.TRUE_POSITIVE, rule="DJS-002", fp="b"),
        ]

        assert contested_rules(findings) == frozenset()
        assert headroom(findings) == (0, 2)


class TestTheGateFailsOnItsDefect:
    """A gate never shown failing is a gate nobody knows the shape of."""

    def test_it_passes_on_the_real_corpus(self) -> None:
        result = subprocess.run(
            [sys.executable, str(BENCHMARKS.parent / "scripts" / "triage_baselines.py")],
            capture_output=True,
            text=True,
            check=False,
            cwd=BENCHMARKS.parent,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "by-rule (held out)" in result.stdout

    def test_it_fails_when_every_rule_is_unanimous(self, tmp_path: Path) -> None:
        """A corpus decided entirely by rule id leaves a model nothing to do,
        and must not read as a healthy benchmark."""
        corpus = tmp_path / "benchmarks"
        corpus.mkdir()
        entries = [
            {
                "fingerprint": f"f{i}",
                "rule_id": "DJS-001" if i % 2 else "DJS-002",
                "verdict": "true_positive" if i % 2 else "accepted_risk",
                "file": "settings.py",
                "line": i,
                "note": "",
            }
            for i in range(40)
        ]
        (corpus / "fake.json").write_text(
            json.dumps({"target": "fake", "findings": entries}), encoding="utf-8"
        )
        script = (BENCHMARKS.parent / "scripts" / "triage_baselines.py").read_text()
        patched = tmp_path / "gate.py"
        patched.write_text(
            script.replace(
                "ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(tmp_path)!r})"
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(patched)],
            capture_output=True,
            text=True,
            check=False,
            cwd=BENCHMARKS.parent,
        )

        assert result.returncode == 1, result.stdout
        assert "nothing left for a model to do" in result.stdout

    def test_it_fails_when_a_blind_baseline_solves_the_corpus(self, tmp_path: Path) -> None:
        """Two rules, each unanimous, in both directions: the held-out lookup
        cannot learn them from one target, but with two targets it can, and
        then the corpus is measuring nothing."""
        corpus = tmp_path / "benchmarks"
        corpus.mkdir()
        for target in ("a", "b"):
            entries = [
                {
                    "fingerprint": f"{target}{i}",
                    "rule_id": "DJS-001" if i % 2 else "DJS-002",
                    "verdict": "true_positive" if i % 2 else "accepted_risk",
                    "file": "s.py",
                    "line": i,
                    "note": "",
                }
                for i in range(40)
            ]
            (corpus / f"{target}.json").write_text(
                json.dumps({"target": target, "findings": entries}), encoding="utf-8"
            )
        script = (BENCHMARKS.parent / "scripts" / "triage_baselines.py").read_text()
        patched = tmp_path / "gate.py"
        patched.write_text(
            script.replace(
                "ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(tmp_path)!r})"
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(patched)],
            capture_output=True,
            text=True,
            check=False,
            cwd=BENCHMARKS.parent,
        )

        assert result.returncode == 1, result.stdout
        assert "no longer discriminates" in result.stdout


class TestBaselineTable:
    def test_all_four_baselines_are_produced(self, truth: list[ReviewedFinding]) -> None:
        names = {b.name for b in baselines(truth)}

        assert names == {
            "always-true-positive",
            "always-accepted-risk",
            "always-abstain",
            "by-rule",
            "by-rule (held out)",
        }

    def test_every_baseline_scores_every_reviewed_finding(
        self, truth: list[ReviewedFinding]
    ) -> None:
        """Conservation: a baseline must place each finding in exactly one cell.

        Checked against the size of its own input rather than a recorded corpus
        total. The property is that nothing is dropped or double-counted, and
        pinning a literal expressed that only for as long as the corpus stood
        still -- it broke whenever an unrelated family gained a rule, which
        taught nothing about scoring. The `truth` guard keeps the check from
        passing vacuously on an empty load.
        """
        assert truth

        for result in baselines(truth):
            counted = sum(result.total(actual) for actual in result.matrix)
            assert counted == len(truth), result.name

    def test_no_baseline_is_perfect(self, truth: list[ReviewedFinding]) -> None:
        """If one were, the corpus would not be measuring anything."""
        for result in baselines(truth):
            perfect = (
                result.recall(Verdict.TRUE_POSITIVE) == 1.0
                and result.recall(Verdict.ACCEPTED_RISK) == 1.0
            )
            assert not perfect, result.name
