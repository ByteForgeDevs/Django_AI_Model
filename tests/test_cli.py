"""CLI contract: exit codes and output routing are what CI depends on."""

import json
import re
import shutil
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from djaudit import engine
from djaudit.baseline import Baseline
from djaudit.cli import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, app
from djaudit.triage import Triage, Verdict

runner = CliRunner()

ONLY_DEBUG = ["--select", "DJS-001"]
"""CLI mechanics -- exit codes, output files, baselines -- are the subject of
these tests; the fixtures are only a source of findings. Pinning the run to one
rule keeps the assertions about the mechanism rather than about how many rules
the catalogue happens to contain."""


class TestExitCodes:
    def test_findings_at_or_above_fail_on_exit_one(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project)])
        assert result.exit_code == EXIT_FINDINGS

    def test_raising_fail_on_above_the_worst_finding_exits_zero(self, overridden_project):
        result = runner.invoke(app, ["run", str(overridden_project), *ONLY_DEBUG])
        assert result.exit_code == EXIT_OK

    def test_findings_below_fail_on_still_exit_zero(self, overridden_project):
        result = runner.invoke(
            app,
            [
                "run",
                str(overridden_project),
                *ONLY_DEBUG,
                "--min-severity",
                "info",
                "--min-confidence",
                "tentative",
                "--fail-on",
                "high",
            ],
        )
        assert "DJS-001" in result.output, result.output
        assert result.exit_code == EXIT_OK

    def test_a_bad_path_is_a_tool_error_not_a_finding(self, tmp_path):
        """CI must distinguish 'found problems' from 'installation is broken'."""
        result = runner.invoke(app, ["run", str(tmp_path / "nope")])
        assert result.exit_code == EXIT_ERROR

    def test_a_file_instead_of_a_directory_is_a_tool_error(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1")
        assert runner.invoke(app, ["run", str(target)]).exit_code == EXIT_ERROR

    def test_an_unreadable_baseline_is_a_tool_error(self, vulnerable_project, tmp_path):
        bad = tmp_path / "baseline.json"
        bad.write_text("{nope")
        result = runner.invoke(app, ["run", str(vulnerable_project), "--baseline", str(bad)])
        assert result.exit_code == EXIT_ERROR

    def test_an_unauditable_project_is_a_tool_error_not_a_pass(self, tmp_path):
        """Exiting 0 here would tell CI a project is clean that nothing examined."""
        root = tmp_path / "proj"
        (root / "conf").mkdir(parents=True)
        (root / "manage.py").write_text("import os\n")
        (root / "conf" / "settings.py").write_text(
            "from configurations import Configuration\n\n"
            "class Base(Configuration):\n    SECRET_KEY = 'x'\n"
        )
        result = runner.invoke(app, ["run", str(root)])
        assert result.exit_code == EXIT_ERROR
        assert "incomplete" in result.output

    def test_the_diagnostic_reaches_json_consumers(self, tmp_path):
        root = tmp_path / "proj"
        (root / "conf").mkdir(parents=True)
        (root / "manage.py").write_text("import os\n")
        (root / "conf" / "settings.py").write_text("X = 1\n")
        out = tmp_path / "out.json"
        runner.invoke(app, ["run", str(root), "-f", "json", "-o", str(out)])
        payload = json.loads(out.read_text())
        assert payload["diagnostics"][0]["code"] == "no-settings-module"
        assert payload["diagnostics"][0]["blocking"] is True


class TestOutputFormats:
    def test_json_output_is_parseable(self, vulnerable_project, tmp_path):
        out = tmp_path / "out.json"
        runner.invoke(
            app, ["run", str(vulnerable_project), *ONLY_DEBUG, "-f", "json", "-o", str(out)]
        )
        assert len(json.loads(out.read_text())["findings"]) == 2

    def test_sarif_output_is_parseable(self, vulnerable_project, tmp_path):
        out = tmp_path / "out.sarif"
        runner.invoke(app, ["run", str(vulnerable_project), "-f", "sarif", "-o", str(out)])
        assert json.loads(out.read_text())["version"] == "2.1.0"

    @pytest.mark.parametrize("fmt", ["terminal", "json", "sarif"])
    def test_output_parent_directories_are_created(self, vulnerable_project, tmp_path, fmt):
        """Every format must behave the same here.

        Terminal previously opened the file directly while JSON and SARIF
        created parents first, so `-o reports/out.txt` failed on a fresh
        checkout for one format out of three.
        """
        out = tmp_path / "reports" / "nested" / f"out.{fmt}"
        result = runner.invoke(app, ["run", str(vulnerable_project), "-f", fmt, "-o", str(out)])
        assert result.exit_code in {EXIT_OK, EXIT_FINDINGS}, result.output
        assert out.is_file()
        assert out.read_text().strip()

    def test_terminal_file_output_names_the_rule(self, vulnerable_project, tmp_path):
        out = tmp_path / "reports" / "out.txt"
        runner.invoke(app, ["run", str(vulnerable_project), "-f", "terminal", "-o", str(out)])
        assert "DJS-001" in out.read_text()

    def test_terminal_output_names_the_rule_and_location(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project)])
        assert "DJS-001" in result.output
        assert "production.py" in result.output


