"""Asking for external tools, and what asking must not change.

`--external` is off by default, and the default is load-bearing twice over.
Every precision number this project records was measured without it, so a flag
that leaked findings into a default run would silently invalidate the
benchmarks; and one of the adapters queries a vulnerability database over the
network, which is not something a static analyser should do because somebody
typed its name.

**No test here runs a real external tool.** `pip-audit` reaches the network and
`ruff` is not guaranteed to be installed wherever this suite runs, so both are
replaced by adapters that answer from memory. What is under test is the fold —
that an external finding is suppressed, baselined, thresholded, fingerprinted
and ranked by exactly the rules that govern one of ours — and not the tools,
which have their own suites.
"""

from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from djaudit import adapters, engine
from djaudit.adapters import Availability, ClaimTable, Report
from djaudit.baseline import Baseline
from djaudit.cli import app
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

PROJECT = Path("tests/fixtures/vulnerable_project")


def unwrapped(text: str) -> str:
    """Rich wraps to the terminal width; the claim here is about words."""
    return " ".join(text.split())


def external_finding(
    *,
    code: str = "RUFF-S324",
    file: str = "manage.py",
    line: int = 1,
    severity: Severity = Severity.MEDIUM,
    confidence: Confidence = Confidence.FIRM,
) -> Finding:
    """A finding shaped exactly as `adapters.base.as_finding` shapes one.

    Built here rather than by calling `as_finding` so that a change to the claim
    table cannot quietly change what these tests are folding in.
    """
    return Finding(
        rule_id=code,
        title="insecure hash",
        severity=severity,
        confidence=confidence,
        family=Family.DJS,
        tier=Tier.STATIC,
        location=Location(file=file, line=line, column=1, snippet="hashlib.md5(data)"),
        message="probable use of insecure hash function",
        rationale="md5 is not collision resistant",
        remediation="use sha256",
        evidence=(Evidence(kind=EvidenceKind.COMMAND_OUTPUT, content=code, source="ruff 0.16.1"),),
        properties={"external": "ruff", "code": code},
    )


@dataclass
class FakeAdapter:
    """An adapter that answers from memory. Never touches a subprocess."""

    name: str = "ruff"
    findings: tuple[Finding, ...] = ()
    diagnostics: tuple[str, ...] = ()
    availability: Availability | None = None
    explode: bool = False
    asked: list[Path] = field(default_factory=list)

    @property
    def table(self) -> ClaimTable:  # pragma: no cover - unused by the engine
        raise NotImplementedError

    def probe(self) -> Availability:  # pragma: no cover - unused by the engine
        raise NotImplementedError

    def collect(self, root: object) -> Report:
        self.asked.append(Path(str(root)))
        if self.explode:
            raise RuntimeError("adapter is broken")
        return Report(
            tool=self.name,
            findings=self.findings,
            availability=(
                self.availability
                if self.availability is not None
                else Availability(tool=self.name, version="1.0")
            ),
            diagnostics=self.diagnostics,
        )


def run(**kwargs: object) -> engine.RunResult:
    return engine.run(PROJECT, **kwargs)  # type: ignore[arg-type]


class TestNotAskingForThem:
    """The default. Everything below depends on this staying true."""

    def test_no_adapter_is_consulted(self) -> None:
        spy = FakeAdapter(findings=(external_finding(),))
        run()
        assert spy.asked == []

    def test_the_findings_are_the_same_ones(self) -> None:
        plain = run()
        with_none = run(external=())
        assert [f.fingerprint for f in plain.findings] == [
            f.fingerprint for f in with_none.findings
        ]

    def test_nothing_is_reported_about_tools(self) -> None:
        result = run()
        assert result.external_notices == ()
        assert result.external_diagnostics == ()
        assert result.external_duplicates == 0


