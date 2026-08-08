"""Provenance labelling, in output and in SARIF.

The property under test is not "labels are present" but "labels are true", and
those come apart in a specific way this code got wrong once: a single label per
result meant an offline run marked twenty-four deterministic findings
`unavailable` and `reproducible: false`, because the *verdict* about them was
unavailable. A consumer believing that would have thrown away every one of
them. The tests below pin the two statements apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import djaudit.rules  # noqa: F401  -- registers every rule
from djaudit import cli, engine
from djaudit.llm.evaluate import Verdict
from djaudit.llm.triage import Judgement, Source, TriageRun, provenance_of, verdicts_for
from djaudit.models import Confidence, Family, Finding, Location, Severity, Tier
from djaudit.provenance import (
    DETERMINISTIC,
    Authorship,
    Provenance,
    describe,
)
from djaudit.provenance import Verdict as LabelledVerdict
from djaudit.reporters import json_reporter, sarif

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "vulnerable_project"


def make_finding(rule_id: str = "DJS-001") -> Finding:
    return Finding(
        rule_id=rule_id,
        title="a title",
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        family=Family.DJS,
        tier=Tier.STATIC,
        location=Location(file="settings.py", line=1, snippet="DEBUG = True"),
        message="a message",
        rationale="a rationale",
        remediation="a remediation",
        fingerprint=f"fp-{rule_id}",
    )


class TestTheVocabulary:
    def test_only_a_model_may_be_named(self) -> None:
        """Naming a model on a corpus hit would imply it was consulted."""
        with pytest.raises(ValueError, match="only MODEL may"):
            Provenance(authorship=Authorship.CORPUS, model="gpt-9")
        with pytest.raises(ValueError, match="only MODEL may"):
            Provenance(authorship=Authorship.DETERMINISTIC, model="gpt-9")
        assert Provenance(authorship=Authorship.MODEL, model="gpt-9").model == "gpt-9"

    def test_reproducible_is_not_the_same_as_deterministic(self) -> None:
        """A corpus hit reproduces without being a rule's output."""
        corpus = Provenance(authorship=Authorship.CORPUS)
        assert corpus.reproducible
        assert not corpus.deterministic
        assert DETERMINISTIC.reproducible and DETERMINISTIC.deterministic

    def test_a_model_answer_is_not_reproducible(self) -> None:
        assert not Provenance(authorship=Authorship.MODEL, model="m").reproducible
        assert not Provenance(authorship=Authorship.UNAVAILABLE).reproducible

    def test_an_absent_model_is_omitted_not_emitted_empty(self) -> None:
        """So a consumer can test presence without also testing for ''."""
        assert "model" not in DETERMINISTIC.as_properties()
        assert "detail" not in Provenance(authorship=Authorship.MODEL).as_properties()

    def test_every_authorship_describes_itself(self) -> None:
        """Distinctness is not enough -- four wrong sentences are also four.

        Each rendering is pinned to its meaning, so a description that swaps
        "recorded human triage" for "model" fails even though the set of
        strings stays the same size.
        """
        expected = {
            Authorship.DETERMINISTIC: "deterministic rule",
            Authorship.CORPUS: "recorded human triage",
            Authorship.UNAVAILABLE: "no model answered",
            Authorship.MODEL: "model some-model",
        }
        assert set(expected) == set(Authorship), "an authorship has no description"
        for authorship, sentence in expected.items():
            named = "some-model" if authorship is Authorship.MODEL else ""
            assert describe(Provenance(authorship=authorship, model=named)) == sentence

    def test_an_unnamed_model_still_says_model(self) -> None:
        assert describe(Provenance(authorship=Authorship.MODEL)) == "model"

    def test_a_labelled_verdict_carries_its_label(self) -> None:
        """Dropping the label leaves a provenance attached to nothing."""
        payload = LabelledVerdict(
            label="true_positive",
            provenance=Provenance(authorship=Authorship.CORPUS),
        ).as_properties()
        assert payload["verdict"] == "true_positive"
        assert payload["provenance"]["authorship"] == "corpus"


class TestFindingsAreAlwaysDeterministic:
    def test_sarif_labels_every_result_even_with_no_triage(self) -> None:
        """Absence of a label must never be readable as a claim."""
        result = engine.run(FIXTURE, min_confidence=Confidence.TENTATIVE)
        document = sarif.build(result)
        assert document["runs"][0]["results"], "the fixture must produce findings"
        for entry in document["runs"][0]["results"]:
            assert entry["properties"]["provenance"]["authorship"] == "deterministic"
            assert "triage" not in entry["properties"]

    def test_json_labels_every_finding_even_with_no_triage(self) -> None:
        result = engine.run(FIXTURE, min_confidence=Confidence.TENTATIVE)
        document = json_reporter.build(result)
        assert document["findings"]
        for entry in document["findings"]:
            assert entry["provenance"]["authorship"] == "deterministic"
            assert "triage" not in entry

    def test_an_unavailable_verdict_does_not_taint_its_finding(self) -> None:
        """The defect that motivated splitting the two statements.

        An offline run has no verdict for anything. That says nothing at all
        about the findings, which came from AST rules and will reproduce.
        """
        result = engine.run(FIXTURE, min_confidence=Confidence.TENTATIVE)
        unavailable = LabelledVerdict(
            label="abstained",
            provenance=Provenance(authorship=Authorship.UNAVAILABLE, detail="offline"),
        )
        labels = {f.fingerprint: unavailable for f in result.findings}

        for entry in sarif.build(result, labels)["runs"][0]["results"]:
            assert entry["properties"]["provenance"]["authorship"] == "deterministic"
            assert entry["properties"]["provenance"]["reproducible"] is True
            assert entry["properties"]["triage"]["verdict"] == "abstained"
            assert entry["properties"]["triage"]["provenance"]["authorship"] == "unavailable"
            assert entry["properties"]["triage"]["provenance"]["reproducible"] is False


