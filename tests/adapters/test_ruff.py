"""The ruff adapter: what it keeps, what it refuses, and what it does when the
output is too big to come back through a pipe.

Nothing here runs ruff except one test that skips when it is absent. The rest
work from recorded output, because a test that shells out to a linter measures
the linter's version rather than our decisions, and the decisions are the whole
of this module.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from djaudit.adapters.base import Availability, Claim
from djaudit.adapters.ruff import ARGUMENTS, CLAIMS, SELECT, RuffAdapter, parse
from djaudit.models import Confidence, Family, Severity

ADOPTED = {"S113", "S324", "DJ006", "DJ007"}

_RUFF = Availability(tool="ruff", version="0.16.1")


def diagnostic(code: str, path: str, row: int = 12, name: str = "some-check") -> dict[str, object]:
    """One entry shaped like ruff's JSON, with the fields we actually read."""
    return {
        "code": code,
        "name": name,
        "message": f"{code} says something",
        "filename": path,
        "location": {"row": row, "column": 5},
        "end_location": {"row": row, "column": 19},
        "url": f"https://docs.astral.sh/ruff/rules/{name}/",
        "fix": None,
    }


class TestTheDecisionsAreComplete:
    def test_every_claim_is_about_a_distinct_code(self) -> None:
        codes = [c.code for c in CLAIMS.claims]
        assert len(codes) == len(set(codes))

    def test_exactly_these_four_codes_are_adopted(self) -> None:
        # Named rather than counted, so that adopting a fifth is a decision
        # somebody has to make here rather than a number that drifts.
        assert {c.code for c in CLAIMS.claims if c.adopted} == ADOPTED

    def test_the_subsumed_code_names_the_rule_that_covers_it(self) -> None:
        subsumed = {c.code: c.ours for c in CLAIMS.claims if c.claim is Claim.SUBSUMED}
        assert subsumed == {"DJ001": ("DJD-002",)}

    def test_every_rejection_carries_a_measurement(self) -> None:
        # A rejection is the one decision with no visible consequence, so its
        # reason is the only thing standing between it and a shrug. Each should
        # say how many findings it looked at.
        for claim in CLAIMS.claims:
            if claim.claim is Claim.REJECTED:
                assert any(ch.isdigit() for ch in claim.why), claim.code

    def test_the_configuration_cannot_be_overridden_by_the_target(self) -> None:
        # Without `--isolated` a project silences our audit by editing its own
        # `pyproject.toml`, which is the wrong way round.
        assert "--isolated" in ARGUMENTS
        assert "--no-cache" in ARGUMENTS
        assert SELECT == "DJ,S"

    def test_every_adopted_code_says_what_to_do_about_it(self) -> None:
        # An adopted finding is one we put our name on, and ruff's own message
        # is a description rather than an instruction. Blanking any of these
        # four survived mutation until this existed.
        remediations = {c.code: c.remediation for c in CLAIMS.claims if c.adopted}
        assert set(remediations) == ADOPTED
        for code, text in remediations.items():
            assert len(text) > 40, code

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            # The specific instruction each one exists to give, not just that
            # some prose is present.
            ("S113", "timeout="),
            ("S324", "usedforsecurity=False"),
            ("DJ007", "List the fields"),
            ("DJ006", "Replace `exclude` with `fields`"),
        ],
    )
    def test_the_fix_is_named_rather_than_gestured_at(self, code: str, expected: str) -> None:
        (claim,) = [c for c in CLAIMS.claims if c.code == code]
        assert expected in claim.remediation