class TestBaselineWorkflow:
    def test_writing_a_baseline_then_running_reports_nothing_new(
        self, vulnerable_project, tmp_path
    ):
        baseline = tmp_path / "baseline.json"

        written = runner.invoke(
            app, ["run", str(vulnerable_project), "--write-baseline", str(baseline)]
        )
        assert written.exit_code == EXIT_OK

        second = runner.invoke(app, ["run", str(vulnerable_project), "--baseline", str(baseline)])
        assert second.exit_code == EXIT_OK
        assert "No findings" in second.output

    def test_baseline_captures_findings_hidden_by_default_thresholds(
        self, overridden_project, tmp_path
    ):
        """Otherwise lowering a threshold later resurfaces old findings as 'new'."""
        baseline = tmp_path / "baseline.json"
        runner.invoke(
            app,
            ["run", str(overridden_project), *ONLY_DEBUG, "--write-baseline", str(baseline)],
        )
        assert len(Baseline.load(baseline)) == 1


class TestRuleSelection:
    def test_select_restricts_to_named_rules(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project), "--select", "DJS-001"])
        assert "DJS-001" in result.output

    def test_family_filter_can_silence_everything(self, vulnerable_project):
        result = runner.invoke(app, ["run", str(vulnerable_project), "--family", "DJM"])
        assert result.exit_code == EXIT_OK
        assert "No findings" in result.output


