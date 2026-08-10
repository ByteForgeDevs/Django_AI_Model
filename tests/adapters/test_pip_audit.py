"""The pip-audit adapter: what it audits, what it refuses to guess, and why it
never touches the network from here.

`data/pip_audit_django_3_2_0.json` is real output, recorded from a run against
`django==3.2.0`, `requests==2.19.0` and `jinja2==2.10`. It keeps a duplicate
advisory entry because OSV really returns those -- 52 of the 114 entries in the
probe measurement were repeats -- and a fixture that quietly cleaned that up
would test a tool nobody runs.

Nothing here reaches the network. This is the one adapter that does when a user
runs it, so it is also the one whose tests must not: a suite that depends on a
vulnerability database is a suite whose result changes when somebody publishes
an advisory.
"""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest

from djaudit import manifest
from djaudit.adapters.base import Availability, Claim, Claimed
from djaudit.adapters.pip_audit import (
    ARGUMENTS,
    CLAIMS,
    Advisory,
    PipAuditAdapter,
    as_finding,
    by_file,
    parse,
    pinned,
    summarise,
)
from djaudit.models import Confidence, Family, Finding, Severity

RECORDED_PATH = Path(__file__).parent / "data" / "pip_audit_django_3_2_0.json"
RECORDED = RECORDED_PATH.read_text()
PIP_AUDIT = Availability(tool="pip-audit", version="2.10.1")

