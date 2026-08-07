"""Tests for fix proposal.

The load-bearing classes are `TestTheAgreementGate` (a fixer may only write a
value its own rule names) and `TestItRefusesRatherThanGuesses`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import djaudit.rules  # noqa: F401  -- registers every rule
from djaudit import engine, registry
from djaudit.llm.edit import DIFF_CONTEXT
from djaudit.llm.fix import (
    FIXERS,
    Fix,
    Refusal,
    assignment_at,
    combined,
    fix_for,
    fixes,
    patch,
    safe_context,
    secret_lines,
)
from djaudit.models import Confidence, Family, Finding, Location, Severity, Tier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_finding(
    rule_id: str = "DJS-001",
    file: str = "settings.py",
    line: int = 1,
    snippet: str = "DEBUG = True",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        title="a title",
        severity=Severity.HIGH,
        confidence=Confidence.CERTAIN,
        family=Family.DJS,
        tier=Tier.STATIC,
        location=Location(file=file, line=line, snippet=snippet),
        message="a message",
        rationale="a rationale",
        remediation="a remediation",
    )


def project(tmp_path: Path, source: str, name: str = "settings.py") -> Path:
    (tmp_path / name).write_text(source, encoding="utf-8")
    return tmp_path


class TestTheAgreementGate:
    """A fixer may not decide for itself what the right value is."""

    def test_the_table_agrees_with_every_rule_it_claims_to_fix(self) -> None:
        for rule_id, fixer in FIXERS.items():
            remediation = " ".join(registry.get(rule_id).meta.remediation.split())

            assert f"{fixer.setting} = {fixer.value}" in remediation, (
                f"{rule_id} does not tell anyone to write "
                f"{fixer.setting} = {fixer.value}, so neither may we"
            )

    def test_every_fixable_rule_exists(self) -> None:
        for rule_id in FIXERS:
            assert registry.get(rule_id).meta.id == rule_id

    def test_hsts_is_deliberately_not_fixable(self) -> None:
        """The case that proves the gate is necessary and not sufficient.

        DJS-007's remediation names 31536000 outright, so a table entry for it
        would pass the gate above. The same remediation says to ramp up rather
        than jump, and HSTS is sticky for the max-age it arrived with -- so the
        one-step fix is one the rule argues against.
        """
        remediation = " ".join(registry.get("DJS-007").meta.remediation.split())

        assert "SECURE_HSTS_SECONDS = 31536000" in remediation
        assert "Ramp up rather than jumping straight there" in remediation
        assert "DJS-007" not in FIXERS

    def test_no_fixer_writes_a_value_that_is_not_a_literal(self) -> None:
        """A computed value would be a second rule engine hiding in a table."""
        assert {f.value for f in FIXERS.values()} <= {"True", "False"}


class TestItRefusesRatherThanGuesses:
    def test_a_rule_with_no_fixer_is_named_not_skipped(self) -> None:
        outcome = fix_for(make_finding(rule_id="DJS-013"), Path("/nowhere"))

        assert isinstance(outcome, Refusal)
        assert "DJS-013" in outcome.reason
        assert "depends on the project" in outcome.reason

    def test_a_masked_value_is_refused_before_anything_is_read(self) -> None:
        """The secret case. No patch may be built from text we do not have."""
        finding = make_finding(rule_id="DJS-001", snippet='SECRET_KEY = "*x<redacted:50 chars>"')

        outcome = fix_for(finding, Path("/nowhere"))

        assert isinstance(outcome, Refusal)
        assert "masked" in outcome.reason

    def test_no_secret_rule_has_a_fixer(self) -> None:
        """Generating a key into a diff would publish a live credential."""
        assert {"DJS-002", "DJS-003", "DJS-004", "DJS-005"} & set(FIXERS) == set()

    def test_an_unreadable_file_is_refused(self, tmp_path: Path) -> None:
        outcome = fix_for(make_finding(file="gone.py"), tmp_path)

        assert isinstance(outcome, Refusal)
        assert "could not be read" in outcome.reason

    def test_a_line_that_moved_is_refused(self, tmp_path: Path) -> None:
        root = project(tmp_path, "# a comment\nDEBUG = True\n")

        outcome = fix_for(make_finding(line=1), root)

        assert isinstance(outcome, Refusal)
        assert "not an assignment to DEBUG" in outcome.reason

    def test_a_different_setting_on_the_right_line_is_refused(self, tmp_path: Path) -> None:
        root = project(tmp_path, "TEMPLATE_DEBUG = True\n")

        outcome = fix_for(make_finding(line=1), root)

        assert isinstance(outcome, Refusal)
        assert "not an assignment to DEBUG" in outcome.reason

    def test_an_already_correct_value_is_refused(self, tmp_path: Path) -> None:
        root = project(tmp_path, "DEBUG = False\n")

        outcome = fix_for(make_finding(line=1), root)

        assert isinstance(outcome, Refusal)
        assert "already False" in outcome.reason

    def test_an_unparseable_file_is_refused(self, tmp_path: Path) -> None:
        root = project(tmp_path, "DEBUG = True\ndef (:\n")

        outcome = fix_for(make_finding(line=1), root)

        assert isinstance(outcome, Refusal)

    def test_a_parenthesised_value_is_refused(self, tmp_path: Path) -> None:
        """The 0.21% from 6.4.1, arriving through a real caller."""
        root = project(tmp_path, "DEBUG = (True and\n         True)\n")

        outcome = fix_for(make_finding(line=1), root)

        assert isinstance(outcome, Refusal)
        assert "cannot be located exactly" in outcome.reason


class TestAssignmentAt:
    def test_it_finds_the_assignment(self, tmp_path: Path) -> None:
        node = assignment_at("A = 1\nDEBUG = True\n", 2, "DEBUG")

        assert node is not None

    def test_a_comment_naming_the_setting_is_not_an_assignment(self) -> None:
        """Why this is an AST lookup and not a text search."""
        assert assignment_at("# DEBUG = True is wrong\nX = 1\n", 1, "DEBUG") is None

    def test_a_dictionary_key_naming_the_setting_is_not_an_assignment(self) -> None:
        assert assignment_at('OPTIONS = {"DEBUG": True}\n', 1, "DEBUG") is None

    def test_a_string_naming_the_setting_is_not_an_assignment(self) -> None:
        assert assignment_at('NOTE = "DEBUG = True"\n', 1, "DEBUG") is None

    def test_an_attribute_assignment_is_not_a_name_assignment(self) -> None:
        assert assignment_at("settings.DEBUG = True\n", 1, "DEBUG") is None

    def test_a_multiple_target_assignment_still_matches(self) -> None:
        assert assignment_at("DEBUG = TEMPLATE_DEBUG = True\n", 1, "DEBUG") is not None


class TestTheProposedChange:
    def test_it_changes_the_value_and_nothing_else(self, tmp_path: Path) -> None:
        source = "# keep this off\nDEBUG = True  # noqa\n\nOTHER = 1\n"
        root = project(tmp_path, source)

        outcome = fix_for(make_finding(line=2), root)

        assert isinstance(outcome, Fix)
        assert outcome.after == source.replace("DEBUG = True", "DEBUG = False")

    def test_nothing_is_written_to_disk(self, tmp_path: Path) -> None:
        """`--dry-run` is the only mode this substep has."""
        source = "DEBUG = True\n"
        root = project(tmp_path, source)

        fix_for(make_finding(line=1), root)

        assert (root / "settings.py").read_text() == source

    def test_the_patch_is_a_unified_diff(self, tmp_path: Path) -> None:
        root = project(tmp_path, "DEBUG = True\n")

        outcome = fix_for(make_finding(line=1), root)

        assert isinstance(outcome, Fix)
        assert "--- a/settings.py" in outcome.patch
        assert "-DEBUG = True" in outcome.patch
        assert "+DEBUG = False" in outcome.patch

    def test_an_unconditional_fix_is_ready(self, tmp_path: Path) -> None:
        root = project(tmp_path, "DEBUG = True\n")

        outcome = fix_for(make_finding(line=1), root)

        assert isinstance(outcome, Fix)
        assert outcome.ready
        assert outcome.confirm is None

    def test_a_conditional_fix_carries_its_condition(self, tmp_path: Path) -> None:
        root = project(tmp_path, "SECURE_HSTS_INCLUDE_SUBDOMAINS = False\n")

        outcome = fix_for(make_finding(rule_id="DJS-008", line=1), root)

        assert isinstance(outcome, Fix)
        assert not outcome.ready
        assert outcome.confirm is not None
        assert "subdomain" in outcome.confirm

    def test_the_edit_carries_the_rule_that_asked_for_it(self, tmp_path: Path) -> None:
        root = project(tmp_path, "DEBUG = True\n")

        outcome = fix_for(make_finding(line=1), root)

        assert isinstance(outcome, Fix)
        assert [e.why for e in outcome.edits] == ["DJS-001"]


class TestItDoesNotRepublishASecretInContext:
    """A patch's context is unchanged source, printed verbatim.

    Fixing `DEBUG` three lines under `SECRET_KEY` publishes the key while
    changing something else -- the leak arrives through the context, not the
    change. The run already knows which lines those are, because a rule masked
    them, so the window is narrowed until they fall outside it.
    """

    def source_with_a_secret_near_the_fix(self) -> str:
        return 'SECRET_KEY = "a-real-looking-key"\n\nDEBUG = True\n\nOTHER = 1\n'

    def secret_finding(self) -> Finding:
        return make_finding(
            rule_id="DJS-002",
            line=1,
            snippet='SECRET_KEY = "a<redacted:18 chars>"',
        )

    def test_secret_lines_come_from_the_redaction_marker(self) -> None:
        held = secret_lines([self.secret_finding(), make_finding(line=3)])

        assert held == {"settings.py": {1}}

    def test_a_finding_with_no_marker_holds_no_line(self) -> None:
        assert secret_lines([make_finding(line=3)]) == {}

    def test_the_window_narrows_until_the_key_is_outside_it(self, tmp_path: Path) -> None:
        source = self.source_with_a_secret_near_the_fix()
        root = project(tmp_path, source)

        outcome = fix_for(make_finding(line=3), root, [self.secret_finding()])

        assert isinstance(outcome, Fix)
        assert outcome.context < DIFF_CONTEXT
        assert "a-real-looking-key" not in outcome.patch

    def test_the_change_itself_still_shows(self, tmp_path: Path) -> None:
        """Narrowing that hid the fix would be a different kind of useless."""
        root = project(tmp_path, self.source_with_a_secret_near_the_fix())

        outcome = fix_for(make_finding(line=3), root, [self.secret_finding()])

        assert isinstance(outcome, Fix)
        assert "-DEBUG = True" in outcome.patch
        assert "+DEBUG = False" in outcome.patch

    def test_without_a_secret_the_window_stays_wide(self, tmp_path: Path) -> None:
        """The contrast: always returning 0 would pass the tests above."""
        root = project(tmp_path, self.source_with_a_secret_near_the_fix())

        outcome = fix_for(make_finding(line=3), root, [])

        assert isinstance(outcome, Fix)
        assert outcome.context == DIFF_CONTEXT

    def test_an_adjacent_secret_forces_zero_context(self) -> None:
        before = 'SECRET_KEY = "k"\nDEBUG = True\n'
        after = 'SECRET_KEY = "k"\nDEBUG = False\n'

        assert safe_context(before, after, "settings.py", {1}) == 0

    def test_a_distant_secret_costs_nothing(self) -> None:
        before = "".join(f"L{i}\n" for i in range(40)) + "DEBUG = True\n"
        after = before.replace("DEBUG = True", "DEBUG = False")

        assert safe_context(before, after, "settings.py", {1}) == DIFF_CONTEXT

    def test_a_secret_below_the_change_is_found_too(self, tmp_path: Path) -> None:
        """Line numbering has to survive the removed line in between.

        Every other case here puts the key above the fix, where a counter that
        forgets to step over a `-` line still lands correctly. Below it, the
        same bug shifts every context line up by one and the key falls outside
        the window the check believes it is inspecting -- so the window is not
        narrowed and the key is published. Found by mutation.
        """
        source = 'DEBUG = True\nA = 1\nB = 2\nSECRET_KEY = "a-real-looking-key"\n'
        root = project(tmp_path, source)
        secret = make_finding(
            rule_id="DJS-002", line=4, snippet='SECRET_KEY = "a<redacted:18 chars>"'
        )

        outcome = fix_for(make_finding(line=1), root, [secret])

        assert isinstance(outcome, Fix)
        assert outcome.context < DIFF_CONTEXT
        assert "a-real-looking-key" not in outcome.patch

    def test_context_line_numbers_step_over_a_removal(self) -> None:
        """The same defect, stated directly against `safe_context`."""
        before = "DEBUG = True\nA = 1\nB = 2\nC = 3\nSECRET = 1\n"
        after = before.replace("DEBUG = True", "DEBUG = False")

        assert safe_context(before, after, "settings.py", {4}) == 2
        assert safe_context(before, after, "settings.py", {5}) == DIFF_CONTEXT

    def test_the_real_fixture_never_prints_its_key(self, tmp_path: Path) -> None:
        """End to end, against the key actually planted in the fixture."""
        import shutil

        root = tmp_path / "vulnerable_project"
        shutil.copytree(FIXTURES / "vulnerable_project", root)
        planted = (root / "config" / "settings" / "base.py").read_text()
        key = planted.split('SECRET_KEY = "', 1)[1].split('"', 1)[0]
        assert len(key) > 20

        proposed, _ = fixes(engine.run(root, min_confidence=Confidence.TENTATIVE), root)

        assert proposed
        assert key not in patch(proposed)
        assert all(key not in f.patch for f in proposed)

    def test_combining_takes_the_narrowest_window(self, tmp_path: Path) -> None:
        """Two fixes in one file must not widen what one of them shrank."""
        source = (
            'SECRET_KEY = "a-real-looking-key"\n\nDEBUG = True\nSESSION_COOKIE_HTTPONLY = False\n'
        )
        root = project(tmp_path, source)
        secret = self.secret_finding()
        proposed = [
            f
            for f in (
                fix_for(make_finding(line=3), root, [secret]),
                fix_for(make_finding(rule_id="DJS-011", line=4), root, [secret]),
            )
            if isinstance(f, Fix)
        ]
        assert len(proposed) == 2

        assert "a-real-looking-key" not in patch(proposed)


class TestCombining:
    def test_two_fixes_in_one_file_compose(self, tmp_path: Path) -> None:
        """Two diffs against the same original would not."""
        source = "DEBUG = True\nSESSION_COOKIE_HTTPONLY = False\n"
        root = project(tmp_path, source)
        proposed = [
            fix_for(make_finding(line=1), root),
            fix_for(make_finding(rule_id="DJS-011", line=2), root),
        ]
        assert all(isinstance(f, Fix) for f in proposed)

        result = combined([f for f in proposed if isinstance(f, Fix)])

        assert result[root / "settings.py"] == "DEBUG = False\nSESSION_COOKIE_HTTPONLY = True\n"

    def test_the_combined_patch_has_one_header_per_file(self, tmp_path: Path) -> None:
        source = "DEBUG = True\nSESSION_COOKIE_HTTPONLY = False\n"
        root = project(tmp_path, source)
        proposed = [
            f
            for f in (
                fix_for(make_finding(line=1), root),
                fix_for(make_finding(rule_id="DJS-011", line=2), root),
            )
            if isinstance(f, Fix)
        ]

        text = patch(proposed)

        assert text.count("--- a/settings.py") == 1
        assert "-DEBUG = True" in text
        assert "-SESSION_COOKIE_HTTPONLY = False" in text

    def test_no_fixes_is_an_empty_patch(self) -> None:
        assert patch([]) == ""


class TestAgainstTheEngine:
    """The property that matters: the fix clears the finding.

    Everything above checks the diff is well-formed. This checks it is
    *correct*, by running the real engine over the fixture, applying what it
    proposes, and running the engine again -- which is the shape 6.4.3 builds
    on.
    """

    def run_on(self, root: Path) -> list[Finding]:
        return engine.run(root, min_confidence=Confidence.TENTATIVE).findings

    def test_applying_a_fix_removes_the_finding_it_came_from(self, tmp_path: Path) -> None:
        import shutil

        root = tmp_path / "vulnerable_project"
        shutil.copytree(FIXTURES / "vulnerable_project", root)
        before = self.run_on(root)
        proposed, _ = fixes(engine.run(root, min_confidence=Confidence.TENTATIVE), root)
        assert proposed, "the fixture should offer at least one fixable finding"

        for path, after in combined(proposed).items():
            path.write_text(after, encoding="utf-8")
        remaining = {f.fingerprint for f in self.run_on(root)}

        cleared = {f.finding.fingerprint for f in proposed}
        assert cleared & remaining == set()
        assert len(self.run_on(root)) < len(before)

    def test_a_fix_introduces_no_new_finding(self, tmp_path: Path) -> None:
        import shutil

        root = tmp_path / "vulnerable_project"
        shutil.copytree(FIXTURES / "vulnerable_project", root)
        before = {f.fingerprint for f in self.run_on(root)}
        proposed, _ = fixes(engine.run(root, min_confidence=Confidence.TENTATIVE), root)
        for path, after in combined(proposed).items():
            path.write_text(after, encoding="utf-8")

        after_run = {f.fingerprint for f in self.run_on(root)}

        assert after_run - before == set()

    def test_the_fixture_still_parses_after_every_fix(self, tmp_path: Path) -> None:
        import ast
        import shutil

        root = tmp_path / "vulnerable_project"
        shutil.copytree(FIXTURES / "vulnerable_project", root)
        proposed, _ = fixes(engine.run(root, min_confidence=Confidence.TENTATIVE), root)

        for after in combined(proposed).values():
            ast.parse(after)

    def test_refusals_outnumber_fixes_and_that_is_the_point(self, tmp_path: Path) -> None:
        import shutil

        root = tmp_path / "vulnerable_project"
        shutil.copytree(FIXTURES / "vulnerable_project", root)

        proposed, refused = fixes(engine.run(root, min_confidence=Confidence.TENTATIVE), root)

        assert len(refused) > len(proposed)
        assert all(r.reason for r in refused)


@pytest.mark.parametrize("rule_id", sorted(FIXERS))
def test_each_fixer_produces_a_diff_on_its_own_shape(rule_id: str, tmp_path: Path) -> None:
    fixer = FIXERS[rule_id]
    wrong = "False" if fixer.value == "True" else "True"
    root = project(tmp_path, f"{fixer.setting} = {wrong}\n")

    outcome = fix_for(make_finding(rule_id=rule_id, line=1), root)

    assert isinstance(outcome, Fix)
    assert outcome.after == f"{fixer.setting} = {fixer.value}\n"