class TestReadingRuffsOutput:
    def test_an_absolute_filename_becomes_project_relative(self, tmp_path: Path) -> None:
        payload = json.dumps([diagnostic("S324", str(tmp_path / "hc" / "api" / "models.py"))])
        (found,) = parse(payload, tmp_path)
        assert found.file == "hc/api/models.py"

    def test_the_span_and_the_link_survive(self, tmp_path: Path) -> None:
        payload = json.dumps([diagnostic("S113", str(tmp_path / "a.py"), row=7)])
        (found,) = parse(payload, tmp_path)
        assert (found.line, found.column) == (7, 5)
        assert (found.end_line, found.end_column) == (7, 19)
        assert found.url.startswith("https://docs.astral.sh/ruff/")

    def test_a_file_outside_the_project_is_dropped(self, tmp_path: Path) -> None:
        # An absolute path from somewhere else cannot be resolved by any reader
        # of the report and would move the fingerprint of a finding that has not
        # changed.
        payload = json.dumps([diagnostic("S324", "/elsewhere/lib/hash.py")])
        assert parse(payload, tmp_path) == ()

    def test_no_findings_is_not_an_error(self, tmp_path: Path) -> None:
        assert parse("[]", tmp_path) == ()
        assert parse("   ", tmp_path) == ()

    def test_a_missing_end_location_is_allowed(self, tmp_path: Path) -> None:
        entry = diagnostic("S113", str(tmp_path / "a.py"))
        entry["end_location"] = None
        (found,) = parse(json.dumps([entry]), tmp_path)
        assert found.end_line is None


class TestWhichFindingsSurvive:
    def test_an_adopted_code_is_kept_and_namespaced(self, tmp_path: Path) -> None:
        payload = json.dumps([diagnostic("S113", str(tmp_path / "svc" / "client.py"))])
        found = CLAIMS.adopt(parse(payload, tmp_path), _RUFF)
        assert [f.rule_id for f in found] == ["RUFF-S113"]

    def test_an_adopted_finding_takes_a_djaudit_family(self, tmp_path: Path) -> None:
        payload = json.dumps([diagnostic("DJ007", str(tmp_path / "forms.py"))])
        (finding,) = CLAIMS.adopt(parse(payload, tmp_path), _RUFF)
        assert finding.family is Family.DJA
        assert finding.severity is Severity.HIGH

    def test_the_hash_rule_is_tentative_because_the_tool_cannot_tell(self, tmp_path: Path) -> None:
        payload = json.dumps([diagnostic("S324", str(tmp_path / "models.py"))])
        (finding,) = CLAIMS.adopt(parse(payload, tmp_path), _RUFF)
        assert finding.confidence is Confidence.TENTATIVE

    def test_a_subsumed_code_produces_nothing(self, tmp_path: Path) -> None:
        payload = json.dumps([diagnostic("DJ001", str(tmp_path / "models.py"))])
        assert CLAIMS.adopt(parse(payload, tmp_path), _RUFF) == ()

    def test_a_rejected_code_produces_nothing(self, tmp_path: Path) -> None:
        payload = json.dumps([diagnostic("S101", str(tmp_path / "views.py"))])
        assert CLAIMS.adopt(parse(payload, tmp_path), _RUFF) == ()

    def test_an_adopted_code_in_test_code_produces_nothing(self, tmp_path: Path) -> None:
        payload = json.dumps([diagnostic("S324", str(tmp_path / "tests" / "helpers.py"))])
        assert CLAIMS.adopt(parse(payload, tmp_path), _RUFF) == ()

    def test_a_code_ruff_adds_later_is_surfaced_rather_than_guessed_at(
        self, tmp_path: Path
    ) -> None:
        payload = json.dumps([diagnostic("S999", str(tmp_path / "views.py"))])
        found = parse(payload, tmp_path)
        assert CLAIMS.adopt(found, _RUFF) == ()
        assert CLAIMS.unclaimed(found) == ("S999",)