class TestVerdictsCarryTheirOrigin:
    def _judged(self, source: Source, reason: str = "a reason") -> Judgement:
        return Judgement(
            finding=make_finding(),
            verdict=Verdict.TRUE_POSITIVE,
            source=source,
            reason=reason,
        )

    def test_each_source_maps_to_its_own_authorship(self) -> None:
        seen = {provenance_of(self._judged(s)).authorship for s in Source}
        assert len(seen) == len(Source), "two sources collapsed onto one label"

    def test_a_model_verdict_names_the_model(self) -> None:
        labelled = provenance_of(self._judged(Source.MODEL), model="some-model")
        assert labelled.authorship is Authorship.MODEL
        assert labelled.model == "some-model"

    def test_a_corpus_verdict_never_names_a_model(self) -> None:
        """Even when one was configured and answered other findings."""
        labelled = provenance_of(self._judged(Source.CORPUS), model="some-model")
        assert labelled.model == ""
        assert labelled.as_properties().get("model") is None

    def test_the_reason_is_carried_through(self) -> None:
        labelled = provenance_of(self._judged(Source.CORPUS, "3 projects agreed"))
        assert labelled.detail == "3 projects agreed"

    def test_the_verdict_label_survives_the_trip(self) -> None:
        run = TriageRun(judgements=[self._judged(Source.CORPUS)])
        assert verdicts_for(run)["fp-DJS-001"].label == "true_positive"

    def test_a_finding_without_a_fingerprint_is_skipped_not_keyed_on_empty(self) -> None:
        """Keying on '' would collapse every unfingerprinted finding onto one."""
        blank = Finding(
            rule_id="DJS-001",
            title="t",
            severity=Severity.LOW,
            confidence=Confidence.FIRM,
            family=Family.DJS,
            tier=Tier.STATIC,
            location=Location(file="a.py", line=1),
            message="m",
            rationale="r",
            remediation="r",
            fingerprint="",
        )
        assert blank.fingerprint == "", "the fixture must actually lack one"
        run = TriageRun(
            judgements=[
                Judgement(blank, Verdict.TRUE_POSITIVE, Source.CORPUS, "x"),
                self._judged(Source.CORPUS),
            ]
        )
        assert "" not in verdicts_for(run)
        assert len(verdicts_for(run)) == 1


class TestThroughTheCli:
    def _triage(self, *extra: str) -> dict[str, object]:
        result = CliRunner().invoke(cli.app, ["triage", str(FIXTURE), *extra])
        assert result.exit_code == 0, result.output
        return dict(json.loads(result.stdout))

    def test_sarif_output_carries_both_statements(self) -> None:
        entries = self._triage("--format", "sarif")["runs"]
        assert isinstance(entries, list)
        results = entries[0]["results"]
        assert results
        for entry in results:
            assert entry["properties"]["provenance"]["authorship"] == "deterministic"
            triage = entry["properties"]["triage"]
            assert triage["verdict"] in {"true_positive", "accepted_risk", "abstained"}
            assert triage["provenance"]["authorship"] in {"corpus", "model", "unavailable"}

    def test_json_output_carries_both_statements(self) -> None:
        findings = self._triage("--format", "json")["findings"]
        assert isinstance(findings, list)
        assert findings
        for entry in findings:
            assert entry["provenance"]["authorship"] == "deterministic"
            assert entry["triage"]["verdict"] in {
                "true_positive",
                "accepted_risk",
                "abstained",
            }

    def test_an_offline_run_names_no_model_anywhere(self) -> None:
        """The default path must not imply an involvement that did not happen."""
        document = self._triage("--format", "json")
        # The `model` *key* is what would name one; the word appears innocently
        # in prose ("no model was consulted"), so match the key exactly.
        assert '"model":' not in json.dumps(document)
        findings = document["findings"]
        assert isinstance(findings, list)
        for entry in findings:
            assert entry["triage"]["provenance"]["authorship"] == "unavailable"

    def test_the_sarif_still_validates_as_sarif(self) -> None:
        """Adding properties must not break the shape consumers rely on."""
        document = self._triage("--format", "sarif")
        assert document["version"] == "2.1.0"
        run = document["runs"][0]  # type: ignore[index]
        rules = run["tool"]["driver"]["rules"]
        for entry in run["results"]:
            assert rules[entry["ruleIndex"]]["id"] == entry["ruleId"]
