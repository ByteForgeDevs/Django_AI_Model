"""The adapters note's gate, checked against defects it must not miss.

`scripts/check_adapters_doc.py` exists because none of the note's claims break
a test when they change. A count of claims in a table, which verdict a code
carries, which adapter reaches the network -- all of them can drift silently,
and a note nobody checks is worse than no note, because it is believed.

That makes the gate itself load-bearing, so each check here is given the defect
it was written to catch. Two of these are regressions rather than hypotheses.
The first draft compared the claim table's subsumed pair by asking whether both
names appeared *somewhere* in the note, and the note states that pairing twice
-- so rewriting one of the two to name a different rule left the note
contradicting itself and the gate green. The second draft looks at every place
the external code is mentioned.

The defects come in two directions, and both matter. The note can be edited
away from the code, which is the common case; and the code can move under a
note nobody touched, which is the case that actually happens during a refactor.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from djaudit import adapters
from djaudit.adapters.base import Adapter, Availability, Claim, Claimed, ClaimTable, Report
from djaudit.adapters.pip_audit import ARGUMENTS
from djaudit.adapters.ruff import CLAIMS as RUFF
from djaudit.models import Confidence, Family, Severity

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_adapters_doc  # noqa: E402

NOTE = ROOT / "docs/architecture/adapters.md"


@pytest.fixture
def note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable copy of the note, so a defect can be injected without editing it.

    `ROOT` is deliberately left pointing at the real tree: the cited-path check
    is one of the things under test, and a sandbox that recreated every cited
    file would be checking the sandbox builder instead.
    """
    copy = tmp_path / "adapters.md"
    copy.write_text(NOTE.read_text())
    monkeypatch.setattr(check_adapters_doc, "DOC", copy)
    return copy


def complaint(capsys: pytest.CaptureFixture[str]) -> str:
    """Run the gate and return what it said.

    Asserting only the exit code would let a test pass on the wrong complaint.
    Several of these defects make the note inconsistent in more than one way if
    the edit is careless, and a test that accepts any failure cannot tell the
    check it was written for from the one standing next to it.
    """
    code = check_adapters_doc.main()
    said = capsys.readouterr().out
    assert code == 1, f"the gate passed, saying: {said.strip()}"
    return said


def edit(path: Path, old: str, new: str, *, count: int = 1) -> None:
    """Replace `old`, refusing to make a change that changes nothing.

    Every test below is an un-fix, and an un-fix that does not alter the text
    is indistinguishable from a gate that passes. Asserting the anchor is
    present is what keeps these tests from decaying into no-ops the day the
    note is reworded.
    """
    text = path.read_text()
    found = text.count(old)
    assert found == count, f"expected {count} of {old!r} in the note, found {found}"
    path.write_text(text.replace(old, new))


class TestItPassesOnTheRealTree:
    def test_the_note_and_the_code_agree_right_now(self) -> None:
        assert check_adapters_doc.main() == 0

    def test_a_missing_note_is_a_failure_not_a_skip(self, note: Path) -> None:
        note.unlink()
        assert check_adapters_doc.main() == 1