class TestOtherCommands:
    def test_rules_lists_the_catalogue(self):
        result = runner.invoke(app, ["rules"])
        assert result.exit_code == EXIT_OK
        assert "DJS-001" in result.output

    def test_version_prints_a_version(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == EXIT_OK
        assert result.output.strip()

    def test_eval_passes_on_the_fixtures(self, vulnerable_project):
        result = runner.invoke(app, ["eval", str(vulnerable_project)])
        assert result.exit_code == EXIT_OK
        assert "evaluation passed" in result.output

    def test_eval_fails_loudly_on_a_regression(self, vulnerable_project, tmp_path):
        manifest = tmp_path / "expected.json"
        manifest.write_text(json.dumps({"expected": []}))
        result = runner.invoke(app, ["eval", str(vulnerable_project), "--manifest", str(manifest)])
        assert result.exit_code == EXIT_FINDINGS
        assert "UNEXPECTED" in result.output


class TestBenchmarkCommand:
    """The precision gate's CLI surface. CI reads only the exit code."""

    def seed(self, tmp_path, entries=(), rate=0.10):
        path = tmp_path / "triage.json"
        Triage(target="fixture", entries=tuple(entries), max_false_positive_rate=rate).save(path)
        return path

    def test_untriaged_findings_exit_one(self, vulnerable_project, tmp_path):
        path = self.seed(tmp_path)
        result = runner.invoke(app, ["benchmark", str(vulnerable_project), "--triage", str(path)])
        assert result.exit_code == EXIT_FINDINGS
        assert "untriaged" in result.output

    def test_a_missing_triage_file_is_a_tool_error(self, vulnerable_project, tmp_path):
        result = runner.invoke(
            app, ["benchmark", str(vulnerable_project), "--triage", str(tmp_path / "nope.json")]
        )
        assert result.exit_code == EXIT_ERROR

    def test_a_bad_project_path_is_a_tool_error(self, tmp_path):
        path = self.seed(tmp_path)
        result = runner.invoke(app, ["benchmark", str(tmp_path / "nope"), "--triage", str(path)])
        assert result.exit_code == EXIT_ERROR

    def test_update_seeds_untriaged_findings_as_false_positive(self, vulnerable_project, tmp_path):
        # Unreviewed entries must never count in our favour.
        path = self.seed(tmp_path)
        result = runner.invoke(
            app, ["benchmark", str(vulnerable_project), "--triage", str(path), "--update"]
        )
        assert result.exit_code == EXIT_FINDINGS

        written = Triage.load(path)
        # Counting findings here would only pin this test to the size of the
        # rule catalogue, and it has already been bumped once per rule added.
        # What --update promises is that every finding gets an entry and that
        # none of them is seeded in our favour.
        assert len(written) == len(engine.run(vulnerable_project).findings)
        assert {e.verdict for e in written.entries} == {Verdict.FALSE_POSITIVE}

    def test_update_never_overwrites_an_existing_verdict(self, vulnerable_project, tmp_path):
        path = self.seed(tmp_path)
        runner.invoke(
            app, ["benchmark", str(vulnerable_project), "--triage", str(path), "--update"]
        )

        reviewed = Triage.load(path)
        first = reviewed.entries[0]
        reviewed.save(path)
        Triage(
            target="fixture",
            entries=(
                replace(first, verdict=Verdict.TRUE_POSITIVE, note="checked"),
                *reviewed.entries[1:],
            ),
        ).save(path)

        runner.invoke(
            app, ["benchmark", str(vulnerable_project), "--triage", str(path), "--update"]
        )

        after = Triage.load(path).by_fingerprint[first.fingerprint]
        assert after.verdict is Verdict.TRUE_POSITIVE
        assert after.note == "checked"

    def test_a_fully_triaged_project_exits_zero(self, vulnerable_project, tmp_path):
        path = self.seed(tmp_path)
        runner.invoke(
            app, ["benchmark", str(vulnerable_project), "--triage", str(path), "--update"]
        )

        seeded = Triage.load(path)
        Triage(
            target="fixture",
            entries=tuple(
                replace(e, verdict=Verdict.TRUE_POSITIVE, note="planted") for e in seeded.entries
            ),
        ).save(path)

        result = runner.invoke(app, ["benchmark", str(vulnerable_project), "--triage", str(path)])
        assert result.exit_code == EXIT_OK
        assert "precision 100.0%" in result.output


class TestScoringCommandsRefuseAnIncompleteRun:
    """`run` has refused to exit 0 on a blocking diagnostic since Phase 1.

    `eval` and `benchmark` did not, so whether CI noticed a project the tool
    could not read depended on which command the workflow happened to call --
    and those two are the only commands CI calls. Both score silence, and
    silence is what an unread project produces: every control case satisfied,
    nothing untriaged, precision 100%.
    """

    def unreadable(self, tmp_path):
        root = tmp_path / "proj"
        (root / "conf").mkdir(parents=True)
        (root / "manage.py").write_text("import os\n")
        (root / "conf" / "settings.py").write_text(
            "from configurations import Configuration\n\n"
            "class Base(Configuration):\n    SECRET_KEY = 'x'\n    DEBUG = True\n"
        )
        return root

    def test_eval_exits_non_zero(self, tmp_path):
        root = self.unreadable(tmp_path)
        (root / "expected.json").write_text(
            json.dumps({"must_not_report": [{"rule_id": "DJS-001", "file": "conf/settings.py"}]})
        )

        result = runner.invoke(app, ["eval", str(root)])

        assert result.exit_code == EXIT_FINDINGS
        assert "INCOMPLETE" in result.output

    def test_benchmark_exits_non_zero(self, tmp_path):
        root = self.unreadable(tmp_path)
        path = tmp_path / "triage.json"
        Triage(target="fixture").save(path)

        result = runner.invoke(app, ["benchmark", str(root), "--triage", str(path)])

        assert result.exit_code == EXIT_FINDINGS
        assert "analysis incomplete" in result.output

    def test_a_readable_project_is_unaffected(self, vulnerable_project):
        # The guard must not turn every fixture red; it fires on the projects
        # nothing could read, not on the ones with nothing to say.
        result = runner.invoke(app, ["eval", str(vulnerable_project)])

        assert result.exit_code == EXIT_OK


class TestJobSummary:
    """CI writes markdown to $GITHUB_STEP_SUMMARY so a red build explains itself."""

    def test_eval_summary_reports_recall(self, vulnerable_project, tmp_path):
        out = tmp_path / "summary.md"
        runner.invoke(app, ["eval", str(vulnerable_project), "--summary", str(out)])

        text = out.read_text()
        assert "## Recall" in text
        assert "| recall | 100.0% |" in text

    def test_benchmark_summary_reports_untriaged(self, vulnerable_project, tmp_path):
        triage = tmp_path / "triage.json"
        Triage(target="fixture").save(triage)
        out = tmp_path / "summary.md"

        runner.invoke(
            app,
            ["benchmark", str(vulnerable_project), "--triage", str(triage), "--summary", str(out)],
        )

        text = out.read_text()
        assert "## Precision — fixture" in text
        assert "❌ fail" in text
        assert f"Untriaged ({len(engine.run(vulnerable_project).findings)})" in text
        assert "--update" in text

    def test_summaries_append_rather_than_truncate(self, vulnerable_project, tmp_path):
        # Several CI steps share one summary file; truncating loses the others.
        out = tmp_path / "summary.md"
        out.write_text("## Existing section\n\n")

        runner.invoke(app, ["eval", str(vulnerable_project), "--summary", str(out)])
        runner.invoke(app, ["eval", str(vulnerable_project), "--summary", str(out)])

        text = out.read_text()
        assert "## Existing section" in text
        assert text.count("## Recall") == 2

    def test_summary_path_parents_are_created(self, vulnerable_project, tmp_path):
        out = tmp_path / "nested" / "deeper" / "summary.md"
        runner.invoke(app, ["eval", str(vulnerable_project), "--summary", str(out)])
        assert out.is_file()


class TestTriageCommand:
    """The command must be useful with no model, and honest that it had none."""

    def test_it_ranks_the_orm_fixture_without_a_model(self, orm_project):
        result = runner.invoke(app, ["triage", str(orm_project)])

        assert result.exit_code == EXIT_OK
        assert "no model was consulted" in result.output

    def test_settled_rules_are_labelled_as_borrowed(self, orm_project):
        result = runner.invoke(app, ["triage", str(orm_project)])

        # DJP-001 ships in the corpus prior, so it must be judged without a
        # call and marked as coming from the corpus rather than from a model.
        assert "corpus" in result.output
        assert "true_positive" in result.output

    def test_unsettled_findings_are_undecided_rather_than_dismissed(self, orm_project):
        """The failure that would matter.

        With no model, a finding the corpus cannot settle must surface as
        undecided. Rendering it as an accepted risk would be the tool quietly
        telling someone to ignore a defect nobody looked at.
        """
        result = runner.invoke(app, ["triage", str(orm_project)])

        assert "abstained" in result.output
        assert "unavailable" in result.output
        assert "0 judged acceptable" in result.output

    def test_no_llm_overrides_a_config_that_enables_one(self, tmp_path, orm_project):
        project = tmp_path / "proj"
        shutil.copytree(orm_project, project)
        project.joinpath("pyproject.toml").write_text(
            '[tool.djaudit.llm]\nenabled = true\nprovider = "openai"\napi_key_env = "NOPE_KEY"\n'
        )

        result = runner.invoke(app, ["triage", str(project), "--no-llm"])

        assert result.exit_code == EXIT_OK
        assert "no model was consulted" in result.output

    def test_a_missing_path_is_a_tool_error(self, tmp_path):
        result = runner.invoke(app, ["triage", str(tmp_path / "nope")])
        assert result.exit_code == EXIT_ERROR

    def test_suggest_offers_nothing_when_no_model_was_consulted(self, orm_project):
        """The whole point of the flag, on the path everyone will actually take.

        Offline, every verdict is borrowed from the corpus or absent, and
        neither is grounds for writing a permanent comment into someone's
        source. The command has to say so rather than print an empty section.
        """
        result = runner.invoke(app, ["triage", str(orm_project), "--suggest"])

        assert result.exit_code == EXIT_OK
        assert "no suppression is justified by this run" in result.output

    def test_a_corpus_accepted_risk_is_refused_out_loud(self, drf_project):
        """The dangerous case: a verdict that *looks* like grounds and is not.

        DJA-010 is in the corpus prior as an accepted risk, so this run judges
        it acceptable without asking anyone. Ranking on that basis is fine and
        reversible. Writing `# djaudit: ignore[DJA-010]` into the file on that
        basis is neither, so it must be refused and the refusal must be said.
        """
        result = runner.invoke(app, ["triage", str(drf_project), "--suggest"])

        assert "1 not offered" in result.output
        assert "DJA-010" in result.output
        assert "corpus verdict cannot justify" in result.output.replace("\n", " ")

    def test_group_collapses_repeats_of_one_rule_in_one_file(self, orm_project):
        result = runner.invoke(app, ["triage", str(orm_project), "--group"])

        assert result.exit_code == EXIT_OK
        assert "themes" in result.output
        assert "fewer things to read" in result.output

    def test_the_grouped_view_still_says_no_model_was_consulted(self, orm_project):
        """The one place a summary could mislead badly.

        A grouped table looks more authoritative than a flat one -- fewer rows,
        each standing for several findings. If the provenance line went missing
        from this view, "abstained" would read as a considered judgement.
        """
        result = runner.invoke(app, ["triage", str(orm_project), "--group"])

        assert "no model was consulted" in result.output
        assert "settled from the recorded corpus" in result.output

    def test_grouping_changes_the_presentation_and_not_the_count(self, orm_project):
        flat = runner.invoke(app, ["triage", str(orm_project)])
        grouped = runner.invoke(app, ["triage", str(orm_project), "--group"])

        count = re.search(r"(\d+) findings", flat.output)
        assert count and f"{count.group(1)} findings in " in grouped.output

    def test_group_is_off_unless_asked_for(self, orm_project):
        result = runner.invoke(app, ["triage", str(orm_project)])

        assert "themes" not in result.output

    def test_suggest_is_off_unless_asked_for(self, orm_project):
        result = runner.invoke(app, ["triage", str(orm_project)])

        assert "suppression" not in result.output
        assert "not offered" not in result.output

    def test_it_refuses_an_incomplete_run(self, tmp_path):
        root = tmp_path / "proj"
        (root / "conf").mkdir(parents=True)
        (root / "manage.py").write_text("import os\n")
        (root / "conf" / "settings.py").write_text(
            "from configurations import Configuration\n\n"
            "class Base(Configuration):\n    SECRET_KEY = 'x'\n    DEBUG = True\n"
        )

        result = runner.invoke(app, ["triage", str(root)])

        assert result.exit_code == EXIT_ERROR


class TestExplainCommand:
    """Explain must be useful offline, and must refuse rather than guess."""

    @staticmethod
    def a_fingerprint(project) -> str:
        result = runner.invoke(app, ["run", str(project), "--format", "json"])
        fingerprint = json.loads(result.output)["findings"][0]["fingerprint"]
        assert isinstance(fingerprint, str)
        return fingerprint

    def test_it_explains_a_real_finding(self, orm_project):
        fingerprint = self.a_fingerprint(orm_project)

        result = runner.invoke(app, ["explain", fingerprint, str(orm_project)])

        assert result.exit_code == EXIT_OK
        assert "What is wrong" in result.output
        assert "What to do" in result.output
        assert "References" in result.output

    def test_a_prefix_is_enough(self, orm_project):
        fingerprint = self.a_fingerprint(orm_project)

        result = runner.invoke(app, ["explain", fingerprint[:8], str(orm_project)])

        assert result.exit_code == EXIT_OK
        assert "What is wrong" in result.output

    def test_an_unknown_fingerprint_is_a_tool_error(self, orm_project):
        result = runner.invoke(app, ["explain", "0123456789abcdef", str(orm_project)])

        assert result.exit_code == EXIT_ERROR
        # Rich wraps at the terminal width and leaves the trailing space
        # before the break, so both have to be collapsed.
        assert "may have been fixed" in " ".join(result.output.split())

    def test_a_prefix_too_short_to_mean_anything_is_refused(self, orm_project):
        result = runner.invoke(app, ["explain", "ab", str(orm_project)])

        assert result.exit_code == EXIT_ERROR
        assert "too short" in result.output

    def test_a_missing_path_is_a_tool_error(self, tmp_path):
        result = runner.invoke(app, ["explain", "abcdef", str(tmp_path / "nope")])

        assert result.exit_code == EXIT_ERROR

    def test_impact_adds_the_framing_for_a_non_specialist(self, orm_project):
        fingerprint = self.a_fingerprint(orm_project)

        result = runner.invoke(app, ["explain", fingerprint, str(orm_project), "--impact"])

        assert result.exit_code == EXIT_OK
        assert "Who this affects" in result.output
        assert "How urgent" in result.output
        assert "When this does not apply to you" in result.output

    def test_impact_is_off_unless_asked_for(self, orm_project):
        fingerprint = self.a_fingerprint(orm_project)

        result = runner.invoke(app, ["explain", fingerprint, str(orm_project)])

        assert "Who this affects" not in result.output

    def test_it_consults_no_model_and_says_nothing_about_one(self, orm_project):
        """Offline is not a degraded mode here, so there is nothing to disclose."""
        fingerprint = self.a_fingerprint(orm_project)

        result = runner.invoke(app, ["explain", fingerprint, str(orm_project)])

        assert "model" not in result.output.lower()
