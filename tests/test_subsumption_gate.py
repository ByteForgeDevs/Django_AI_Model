"""The gate that makes a `SUBSUMED` claim answerable.

Marking an external rule code as subsumed deletes every finding that code would
have produced, on the strength of an assertion that one of our rules covers the
same ground. Nothing else in the project can check that assertion: a precision
gate only ever sees findings that were made, and the whole problem here is
findings that were not.

These tests are about the checker rather than the claim. That it reports a
location our rule stopped covering, that it refuses to run when there is
nothing to check, that it reads a rule's grouped evidence rather than only its
headline location, and that a recorded expectation which no longer matches
reality fails in both directions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)

ROOT = Path(__file__).resolve().parent.parent


def load() -> Any:
    """Import the script by path, the way the other gate tests do."""
    path = ROOT / "scripts" / "check_subsumption.py"
    spec = importlib.util.spec_from_file_location("check_subsumption", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = load()


def residual(code: str = "DJ001", reported: int = 3, unmatched: tuple[str, ...] = ()) -> Any:
    return GATE.Residual(code=code, ours=("DJD-002",), reported=reported, unmatched=unmatched)


def finding(rule_id: str, file: str, line: int, evidence: str = "") -> Finding:
    return Finding(
        rule_id=rule_id,
        title="t",
        family=Family.DJD,
        severity=Severity.MEDIUM,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        location=Location(file=file, line=line),
        message="m",
        rationale="r",
        remediation="fix",
        evidence=(Evidence(kind=EvidenceKind.AST, content=evidence, source=file),)
        if evidence
        else (),
    )


class TestWhereTheRuleActuallyPoints:
    """A rule that groups by model still covers every column it grouped."""

    def test_a_rule_covers_the_line_it_names(self) -> None:
        covered = GATE.locations_of([finding("DJD-002", "app/models.py", 12)], ("DJD-002",))
        assert covered == {("app/models.py", 12)}

    def test_it_also_covers_the_lines_it_only_listed_as_evidence(self) -> None:
        # `DJD-002` reports once per model and names the first offending column,
        # listing the rest in evidence. Reading only the headline would report
        # fourteen of pretix's fifteen Invoice columns as uncovered, and the
        # shortfall would be an artefact of this script rather than a fact.
        covered = GATE.locations_of(
            [
                finding(
                    "DJD-002",
                    "app/models.py",
                    12,
                    "a = models.CharField(..., null=True)  # line 12\n"
                    "b = models.CharField(..., null=True)  # line 19",
                )
            ],
            ("DJD-002",),
        )
        assert covered == {("app/models.py", 12), ("app/models.py", 19)}

    def test_another_rule_s_findings_are_not_borrowed(self) -> None:
        # The claim names the rule that covers the ground. Counting any finding
        # at that line would let an unrelated rule prove a claim it has nothing
        # to do with.
        covered = GATE.locations_of([finding("DJS-001", "app/models.py", 12)], ("DJD-002",))
        assert covered == set()

    def test_evidence_that_names_no_line_is_not_read_as_one(self) -> None:
        covered = GATE.locations_of(
            [finding("DJD-002", "app/models.py", 12, "a = models.CharField(null=True)")],
            ("DJD-002",),
        )
        assert covered == {("app/models.py", 12)}


class TestWhatMakesItFail:
    def test_a_location_our_rule_stopped_covering_is_named(self) -> None:
        # The regression this gate exists for, and the one it was proved
        # against: narrowing `DJD-002` by three columns of pretix produced
        # exactly this, where every other gate in the project stayed green.
        problems = GATE.compare(
            {"DJ001": residual(unmatched=("app/models.py:45",))},
            {"DJ001": residual()},
        )
        assert len(problems) == 1
        assert "app/models.py:45" in problems[0]
        assert "DJD-002" in problems[0]

    def test_a_location_that_became_covered_must_be_re_recorded(self) -> None:
        # Not a failure of the code, but the recorded file is now wrong, and a
        # stale expectation is what lets the next real regression through.
        (problem,) = GATE.compare(
            {"DJ001": residual()}, {"DJ001": residual(unmatched=("app/models.py:45",))}
        )
        assert "no longer is" in problem

    def test_the_tool_finding_a_different_number_is_a_failure_on_its_own(self) -> None:
        # A ruff upgrade that widens `DJ001` adds ground our rule never agreed
        # to cover. It shows up here even when every previously-recorded
        # location still behaves.
        (problem,) = GATE.compare({"DJ001": residual(reported=4)}, {"DJ001": residual(reported=3)})
        assert "reports 4 locations, recorded 3" in problem

    def test_repointing_a_claim_at_a_different_rule_is_a_failure(self) -> None:
        moved = GATE.Residual(code="DJ001", ours=("DJD-009",), reported=3, unmatched=())
        (problem,) = GATE.compare({"DJ001": moved}, {"DJ001": residual()})
        assert "claims DJD-009, recorded as DJD-002" in problem

    def test_a_new_subsumed_claim_cannot_arrive_unrecorded(self) -> None:
        (problem,) = GATE.compare({"DJ008": residual(code="DJ008")}, {})
        assert "never recorded" in problem

    def test_a_claim_that_stopped_being_subsumed_is_reported(self) -> None:
        (problem,) = GATE.compare({}, {"DJ001": residual()})
        assert "no longer a subsumed claim" in problem

    def test_agreement_is_silent(self) -> None:
        expected = residual(unmatched=("app/models.py:45",))
        assert GATE.compare({"DJ001": expected}, {"DJ001": expected}) == []


class TestWhatItRefusesToDo:
    def test_it_will_not_pass_by_having_nothing_to_check(self, monkeypatch: Any) -> None:
        # The failure mode that makes a gate worthless: empty tables compare
        # equal to an empty expectation and the run goes green while checking
        # nothing at all.
        monkeypatch.setattr(GATE, "TABLES", ())
        with pytest.raises(SystemExit, match="no subsumed claims"):
            GATE.measure(ROOT, "ruff")

    def test_it_will_not_pass_on_an_expectation_file_with_no_claims(self, tmp_path: Path) -> None:
        path = tmp_path / "x.json"
        path.write_text(json.dumps({"target": "x"}))
        with pytest.raises(SystemExit, match="no claims object"):
            GATE.read(path)

    def test_a_recorded_file_round_trips(self, tmp_path: Path) -> None:
        # The written form has to be the form the checker reads back, or the
        # first run after a re-record fails for no reason.
        original = residual(unmatched=("a.py:1", "b.py:2"))
        path = tmp_path / "x.json"
        path.write_text(json.dumps({"target": "x", "claims": {"DJ001": original.as_json()}}))
        assert GATE.read(path) == {"DJ001": original}

    def test_a_missing_count_cannot_read_as_a_matching_one(self, tmp_path: Path) -> None:
        # A truncated file must fail rather than default to whatever was
        # measured. `-1` is unreachable because a tool cannot report a negative
        # number of locations.
        path = tmp_path / "x.json"
        path.write_text(json.dumps({"target": "x", "claims": {"DJ001": {"ours": ["DJD-002"]}}}))
        (problem,) = GATE.compare({"DJ001": residual(reported=0)}, GATE.read(path))
        assert "recorded -1" in problem

    def test_the_subsumed_codes_it_checks_are_the_ones_actually_claimed(self) -> None:
        # Reading a table nobody uses would be a gate over a fiction. This ties
        # the script's TABLES to the adapter the run really loads.
        from djaudit.adapters.base import Claim
        from djaudit.adapters.ruff import CLAIMS

        assert CLAIMS in GATE.TABLES
        assert [c.code for c in CLAIMS.claims if c.claim is Claim.SUBSUMED] == ["DJ001"]


class TestTheRecordedMeasurements:
    """The checked-in files are evidence, so they are held to being evidence."""

    @pytest.mark.parametrize("target", ["healthchecks", "netbox", "pretix"])
    def test_every_target_has_one_and_it_names_itself(self, target: str) -> None:
        path = ROOT / "benchmarks" / "subsumption" / f"{target}.json"
        document = json.loads(path.read_text())
        assert document["target"] == target

    @pytest.mark.parametrize("target", ["healthchecks", "netbox", "pretix"])
    def test_the_arithmetic_holds(self, target: str) -> None:
        # matched + unmatched == reported, or the file is not describing a
        # partition of anything and the counts mean nothing.
        path = ROOT / "benchmarks" / "subsumption" / f"{target}.json"
        for entry in json.loads(path.read_text())["claims"].values():
            assert entry["matched"] + len(entry["unmatched"]) == entry["reported"]

    def test_the_corpus_exercises_the_matched_side_somewhere(self) -> None:
        # An expectation where nothing is ever matched would be satisfied by a
        # rule that reports nothing, and the gate would be measuring silence.
        total = 0
        for target in ("healthchecks", "netbox", "pretix"):
            path = ROOT / "benchmarks" / "subsumption" / f"{target}.json"
            total += sum(e["matched"] for e in json.loads(path.read_text())["claims"].values())
        assert total > 0