class TestAskingForThem:
    def test_the_findings_arrive(self) -> None:
        result = run(external=(FakeAdapter(findings=(external_finding(),)),))
        assert "RUFF-S324" in {f.rule_id for f in result.findings}

    def test_the_adapter_is_asked_about_the_project(self) -> None:
        spy = FakeAdapter(findings=(external_finding(),))
        run(external=(spy,))
        assert spy.asked == [PROJECT.resolve()]

    def test_ours_are_still_all_there(self) -> None:
        before = {f.fingerprint for f in run().findings}
        after = {f.fingerprint for f in run(external=(FakeAdapter(),)).findings}
        assert before <= after

    def test_every_tool_is_named_whether_or_not_it_found_anything(self) -> None:
        result = run(
            external=(
                FakeAdapter(name="ruff", findings=(external_finding(),)),
                FakeAdapter(name="pip-audit"),
            )
        )
        assert len(result.external_notices) == 2
        assert any("contributed 1 findings" in n for n in result.external_notices)
        assert any("contributed 0 findings" in n for n in result.external_notices)

    def test_what_a_tool_could_not_read_is_a_diagnostic(self) -> None:
        result = run(external=(FakeAdapter(diagnostics=("3 requirements are unpinned",)),))
        assert result.external_diagnostics == ("ruff: 3 requirements are unpinned",)

    def test_a_missing_tool_is_a_notice_and_not_a_failure(self) -> None:
        absent = FakeAdapter(availability=Availability(tool="ruff", reason="not installed"))
        result = run(external=(absent,))
        assert result.external_notices == ("ruff unavailable: not installed",)
        assert result.rule_errors == {}

    def test_the_raw_count_includes_what_the_tools_said(self) -> None:
        without = run().total_raw
        result = run(
            external=(FakeAdapter(findings=(external_finding(), external_finding(line=9))),)
        )
        assert result.total_raw == without + 2


class TestAnAdapterThatBreaks:
    """`collect` promises not to raise, but the promise is somebody else's."""

    def test_the_run_survives(self) -> None:
        result = run(external=(FakeAdapter(explode=True),))
        assert result.findings

    def test_the_failure_is_recorded_under_the_tool_name(self) -> None:
        result = run(external=(FakeAdapter(name="ruff", explode=True),))
        assert result.rule_errors["ruff"] == "RuntimeError: adapter is broken"

    def test_the_other_tools_are_still_asked(self) -> None:
        survivor = FakeAdapter(name="pip-audit", findings=(external_finding(code="PYSEC-1"),))
        result = run(external=(FakeAdapter(name="ruff", explode=True), survivor))
        assert "PYSEC-1" in {f.rule_id for f in result.findings}


