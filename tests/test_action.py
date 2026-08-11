"""Tests for the composite action in `action.yml`.

The action is a string executed on someone else's runner, so most of what can
go wrong is invisible here. Two things are done about that.

The structural tests below check what a runner would only discover at load
time: a `run` step without `shell:`, an input referenced by a name nobody
declared, a flag the CLI no longer accepts.

`TestTheScriptActuallyRuns` goes further and executes the audit step's script
for real, with its expressions substituted, against a fixture project. That is
the difference between "the YAML looks right" and "the command line this
builds is one djaudit accepts".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_action

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / "action.yml"

EXPRESSION = re.compile(r"\$\{\{(.+?)\}\}", re.DOTALL)
CONDITIONAL = re.compile(
    r"^\s*inputs\.([a-z0-9-]+)\s*&&\s*format\('([^']+)',\s*inputs\.([a-z0-9-]+)\)\s*\|\|\s*''\s*$"
)
PLAIN = re.compile(r"^\s*inputs\.([a-z0-9-]+)\s*$")
STEP_OUTPUT = re.compile(r"^\s*steps\.([a-z0-9-]+)\.outputs\.([a-z0-9-]+)\s*$")


def action() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(ACTION.read_text())
    return loaded


def defaults() -> dict[str, str]:
    return {name: str(spec.get("default", "")) for name, spec in action()["inputs"].items()}


def step(name: str) -> dict[str, Any]:
    for candidate in action()["runs"]["steps"]:
        if candidate.get("name") == name or candidate.get("id") == name:
            found: dict[str, Any] = candidate
            return found
    raise AssertionError(f"no step named {name!r}")


CONDITION = re.compile(r"^\s*inputs\.([a-z0-9-]+)\s*==\s*'([^']*)'\s*$")


def selected(candidate: dict[str, Any], inputs: dict[str, str]) -> bool:
    """Whether a runner would execute this step, given these inputs.

    A composite action's `if:` is what makes `install: false` mean anything, so
    a test that ran every step regardless would be testing a workflow nobody
    can request. Raises on any condition it does not understand, rather than
    defaulting to true: a silently-misread condition would put the step back
    and the reason would not be obvious.
    """
    if "if" not in candidate:
        return True
    match = CONDITION.match(str(candidate["if"]))
    if not match:
        raise AssertionError(f"unhandled step condition: {candidate['if']!r}")
    name, expected = match.groups()
    if name not in inputs:
        raise AssertionError(f"step condition names undeclared input {name!r}")
    return inputs[name] == expected


def render(script: str, inputs: dict[str, str], outputs: dict[str, str] | None = None) -> str:
    """Substitute the workflow expressions this action actually uses.

    Anything else raises rather than being left in place, because a silently
    unsubstituted expression would make the script fail for a reason that has
    nothing to do with what the test is checking.
    """

    def one(match: re.Match[str]) -> str:
        body = match.group(1)
        if plain := PLAIN.match(body):
            return inputs[plain.group(1)]
        if conditional := CONDITIONAL.match(body):
            name = conditional.group(1)
            return conditional.group(2).format(inputs[name]) if inputs[name] else ""
        if out := STEP_OUTPUT.match(body):
            return (outputs or {})[out.group(2)]
        raise AssertionError(f"the test cannot render {body.strip()!r}")

    return EXPRESSION.sub(one, script)


class TestTheScriptActuallyRuns:
    """Execute the audit step rather than reading it."""

    def _run(self, inputs: dict[str, str], tmp_path: Path) -> tuple[int, str, dict[str, str]]:
        script = render(step("audit")["run"], inputs)
        env = dict(os.environ, GITHUB_OUTPUT=str(tmp_path / "gh-output"))
        done = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
            check=False,
        )
        written = (tmp_path / "gh-output").read_text() if (tmp_path / "gh-output").exists() else ""
        emitted = dict(line.split("=", 1) for line in written.splitlines() if "=" in line)
        return done.returncode, done.stdout + done.stderr, emitted

    def test_a_clean_project_passes_and_still_writes_a_report(self, tmp_path: Path) -> None:
        target = tmp_path / "clean"
        target.mkdir()
        inputs = defaults() | {"path": str(target), "sarif-file": "out.sarif"}

        code, output, emitted = self._run(inputs, tmp_path)

        assert code == 0, output
        assert emitted["exit-code"] == "0"
        assert (tmp_path / "out.sarif").exists(), "the upload needs a file even when nothing failed"

    def test_findings_do_not_fail_the_step_but_are_recorded(self, tmp_path: Path) -> None:
        """The whole point of the design: findings must not abort the upload."""
        inputs = defaults() | {
            "path": str(ROOT / "tests/fixtures/vulnerable_project"),
            "sarif-file": "out.sarif",
        }

        code, output, emitted = self._run(inputs, tmp_path)

        assert code == 0, (
            f"the audit step must not fail on findings, or the upload is skipped:\n{output}"
        )
        assert emitted["exit-code"] == "1", "but the failure must still be carried forward"
        report = yaml.safe_load((tmp_path / "out.sarif").read_text())
        assert report["runs"][0]["results"], "the fixture is meant to produce findings"

    def test_the_verdict_step_fails_on_what_the_audit_recorded(self, tmp_path: Path) -> None:
        script = render(step("report the verdict")["run"], defaults(), {"exit-code": "1"})
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        assert done.returncode == 1
        assert "::error::" in done.stdout + done.stderr

    def test_the_verdict_step_passes_on_a_clean_audit(self, tmp_path: Path) -> None:
        script = render(step("report the verdict")["run"], defaults(), {"exit-code": "0"})
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        assert done.returncode == 0
        assert "found nothing" in done.stdout

    def test_a_baseline_is_only_passed_when_one_is_given(self, tmp_path: Path) -> None:
        without = render(step("audit")["run"], defaults() | {"baseline": ""})
        assert "--baseline" not in without, "an empty baseline must not reach the CLI"
        with_one = render(step("audit")["run"], defaults() | {"baseline": "b.json"})
        assert "--baseline b.json" in with_one


class TestTheGate:
    """The gate must complain, not merely exit non-zero."""

    @pytest.fixture
    def tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        copy = tmp_path / "action.yml"
        copy.write_text(ACTION.read_text())
        monkeypatch.setattr(check_action, "ACTION", copy)
        return copy

    def complaint(self, capsys: pytest.CaptureFixture[str]) -> str:
        assert check_action.check() == 1
        return capsys.readouterr().out

    def rewrite(self, tree: Path, old: str, new: str) -> None:
        text = tree.read_text()
        assert text.count(old) == 1, f"anchor appears {text.count(old)} times"
        tree.write_text(text.replace(old, new, 1))

    def test_the_real_action_passes(self) -> None:
        assert check_action.check() == 0

    def test_a_missing_shell_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(
            tree,
            "      shell: bash\n      run: |\n        set -o pipefail",
            "      run: |\n        set -o pipefail",
        )
        assert "without `shell: bash`" in self.complaint(capsys)

    def test_an_undeclared_input_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, "inputs.fail-on }}'; the findings", "inputs.failon }}'; the findings")
        assert "`inputs.failon` is used but never declared" in self.complaint(capsys)

    def test_an_unused_input_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, '--min-severity "${{ inputs.min-severity }}" \\\n', "")
        assert "'min-severity' is declared but never used" in self.complaint(capsys)

    def test_a_flag_the_cli_does_not_have_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, "--min-confidence ", "--min-certainty ")
        assert "`djaudit run` does not accept" in self.complaint(capsys)

    def test_a_default_outside_the_choices_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, '    default: "high"', '    default: "severe"')
        assert "which --fail-on does not accept" in self.complaint(capsys)

    def test_an_uncaptured_exit_code_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.rewrite(tree, " || code=$?", "")
        assert "does not capture its exit code" in self.complaint(capsys)

    def test_uploading_after_the_verdict_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        document = yaml.safe_load(tree.read_text())
        steps = document["runs"]["steps"]
        steps[2], steps[3] = steps[3], steps[2]
        tree.write_text(yaml.safe_dump(document, sort_keys=False))
        assert "hides the findings" in self.complaint(capsys)

    def test_a_missing_action_is_caught(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tree.unlink()
        assert "action.yml is missing" in self.complaint(capsys)


class TestWhatTheActionPromises:
    def test_every_run_step_declares_bash(self) -> None:
        for candidate in action()["runs"]["steps"]:
            if "run" in candidate:
                assert candidate["shell"] == "bash", candidate.get("name")

    def test_the_version_default_matches_the_package(self) -> None:
        from djaudit import __version__

        assert action()["inputs"]["version"]["default"] == __version__, (
            "the action installs a pinned release, so the pin must be a version "
            "that exists by the time the action is published"
        )

    def test_the_outputs_are_declared(self) -> None:
        assert set(action()["outputs"]) == {"sarif-file", "exit-code"}


class TestTheActionEndToEnd:
    """Run every shell step in order, which is where the design is visible.

    The `uses:` upload step needs a runner and is skipped here; CI runs the
    real thing against a clean fixture so that path is not left unexercised.

    `install` is off, which is how the action is used before there is anything
    to install and how CI invokes it today. It is also the honest setting: the
    install step resolves `djaudit==<version>` from PyPI, and while that is
    unpublished the step can only ever succeed by finding a local copy that
    happens to match -- which is exactly how it used to pass here, through an
    editable install whose recorded metadata still read the previous version.
    """

    def test_findings_produce_a_report_and_then_a_failure(self, tmp_path: Path) -> None:
        inputs = defaults() | {
            "path": str(ROOT / "tests/fixtures/vulnerable_project"),
            "sarif-file": "out.sarif",
            "install": "false",
        }
        outputs: dict[str, str] = {}
        results = []
        for candidate in action()["runs"]["steps"]:
            if "run" not in candidate or not selected(candidate, inputs):
                continue
            done = subprocess.run(
                ["bash", "-c", render(candidate["run"], inputs, outputs)],
                capture_output=True,
                text=True,
                cwd=tmp_path,
                env=dict(os.environ, GITHUB_OUTPUT=str(tmp_path / "gh")),
                check=False,
            )
            for line in (
                (tmp_path / "gh").read_text().splitlines() if (tmp_path / "gh").exists() else []
            ):
                if "=" in line:
                    key, value = line.split("=", 1)
                    outputs[key] = value
            results.append((candidate["name"], done.returncode))

        ran = [name for name, _ in results]
        assert "install djaudit" not in ran, f"install was requested off but ran: {ran}"
        assert (tmp_path / "out.sarif").exists(), "the report must exist by the end"
        assert results[-1][1] == 1, f"the last step must fail on findings: {results}"
        assert all(code == 0 for _, code in results[:-1]), (
            f"nothing before the verdict may fail, or the upload is skipped: {results}"
        )

    def test_install_is_selected_when_it_is_asked_for(self) -> None:
        """The contrast, so `selected` cannot be quietly always-false.

        Without this the previous test would pass just as happily against a
        condition evaluator that skipped every step it was shown.
        """
        install = step("install djaudit")

        assert selected(install, defaults() | {"install": "true"})
        assert not selected(install, defaults() | {"install": "false"})

    def test_the_audit_step_is_not_conditional(self) -> None:
        """A skipped audit would leave the verdict reading a stale report."""
        assert selected(step("audit"), defaults() | {"install": "false"})


class TestTheDocumentedInterface:
    """The doc is the only thing a caller reads, so it must be the real one."""

    def test_the_tables_match_the_action_exactly(self) -> None:
        document = (ROOT / "docs/github-action.md").read_text()
        assert check_action._table(document, "Input") == set(action()["inputs"])
        assert check_action._table(document, "Output") == set(action()["outputs"])

    def test_the_doc_shows_the_permission_the_upload_needs(self) -> None:
        document = (ROOT / "docs/github-action.md").read_text()
        assert "security-events: write" in document, (
            "the upload fails without it, and the failure does not say so"
        )

    def test_the_example_pins_a_version(self) -> None:
        document = (ROOT / "docs/github-action.md").read_text()
        assert re.search(r"uses: [\w-]+/[\w-]+@v\d+\.\d+\.\d+", document), (
            "an unpinned example teaches callers to track a moving branch"
        )