REQUIREMENTS = """# a deliberately old set of pins
django==3.2.0
requests==2.19.0
jinja2==2.10
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text(REQUIREMENTS)
    return tmp_path


def requirements_of(project: Path) -> tuple[manifest.Requirement, ...]:
    (found,) = [m for m in manifest.discover(project) if not m.development]
    return found.requirements


class TestWhatItRefusesToGuess:
    """`--disable-pip` will not audit an unpinned requirement, and neither will we.

    The alternative is worse than silence: pip-audit's resolver picks the
    version that resolves *today*, so a clean report about `babel` describes a
    release the deployment may never have installed.
    """

    def test_an_exact_pin_is_audited(self) -> None:
        auditable, skipped = pinned(_requirements("django==3.2.0"))
        assert auditable == ["django==3.2.0"]
        assert skipped == []

    @pytest.mark.parametrize(
        "spec",
        [
            "babel",  # no version at all
            "bleach==6.4.*",  # a wildcard resolves to whatever is newest
            "sphinx-rtd-theme~=3.1.0",  # compatible-release, same problem
            "Django>=5.2,<6.0",  # a range
        ],
    )
    def test_anything_less_than_exact_is_not(self, spec: str) -> None:
        # All four are real lines from the benchmark corpus.
        auditable, skipped = pinned(_requirements(spec))
        assert auditable == []
        assert skipped == [spec]

    def test_extras_do_not_stop_an_exact_pin_being_audited(self) -> None:
        # `Django[argon2]==6.0.7` is healthchecks' actual pin, and the version
        # is exact whatever extras are attached.
        auditable, _ = pinned(_requirements("Django[argon2]==6.0.7"))
        assert auditable == ["Django[argon2]==6.0.7"]

    def test_a_wildcard_with_extras_is_still_refused(self) -> None:
        auditable, skipped = pinned(_requirements("Django[argon2]==5.2.*"))
        assert (auditable, skipped) == ([], ["Django[argon2]==5.2.*"])


class TestGroupingByFile:
    def test_one_pyproject_declaring_many_groups_is_one_file(self) -> None:
        # netbox declares ten optional-dependency groups. Ten runs and ten
        # near-identical diagnostics about one unpinned line is not a report.
        path = Path("/project/pyproject.toml")
        manifests = [
            _manifest(path, ("django-auth-ldap", 5)),
            _manifest(path, ("python3-saml", 6)),
            _manifest(path, ("boto3", 7)),
        ]
        grouped = by_file(manifests)
        assert list(grouped) == [path]
        assert len(grouped[path]) == 3

    def test_a_dependency_in_two_groups_is_counted_once(self) -> None:
        path = Path("/project/pyproject.toml")
        same = ("django-auth-ldap", 5)
        grouped = by_file([_manifest(path, same), _manifest(path, same)])
        assert len(grouped[path]) == 1

    def test_separate_files_stay_separate(self) -> None:
        a, b = Path("/project/requirements.txt"), Path("/project/pyproject.toml")
        grouped = by_file([_manifest(a, ("django", 1)), _manifest(b, ("celery", 2))])
        assert set(grouped) == {a, b}


class TestReadingWhatPipAuditSaid:
    def test_each_advisory_is_tied_to_the_line_that_pinned_it(self, project: Path) -> None:
        found = parse(RECORDED, requirements_of(project), "requirements.txt")
        lines = {(a.package, a.line) for a in found}
        # Line 1 is a comment, so django is on line 2. pip-audit reports a
        # package and never a line; the line is our own reading of the file.
        assert lines == {("django", 2), ("requests", 3)}

    def test_a_repeated_advisory_is_reported_once(self, project: Path) -> None:
        # The fixture holds PYSEC-2022-190 twice, exactly as OSV returned it.
        found = parse(RECORDED, requirements_of(project), "requirements.txt")
        identifiers = [a.identifier for a in found if a.package == "django"]
        assert identifiers.count("PYSEC-2022-190") == 1
        assert len(identifiers) == 3

    def test_a_package_with_no_advisories_produces_nothing(self, project: Path) -> None:
        found = parse(RECORDED, requirements_of(project), "requirements.txt")
        assert not [a for a in found if a.package == "jinja2"]

    def test_the_fix_versions_survive(self, project: Path) -> None:
        found = parse(RECORDED, requirements_of(project), "requirements.txt")
        (sqli,) = [a for a in found if a.identifier == "PYSEC-2022-190"]
        assert sqli.fix_versions == ("4.0.4",)
        assert "CVE-2022-28346" in sqli.aliases

    def test_the_source_is_the_database_not_the_advisory(self, project: Path) -> None:
        found = parse(RECORDED, requirements_of(project), "requirements.txt")
        assert {a.source for a in found} == {"PYSEC"}

    def test_no_dependencies_is_not_an_error(self, project: Path) -> None:
        assert parse('{"dependencies": [], "fixes": []}', requirements_of(project), "r.txt") == ()


class TestTheFindings:
    def findings(self, project: Path) -> tuple[Finding, ...]:
        found = parse(RECORDED, requirements_of(project), "requirements.txt")
        claimed = CLAIMS.by_code["PYSEC"]
        return tuple(as_finding(a, claimed, PIP_AUDIT) for a in found)

    def test_each_advisory_gets_its_own_rule_id(self, project: Path) -> None:
        # The reason `ClaimTable.as_finding` is not used. One id for all of
        # them would give three advisories on one line one fingerprint, and a
        # baseline accepting one would silently accept the other two.
        ids = [f.rule_id for f in self.findings(project)]
        assert len(set(ids)) == len(ids)
        assert "PIP-AUDIT-PYSEC-2022-190" in ids

    def test_the_title_names_the_package_version_and_advisory(self, project: Path) -> None:
        (sqli,) = [f for f in self.findings(project) if "PYSEC-2022-190" in f.rule_id]
        assert sqli.title == "django 3.2.0 is affected by PYSEC-2022-190"

    def test_the_remediation_names_the_version_to_move_to(self, project: Path) -> None:
        (sqli,) = [f for f in self.findings(project) if "PYSEC-2022-190" in f.rule_id]
        assert sqli.remediation.startswith("Upgrade django to 4.0.4.")

    def test_the_snippet_is_the_line_as_written(self, project: Path) -> None:
        (sqli,) = [f for f in self.findings(project) if "PYSEC-2022-190" in f.rule_id]
        assert sqli.location.snippet == "django==3.2.0"

    def test_the_evidence_carries_the_full_advisory(self, project: Path) -> None:
        (sqli,) = [f for f in self.findings(project) if "PYSEC-2022-190" in f.rule_id]
        content = sqli.evidence[0].content
        assert "fixed in 4.0.4" in content
        assert "SQL Injection in Django" in content
        assert sqli.evidence[0].source == "pip-audit 2.10.1"

    def test_the_aliases_become_links(self, project: Path) -> None:
        # Every id in the fixture was checked against osv.dev by hand and
        # resolves, aliases included, so linking them is not guesswork.
        (sqli,) = [f for f in self.findings(project) if "PYSEC-2022-190" in f.rule_id]
        assert "https://osv.dev/vulnerability/PYSEC-2022-190" in sqli.references
        assert "https://osv.dev/vulnerability/CVE-2022-28346" in sqli.references

    def test_it_is_a_settings_finding_and_not_certain(self, project: Path) -> None:
        finding = self.findings(project)[0]
        assert finding.family is Family.DJS
        assert finding.severity is Severity.HIGH
        # Not `certain`: an advisory's affected-range data is itself sometimes
        # broader than the code that was actually vulnerable.
        assert finding.confidence is Confidence.FIRM

    def test_a_long_advisory_is_cut_down_for_the_message(self) -> None:
        assert summarise("first line\nsecond line") == "first line"
        assert len(summarise("x" * 400)) == 160
        assert summarise("x" * 400).endswith("...")


class TestAnAdvisoryWithNothingToSay:
    """The branches a real database only reaches occasionally.

    Every advisory in the recorded payload names a fix and carries prose. One
    that does neither is not hypothetical -- OSV publishes entries before a fix
    exists -- and both are places where a finding could quietly lose its content.
    """

    def advisory(self, **overrides: object) -> Advisory:
        fields: dict[str, object] = {
            "identifier": "PYSEC-2024-1",
            "package": "django",
            "version": "3.2.0",
            "description": "A directory traversal.",
            "fix_versions": ("3.2.13", "4.0.4"),
            "aliases": (),
            "file": "requirements.txt",
            "line": 2,
            "raw": "django==3.2.0",
        }
        fields.update(overrides)
        return Advisory(**fields)  # type: ignore[arg-type]

    def finding(self, **overrides: object) -> Finding:
        return as_finding(self.advisory(**overrides), CLAIMS.by_code["PYSEC"], PIP_AUDIT)

    def test_every_fixed_version_is_offered_and_they_are_separated(self) -> None:
        assert self.finding().remediation.startswith(
            "Upgrade django to 3.2.13, 4.0.4. Upgrade to one of the fixed versions"
        )

    def test_the_standing_advice_survives_the_version_specific_advice(self) -> None:
        assert self.finding().remediation.endswith(CLAIMS.by_code["PYSEC"].remediation)

    def test_with_no_fix_published_the_advice_is_the_claim_s_own(self) -> None:
        # The contrast for the two above: no fix means no version to name, and
        # the finding falls back to the standing advice rather than to "upgrade
        # django to " with nothing after it.
        finding = self.finding(fix_versions=())
        assert finding.remediation == CLAIMS.by_code["PYSEC"].remediation
        assert "Upgrade django to" not in finding.remediation

    def test_the_evidence_says_a_fix_exists(self) -> None:
        assert self.finding().evidence[0].content == (
            "PYSEC-2024-1 affects django 3.2.0; fixed in 3.2.13, 4.0.4\nA directory traversal."
        )

    def test_the_evidence_says_when_one_does_not(self) -> None:
        assert self.finding(fix_versions=()).evidence[0].content == (
            "PYSEC-2024-1 affects django 3.2.0; no fixed version is published"
            "\nA directory traversal."
        )

    def test_an_advisory_with_no_prose_still_says_something(self) -> None:
        finding = self.finding(description="")
        assert finding.message == "PYSEC-2024-1 affects this version"
        assert finding.evidence[0].content == (
            "PYSEC-2024-1 affects django 3.2.0; fixed in 3.2.13, 4.0.4"
        )

    def test_the_properties_name_the_tool_the_database_and_the_pin(self) -> None:
        assert self.finding().properties == {
            "external": "pip-audit",
            "code": "PYSEC",
            "package": "django",
            "version": "3.2.0",
        }

    def test_an_advisory_cannot_be_edited_after_it_is_read(self) -> None:
        advisory = self.advisory()
        assert not hasattr(advisory, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            advisory.version = "4.0"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "missing", [{"family": None}, {"severity": None}, {"family": None, "severity": None}]
    )
    def test_a_claim_missing_either_half_of_a_rank_is_refused(
        self, missing: dict[str, object]
    ) -> None:
        # Reachable because only an ADOPT claim is required to carry both;
        # changing PYSEC to SUBSUMED would leave them unset, and a finding
        # ranked at no severity is worse than no finding.
        fields = {"family": Family.DJS, "severity": Severity.HIGH, **missing}
        claimed = Claimed(
            code="PYSEC",
            claim=Claim.SUBSUMED,
            why="covered",
            ours=("DJS-001",),
            **fields,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="needs a family and a severity"):
            as_finding(self.advisory(), claimed, PIP_AUDIT)


class TestADependencyWeDidNotDeclare:
    """pip-audit answers about the file it was given, but not always in our words.

    It normalises names, and a report can name a package no line of ours matched.
    Dropping it would be losing a vulnerability over a spelling, so it is kept
    and pointed at the top of the file.
    """

    def payload(self, name: str) -> str:
        return json.dumps(
            {
                "dependencies": [
                    {
                        "name": name,
                        "version": "3.2.0",
                        "vulns": [{"id": "PYSEC-2024-1", "fix_versions": [], "aliases": []}],
                    }
                ]
            }
        )

    def test_a_name_spelled_differently_still_finds_its_line(self) -> None:
        (advisory,) = parse(
            self.payload("Django"), _requirements("x==1", "django==3.2.0"), "requirements.txt"
        )
        assert (advisory.line, advisory.raw) == (2, "django==3.2.0")

    def test_a_package_we_never_declared_is_kept_and_reconstructed(self) -> None:
        (advisory,) = parse(self.payload("django"), _requirements("x==1"), "requirements.txt")
        assert advisory.line == 1
        assert advisory.raw == "django==3.2.0"


class TestTheDecision:
    def test_the_table_rules_on_databases_rather_than_advisories(self) -> None:
        # An advisory id is minted daily; no table could rule on one.
        assert [c.code for c in CLAIMS.claims] == ["PYSEC"]
        assert CLAIMS.claims[0].claim is Claim.ADOPT

    def test_resolution_is_off_and_that_is_deliberate(self) -> None:
        # `--disable-pip` is what stops pip-audit building a virtualenv and
        # running pip over the target's requirements, which would download and
        # build packages the static tier promises never to execute.
        assert "--disable-pip" in ARGUMENTS
        assert "--no-deps" in ARGUMENTS
        assert ARGUMENTS[
            ARGUMENTS.index("--vulnerability-service") : ARGUMENTS.index("--vulnerability-service")
            + 2
        ] == ("--vulnerability-service", "osv")


class TestRunningTheTool:
    """Driven by a stub on disk, because the real tool reaches the network.

    Every other test here reads a recorded payload, which proves the parser but
    not the run. These are the only tests that exercise the requirements file we
    write, the arguments we pass and the report we read back, and they are the
    reason `collect` is checkable at all without a vulnerability database.
    """

    def stub(self, tmp_path: Path, action: str) -> str:
        """A fake pip-audit. `action` runs once `--version` is dealt with."""
        script = tmp_path / "bin" / "fake-pip-audit"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--version' in sys.argv:\n"
            "    print('pip-audit 2.10.1')\n"
            "    raise SystemExit(0)\n"
            "given = sys.argv[sys.argv.index('-r') + 1]\n"
            "destination = sys.argv[sys.argv.index('-o') + 1]\n" + action
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(script)

    def echo(self, tmp_path: Path) -> tuple[str, Path]:
        """A stub that records how it was called, and finds nothing."""
        log = tmp_path / "call.json"
        return (
            self.stub(
                tmp_path,
                "import json, pathlib\n"
                f"pathlib.Path({str(log)!r}).write_text(json.dumps("
                "{'argv': sys.argv[1:], 'requirements': open(given).read()}))\n"
                "open(destination, 'w').write(json.dumps({'dependencies': [], 'fixes': []}))\n",
            ),
            log,
        )

    def called(self, project: Path, tmp_path: Path) -> tuple[list[str], str]:
        """The argument list the tool saw, and the file it was handed."""
        executable, log = self.echo(tmp_path)
        report = PipAuditAdapter(executable=executable).collect(project)
        assert report.diagnostics == (), report.diagnostics
        recorded = json.loads(log.read_text())
        return list(recorded["argv"]), str(recorded["requirements"])

    def test_the_findings_come_from_the_file_and_not_the_pipe(self, project: Path) -> None:
        # The ruff adapter shipped broken for exactly this reason: `live.runner`
        # caps a captured stream at 1 MiB, and a JSON report truncated mid-object
        # parses as nothing at all. This stub writes a megabyte of noise to stdout
        # and the real answer to the file, so a reader of stdout finds nothing.
        executable = self.stub(
            project,
            "sys.stdout.write('#' * (1 << 20))\n"
            f"open(destination, 'w').write(open({str(RECORDED_PATH)!r}).read())\n",
        )
        report = PipAuditAdapter(executable=executable).collect(project)
        assert report.diagnostics == ()
        assert {f.rule_id for f in report.findings} == {
            "PIP-AUDIT-PYSEC-2022-190",
            "PIP-AUDIT-PYSEC-2023-13",
            "PIP-AUDIT-PYSEC-2026-2090",
            "PIP-AUDIT-PYSEC-2026-1872",
        }

    def test_resolution_is_disabled_on_the_command_line(
        self, project: Path, tmp_path: Path
    ) -> None:
        # Not a style assertion. Without this flag pip-audit builds a virtualenv
        # and runs pip against the target's requirements, which downloads and
        # executes package build backends -- the one thing the static tier
        # promises never to do.
        argv, _ = self.called(project, tmp_path)
        assert "--disable-pip" in argv

    def test_the_database_and_the_format_are_pinned_on_the_command_line(
        self, project: Path, tmp_path: Path
    ) -> None:
        argv, _ = self.called(project, tmp_path)
        for flag, value in (
            ("--vulnerability-service", "osv"),
            ("--format", "json"),
            ("--progress-spinner", "off"),
        ):
            assert argv[argv.index(flag) + 1] == value, argv

    def test_it_audits_a_file_of_our_own_rather_than_the_target_s(
        self, project: Path, tmp_path: Path
    ) -> None:
        # A read-only checkout is normal, and the file we hand over holds only
        # the lines we chose -- so it cannot be the target's own.
        argv, handed = self.called(project, tmp_path)
        given = Path(argv[argv.index("-r") + 1])
        assert not given.is_relative_to(project)
        assert handed == "django==3.2.0\nrequests==2.19.0\njinja2==2.10\n"

    def test_only_the_exact_pins_are_handed_over(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "requirements.txt").write_text("django==3.2.0\nbabel\nurllib3>=1.26\n")
        executable, log = self.echo(tmp_path)
        report = PipAuditAdapter(executable=executable).collect(project)
        assert json.loads(log.read_text())["requirements"] == "django==3.2.0\n"
        assert report.diagnostics == (
            "requirements.txt: 2 of 3 requirements are not pinned to an exact "
            "version and were not audited (babel, urllib3>=1.26)",
        )

    def test_the_unaudited_list_is_cut_off_rather_than_printed_whole(self, tmp_path: Path) -> None:
        # pretix pins 12 of 76 exactly. A diagnostic naming all 64 is a wall of
        # text nobody reads, so it names three and says there are more.
        project = tmp_path / "project"
        project.mkdir()
        (project / "requirements.txt").write_text(
            "django==3.2.0\n" + "".join(f"pkg{i}\n" for i in range(5))
        )
        executable, _ = self.echo(tmp_path)
        (diagnostic,) = PipAuditAdapter(executable=executable).collect(project).diagnostics
        assert diagnostic == (
            "requirements.txt: 5 of 6 requirements are not pinned to an exact "
            "version and were not audited (pkg0, pkg1, pkg2, ...)"
        )

    def test_a_file_with_nothing_pinned_is_never_run(self, tmp_path: Path) -> None:
        # `--disable-pip` refuses the whole file if one line is unpinned, so a
        # file with no exact pin has nothing to ask about.
        project = tmp_path / "project"
        project.mkdir()
        (project / "requirements.txt").write_text("babel\n")
        executable, log = self.echo(tmp_path)
        report = PipAuditAdapter(executable=executable).collect(project)
        assert not log.exists(), "pip-audit was run with nothing to audit"
        assert len(report.diagnostics) == 1

    def test_a_project_with_no_manifest_says_so(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        executable, _ = self.echo(tmp_path)
        report = PipAuditAdapter(executable=executable).collect(project)
        assert report.ran
        assert report.findings == ()
        assert report.diagnostics == ("no production dependency manifest was found",)

    def test_a_development_manifest_is_not_audited(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "requirements-dev.txt").write_text("django==3.2.0\n")
        executable, _ = self.echo(tmp_path)
        report = PipAuditAdapter(executable=executable).collect(project)
        assert report.diagnostics == ("no production dependency manifest was found",)

    def test_a_tool_that_writes_nothing_is_reported_rather_than_read(self, project: Path) -> None:
        executable = self.stub(project, "raise SystemExit(2)\n")
        report = PipAuditAdapter(executable=executable).collect(project)
        assert report.findings == ()
        (diagnostic,) = report.diagnostics
        assert diagnostic.startswith("requirements.txt: ")
        # Reported as a run that said nothing, not as output we failed to read.
        # Without the guard an empty file reaches the parser and the reader is
        # told the JSON was malformed, which sends them looking for a report
        # that was never written.
        assert "could not be read" not in diagnostic

    def test_a_report_that_is_not_json_is_reported_rather_than_raised(self, project: Path) -> None:
        executable = self.stub(project, "open(destination, 'w').write('not json')\n")
        report = PipAuditAdapter(executable=executable).collect(project)
        assert report.findings == ()
        (diagnostic,) = report.diagnostics
        assert diagnostic.startswith("requirements.txt: ")
        assert "its output could not be read" in diagnostic

    def test_each_manifest_is_one_run_and_each_finding_names_its_own_file(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "requirements.txt").write_text("django==3.2.0\n")
        (project / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "1"\ndependencies = ["django==3.2.0"]\n'
        )
        executable = self.stub(
            project, f"open(destination, 'w').write(open({str(RECORDED_PATH)!r}).read())\n"
        )
        report = PipAuditAdapter(executable=executable).collect(project)
        assert {f.location.file for f in report.findings} == {
            "requirements.txt",
            "pyproject.toml",
        }


class TestWhenItCannotRun:
    def test_a_missing_tool_degrades_instead_of_failing(self, project: Path) -> None:
        report = PipAuditAdapter(executable="pip-audit-not-installed").collect(project)
        assert not report.ran
        assert report.findings == ()
        assert report.diagnostics == ()
        assert "unavailable" in report.explain()

    def test_a_missing_tool_is_silent_rather_than_merely_finding_nothing(
        self, project: Path
    ) -> None:
        # The contrast for the test above: an installed tool that finds nothing
        # is a run, and the report has to be able to tell the two apart.
        executable = TestRunningTheTool().stub(
            project, "open(destination, 'w').write('{\"dependencies\": []}')\n"
        )
        assert PipAuditAdapter(executable=executable).collect(project).ran

    def test_the_adapter_cannot_be_reconfigured_after_it_is_built(self) -> None:
        adapter = PipAuditAdapter()
        assert not hasattr(adapter, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            adapter.executable = "something-else"  # type: ignore[misc]

    def test_the_report_is_filed_under_the_tool_s_name(self, project: Path) -> None:
        executable = TestRunningTheTool().stub(
            project, "open(destination, 'w').write('{\"dependencies\": []}')\n"
        )
        adapter = PipAuditAdapter(executable=executable)
        assert adapter.name == "pip-audit"
        assert adapter.collect(project).tool == "pip-audit"
        assert adapter.probe().tool == "pip-audit"


def _requirements(*raw: str) -> tuple[manifest.Requirement, ...]:
    return tuple(
        manifest.Requirement(name=re.split(r"[\[=><~!]", spec)[0], raw=spec, line=index + 1)
        for index, spec in enumerate(raw)
    )


def _manifest(path: Path, entry: tuple[str, int]) -> manifest.Manifest:
    name, line = entry
    return manifest.Manifest(
        path=path,
        kind="pyproject",
        group="",
        development=False,
        requirements=(manifest.Requirement(name=name, raw=name, line=line),),
    )


def test_the_recorded_output_is_real(project: Path) -> None:
    """A control on the fixture itself.

    Everything above reads one file. If that file stopped being what pip-audit
    emits, every test here would keep passing while the adapter broke, so the
    shape is asserted against what the tool documents rather than assumed.
    """
    document = json.loads(RECORDED)
    assert set(document) == {"dependencies", "fixes"}
    for dependency in document["dependencies"]:
        assert set(dependency) >= {"name", "version", "vulns"}
        for vulnerability in dependency["vulns"]:
            assert set(vulnerability) >= {"id", "fix_versions", "aliases", "description"}