class TestUnderOurOwnRules:
    """The point of folding external findings in before the filters, not after."""

    def test_they_are_fingerprinted(self) -> None:
        result = run(external=(FakeAdapter(findings=(external_finding(),)),))
        assert all(f.fingerprint for f in result.findings)

    def test_two_identical_findings_get_different_identities(self) -> None:
        result = run(external=(FakeAdapter(findings=(external_finding(), external_finding())),))
        prints = [f.fingerprint for f in result.findings if f.rule_id == "RUFF-S324"]
        assert len(prints) == 2
        assert len(set(prints)) == 2

    def test_the_severity_threshold_applies_to_them(self) -> None:
        low = FakeAdapter(findings=(external_finding(severity=Severity.LOW),))
        result = run(external=(low,), min_severity=Severity.HIGH)
        assert "RUFF-S324" not in {f.rule_id for f in result.findings}

    def test_the_confidence_threshold_applies_to_them(self) -> None:
        weak = FakeAdapter(findings=(external_finding(confidence=Confidence.TENTATIVE),))
        result = run(external=(weak,), min_confidence=Confidence.FIRM)
        assert "RUFF-S324" not in {f.rule_id for f in result.findings}

    def test_they_are_counted_as_filtered_not_as_absent(self) -> None:
        low = FakeAdapter(findings=(external_finding(severity=Severity.LOW),))
        plain = run(min_severity=Severity.HIGH).filtered_threshold
        result = run(external=(low,), min_severity=Severity.HIGH)
        assert result.filtered_threshold == plain + 1

    def test_the_baseline_hides_them(self) -> None:
        first = run(external=(FakeAdapter(findings=(external_finding(),)),))
        recorded = Baseline.from_findings(first.findings)
        again = run(external=(FakeAdapter(findings=(external_finding(),)),), baseline=recorded)
        assert again.findings == []

    def test_a_new_external_finding_still_gets_through_a_baseline(self) -> None:
        first = run(external=(FakeAdapter(findings=(external_finding(),)),))
        recorded = Baseline.from_findings(first.findings)
        again = run(
            external=(FakeAdapter(findings=(external_finding(), external_finding(line=99))),),
            baseline=recorded,
        )
        assert [f.rule_id for f in again.findings] == ["RUFF-S324"]

    def test_an_inline_suppression_hides_them(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        target.write_text("import hashlib  # djaudit: ignore[RUFF-S324]\n")
        found = external_finding(file="app.py")
        result = engine.run(tmp_path, external=(FakeAdapter(findings=(found,)),))
        assert result.findings == []
        assert result.suppressed_inline == 1

    def test_they_are_ranked_among_ours_rather_than_appended(self) -> None:
        result = run(
            external=(FakeAdapter(findings=(external_finding(severity=Severity.CRITICAL),)),)
        )
        assert [f.sort_key for f in result.findings] == sorted(f.sort_key for f in result.findings)


class TestTheRegistry:
    """What `--external` resolves to, checked without running any of it."""

    def test_both_adapters_are_offered(self) -> None:
        assert [a.name for a in adapters.every()] == ["ruff", "pip-audit"]

    def test_the_local_tool_runs_before_the_networked_one(self) -> None:
        names = [a.name for a in adapters.every()]
        assert names.index("ruff") < names.index("pip-audit")

    def test_the_networked_tools_are_named_as_data(self) -> None:
        assert {"pip-audit"} == adapters.REACHES_THE_NETWORK

    def test_every_networked_name_is_an_adapter_we_have(self) -> None:
        assert {a.name for a in adapters.every()} >= adapters.REACHES_THE_NETWORK


runner = CliRunner()


@pytest.fixture
def stub_adapters(monkeypatch: pytest.MonkeyPatch) -> FakeAdapter:
    """Make `--external` resolve to something that cannot reach the network."""
    fake = FakeAdapter(findings=(external_finding(),), diagnostics=("could not read setup.py",))
    monkeypatch.setattr(adapters, "every", lambda: (fake,))
    return fake


class TestTheCommandLine:
    def test_the_flag_is_off_by_default(self, stub_adapters: FakeAdapter) -> None:
        runner.invoke(app, ["run", str(PROJECT)])
        assert stub_adapters.asked == []

    def test_asking_reaches_the_adapters(self, stub_adapters: FakeAdapter) -> None:
        runner.invoke(app, ["run", str(PROJECT), "--external"])
        assert stub_adapters.asked == [PROJECT.resolve()]

    def test_refusing_explicitly_also_works(self, stub_adapters: FakeAdapter) -> None:
        runner.invoke(app, ["run", str(PROJECT), "--no-external"])
        assert stub_adapters.asked == []

    def test_json_on_stdout_stays_parseable(self, stub_adapters: FakeAdapter) -> None:
        result = runner.invoke(
            app, ["run", str(PROJECT), "--external", "--format", "json"], catch_exceptions=False
        )
        assert json.loads(result.stdout)["findings"]

    def test_the_notices_are_not_on_stdout(self, stub_adapters: FakeAdapter) -> None:
        result = runner.invoke(app, ["run", str(PROJECT), "--external", "--format", "json"])
        assert "could not read setup.py" not in result.stdout

    def test_the_notices_are_on_stderr(self, stub_adapters: FakeAdapter) -> None:
        result = runner.invoke(app, ["run", str(PROJECT), "--external", "--format", "json"])
        assert "could not read setup.py" in result.stderr
        assert "contributed 1 findings" in result.stderr

    def test_nothing_is_said_about_tools_when_none_were_asked(
        self, stub_adapters: FakeAdapter
    ) -> None:
        result = runner.invoke(app, ["run", str(PROJECT)])
        assert "external:" not in result.stderr


class TestTheDisclosure:
    """A notice that the network was used is worth nothing after the request."""

    def test_it_names_the_tool_that_queries_a_database(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from djaudit.cli import _with_external

        _with_external(granted=True)
        assert "pip-audit will query a vulnerability database" in unwrapped(capsys.readouterr().err)

    def test_it_names_every_tool_that_will_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        from djaudit.cli import _with_external

        _with_external(granted=True)
        printed = capsys.readouterr().err
        assert all(a.name in printed for a in adapters.every())

    def test_it_comes_before_anything_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ordering is the whole claim, so the adapter reports what it could see.

        Asserted through the command rather than by calling the two halves in
        order here, because a test that performs the sequence itself proves
        only that the test can count.
        """
        seen: list[str] = []
        watcher = FakeAdapter(name="pip-audit")

        def collect(root: object) -> Report:
            written = cast("io.BytesIO", sys.stderr.buffer).getvalue()
            seen.append(written.decode())
            return Report(
                tool="pip-audit", availability=Availability(tool="pip-audit", version="1.0")
            )

        monkeypatch.setattr(watcher, "collect", collect)
        monkeypatch.setattr(adapters, "every", lambda: (watcher,))
        runner.invoke(app, ["run", str(PROJECT), "--external", "--format", "json"])
        assert seen, "the adapter was never asked"
        assert "will query a vulnerability database" in unwrapped(seen[0])

    def test_saying_no_discloses_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        from djaudit.cli import _with_external

        assert _with_external(granted=False) == ()
        assert capsys.readouterr().err == ""