class TestRunningTheTool:
    """Driven by a stub on disk rather than the real ruff.

    The stub is what makes the size test possible at all: reproducing the
    failure needs more than a megabyte of output, and generating that from real
    source would mean shipping a project with 12,000 defects in it.
    """

    def stub(self, tmp_path: Path, action: str) -> str:
        """A fake ruff on disk. `action` runs once `--version` is dealt with."""
        script = tmp_path / "fake-ruff"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--version' in sys.argv:\n"
            "    print('ruff 0.16.1')\n"
            "    raise SystemExit(0)\n"
            "destination = sys.argv[sys.argv.index('--output-file') + 1]\n" + action
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(script)

    def test_a_report_larger_than_the_pipe_allows_still_arrives(self, tmp_path: Path) -> None:
        # The regression. `live.runner` caps a captured stream at 1 MiB so a
        # runaway subprocess cannot exhaust memory, and ruff's real output for
        # pretix is larger than that -- read from stdout it arrived truncated
        # mid-object and the largest project in the corpus reported nothing at
        # all. Reading the file ruff wrote is what fixes it.
        project = tmp_path / "project"
        (project / "app").mkdir(parents=True)
        big = json.dumps(
            [diagnostic("S113", str(project / "app" / f"module_{i}.py")) for i in range(4000)]
        )
        assert len(big) > (1 << 20), "the payload must exceed the cap to test anything"
        source = tmp_path / "payload.json"
        source.write_text(big)

        executable = self.stub(
            tmp_path,
            f"import shutil\nshutil.copyfile({str(source)!r}, destination)\nraise SystemExit(1)\n",
        )
        report = RuffAdapter(executable=executable).collect(project)
        assert report.diagnostics == ()
        assert len(report.findings) == 4000

    def test_output_written_nowhere_is_a_diagnostic_not_a_crash(self, tmp_path: Path) -> None:
        # The control for the test above: a tool that prints to stdout instead
        # of writing the file it was given yields nothing here, which is what
        # proves the 4,000 findings above came out of the file.
        executable = self.stub(tmp_path, "print('[]')\nraise SystemExit(0)\n")
        report = RuffAdapter(executable=executable).collect(tmp_path)
        assert report.findings == ()
        assert len(report.diagnostics) == 1

    def test_unreadable_output_is_a_diagnostic_not_a_crash(self, tmp_path: Path) -> None:
        executable = self.stub(
            tmp_path,
            "open(destination, 'w').write('not json{')\nraise SystemExit(1)\n",
        )
        report = RuffAdapter(executable=executable).collect(tmp_path)
        assert report.findings == ()
        assert "could not be read" in report.diagnostics[0]

    def test_a_missing_tool_degrades_instead_of_failing(self, tmp_path: Path) -> None:
        report = RuffAdapter(executable="ruff-that-is-not-installed").collect(tmp_path)
        assert not report.ran
        assert report.findings == ()
        assert "unavailable" in report.explain()
        # An optional tool nobody installed is an ordinary fact about a machine.
        # Without this the guard that returns early can be dropped entirely and
        # every audit on a machine without ruff gains a diagnostic about it.
        assert report.diagnostics == ()

    def test_an_adapter_cannot_be_reconfigured_after_it_is_built(self) -> None:
        # Adapters are constructed once and used across a run, so a mutable one
        # would let anything holding a reference change what the next project
        # is audited with.
        adapter = RuffAdapter()
        with pytest.raises(FrozenInstanceError):
            adapter.executable = "something-else"  # type: ignore[misc]
        assert not hasattr(adapter, "__dict__")


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff is not installed")
class TestAgainstTheRealTool:
    """One test that the recorded output above still resembles the real thing.

    Everything else here would keep passing if ruff changed its JSON shape
    entirely, because everything else reads a fixture this repository wrote.
    """

    def test_the_real_ruff_reports_the_fields_the_parser_reads(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "hashing.py").write_text("import hashlib\n\nh = hashlib.md5(b'x').hexdigest()\n")

        report = RuffAdapter(executable=os.fspath(shutil.which("ruff") or "ruff")).collect(project)

        assert report.ran
        assert report.diagnostics == ()
        assert [f.rule_id for f in report.findings] == ["RUFF-S324"]
        (finding,) = report.findings
        assert finding.location.file == "hashing.py"
        assert finding.location.line == 3
        assert finding.family is Family.DJS