class TestTheToolTable:
    def test_the_number_of_adapters_going_stale(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "**2 adapters** ship", "**3 adapters** ship")
        assert "says 3 adapters ship" in complaint(capsys)

    def test_an_adapter_no_longer_named(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            note, "| `pip-audit` | known vulnerabilities", "| `pip-auditx` | known vulnerabilities"
        )
        assert "does not mark pip-audit as reaching the network" in complaint(capsys)

    def test_the_network_column_cleared(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The claim a reader deciding whether `--external` is safe relies on."""
        edit(note, "| **yes** |", "| no |")
        assert "does not mark pip-audit as reaching the network" in complaint(capsys)

    def test_the_constant_the_disclosure_reads_no_longer_named(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "REACHES_THE_NETWORK", "the network set")
        assert "no longer names the constant" in complaint(capsys)


class TestTheClaimTables:
    def test_the_ruff_total_going_stale(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "ruff's table holds **25 claims**", "ruff's table holds **26 claims**")
        assert "says ruff has 26 claims" in complaint(capsys)

    def test_the_breakdown_no_longer_adding_up(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "1 subsumed, 20 rejected", "1 subsumed, 19 rejected")
        assert "says 19 rejected ruff claims" in complaint(capsys)

    def test_the_adopted_count_going_stale(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "claims**: 4 adopted", "claims**: 5 adopted")
        assert "says 5 adopted ruff claims" in complaint(capsys)

    def test_the_pip_audit_total_going_stale(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "`pip-audit`'s table holds **1 claim", "`pip-audit`'s table holds **2 claim")
        assert "says pip-audit has 2 claims" in complaint(capsys)

    def test_a_verdict_no_longer_described(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "`SUBSUMED`", "`ABSORBED`", count=3)
        assert "does not describe the SUBSUMED verdict" in complaint(capsys)


class TestTheSubsumptionSection:
    """The one verdict that deletes information, so the one worth over-checking."""

    def test_the_prose_pairing_naming_the_wrong_rule(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The regression: this passed while the table header still said `DJD-002`."""
        edit(note, "`DJ001` covered by `DJD-002` —", "`DJ001` covered by `DJD-004` —")
        assert "pairs DJ001 with DJD-004" in complaint(capsys)

    def test_the_table_header_naming_the_wrong_rule(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            note,
            "| `DJ001` locations | covered by `DJD-002` |",
            "| `DJ001` locations | covered by `DJD-004` |",
        )
        assert "pairs DJ001 with DJD-004" in complaint(capsys)

    def test_the_pairing_dropped_from_both_places(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "`DJ001` covered by `DJD-002` —", "`DJ001` covered by one of ours —")
        edit(
            note, "| `DJ001` locations | covered by `DJD-002` |", "| `DJ001` locations | covered |"
        )
        assert "never states that DJ001 is subsumed by DJD-002" in complaint(capsys)

    def test_the_subsumed_code_no_longer_named(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "DJ001", "DJ099", count=2)
        assert "does not name DJ001" in complaint(capsys)

    def test_a_measured_row_going_stale(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "| pretix | 128 | 24 |", "| pretix | 128 | 40 |")
        assert "says 40 covered on pretix, the record says 24" in complaint(capsys)

    def test_a_row_that_overstates_what_was_reported(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "| healthchecks | 3 | 0 |", "| healthchecks | 9 | 0 |")
        assert "says 9 reported on healthchecks, the record says 3" in complaint(capsys)

    def test_the_totals_no_longer_matching_the_records(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "179 locations are reported", "200 locations are reported")
        assert "totals 200 reported and 24 covered" in complaint(capsys)

    def test_the_arithmetic_below_the_table_being_wrong(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """179 - 24 is 155, and the sentence after it is the whole argument."""
        edit(note, "of the 155 that remain", "of the 150 that remain")
        assert "says 150 remain, 179 - 24 is 155" in complaint(capsys)

    def test_a_missing_record_is_a_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The measurement is the evidence; the note without it is an assertion.

        Pointing `ROOT` at an empty directory would also do it, but for the
        wrong reason -- every cited path would vanish too, and the test would
        pass on the path check while the record check went unexercised. So the
        tree is mirrored and exactly one record is withheld.
        """
        root = tmp_path / "tree"
        root.mkdir()
        for entry in ROOT.iterdir():
            if entry.name != "benchmarks":
                (root / entry.name).symlink_to(entry)
        records = root / "benchmarks" / "subsumption"
        records.mkdir(parents=True)
        for record in (ROOT / "benchmarks" / "subsumption").glob("*.json"):
            if record.stem != "pretix":
                (records / record.name).symlink_to(record)
        monkeypatch.setattr(check_adapters_doc, "ROOT", root)
        assert "the recorded shortfall for pretix is missing" in complaint(capsys)


class TestTheBehaviourClaims:
    def test_a_named_flag_no_longer_named(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "--disable-pip", "--freeze-pip")
        assert "does not name --disable-pip" in complaint(capsys)

    def test_the_flag_the_layer_is_behind_no_longer_named(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(note, "--external", "--with-tools", count=2)
        assert "no longer names the flag" in complaint(capsys)

    def test_a_cited_path_that_does_not_exist(
        self, note: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edit(
            note,
            "`src/djaudit/adapters/merge.py`",
            "`src/djaudit/adapters/dedupe.py`",
            count=2,
        )
        assert "cites src/djaudit/adapters/dedupe.py" in complaint(capsys)


class ThirdAdapter:
    """A stand-in for the adapter somebody adds without touching the note."""

    name = "trivy"

    @property
    def table(self) -> ClaimTable:
        return ClaimTable(tool=self.name, claims=())

    def probe(self) -> Availability:
        return Availability(tool=self.name, reason="a stand-in, never run")

    def collect(self, root: object) -> Report:
        raise AssertionError("this adapter exists to be counted, not run")


class TestTheCodeMovingUnderIt:
    """The direction that actually happens: nobody edits the note at all.

    Every test in this class leaves the note exactly as it ships and changes
    the code instead, because a gate that only reads the note is a spell
    checker.
    """

    def test_a_third_adapter_shipping(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shipped = adapters.every()
        monkeypatch.setattr(adapters, "every", lambda: (*shipped, ThirdAdapter()))
        assert "says 2 adapters ship, `every()` returns 3" in complaint(capsys)

    def test_an_adapter_starting_to_reach_the_network(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(adapters, "REACHES_THE_NETWORK", frozenset({"pip-audit", "ruff"}))
        assert "does not mark ruff as reaching the network" in complaint(capsys)

    def test_nothing_reaching_the_network_any_more(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty set would make the network check vacuously true.

        Which is the failure mode this whole file exists for: the check would
        pass having compared nothing, and the note's safety claim would go
        unread.
        """
        monkeypatch.setattr(adapters, "REACHES_THE_NETWORK", frozenset())
        assert "REACHES_THE_NETWORK is empty" in complaint(capsys)

    def test_a_claim_added_to_the_table(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        added = Claimed(
            code="S9999",
            claim=Claim.ADOPT,
            why="a claim nobody has written down in the note",
            title="A newly adopted check",
            family=Family.DJS,
            severity=Severity.MEDIUM,
            confidence=Confidence.FIRM,
        )
        # Patched on the gate rather than at the source: the gate binds the
        # table at import, so patching `djaudit.adapters.ruff` would leave it
        # reading the old one and the test would pass having changed nothing.
        monkeypatch.setattr(
            check_adapters_doc,
            "RUFF",
            ClaimTable(tool=RUFF.tool, claims=(*RUFF.claims, added)),
        )
        assert "says ruff has 25 claims, the table has 26" in complaint(capsys)

    def test_the_subsumption_verdict_being_reversed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If we ever adopt `DJ001`, the note's longest argument is obsolete."""
        kept = tuple(claim for claim in RUFF.claims if claim.claim is not Claim.SUBSUMED)
        monkeypatch.setattr(check_adapters_doc, "RUFF", ClaimTable(tool=RUFF.tool, claims=kept))
        assert check_adapters_doc.main() == 1

    def test_a_documented_flag_dropped_from_the_arguments(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The note explains `--disable-pip`; it is worth having only while it is passed."""
        kept = tuple(a for a in ARGUMENTS if a != "--disable-pip")
        monkeypatch.setattr(check_adapters_doc, "ARGUMENTS", kept)
        assert "discusses --disable-pip" in complaint(capsys)

    def test_the_vulnerability_service_changing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        swapped = tuple("pypi" if a == "osv" else a for a in ARGUMENTS)
        monkeypatch.setattr(check_adapters_doc, "ARGUMENTS", swapped)
        assert "says the OSV service is used" in complaint(capsys)


class TestTheGateReadsWhatItClaimsTo:
    """Guards on the readers themselves, which is where `check_live_doc.py`
    was found returning `None` and reporting success."""

    def test_it_reads_the_adapters_from_the_registry(self) -> None:
        assert [a.name for a in adapters.every()] == ["ruff", "pip-audit"]

    def test_the_third_adapter_stand_in_conforms(self) -> None:
        """Otherwise the count test would be exercising a type error."""
        assert isinstance(ThirdAdapter(), Adapter)

    def test_the_note_names_the_adapters_in_run_order(self) -> None:
        text = " ".join(NOTE.read_text().split())
        positions = [text.index(f"`{a.name}`") for a in adapters.every()]
        assert positions == sorted(positions)

    def test_every_cited_path_is_one_the_repository_has(self) -> None:
        text = NOTE.read_text()
        cited = sorted(set(re.findall(r"`((?:src|tests|scripts|docs|benchmarks)/[\w./-]+)`", text)))
        assert cited, "the note cites no paths, so the cited-path check verifies nothing"
        assert [path for path in cited if not (ROOT / path).exists()] == []
