"""The plan checker, checked.

``scripts/check_plan.py`` is the gate that decides whether the project plan is
still true, and until now it had no tests at all. That is worth noticing on its
own: the one script whose job is to catch drift was itself unverified, and it
duly missed the largest piece of drift in the document -- a whole phase marked
*Not started* while its fifteen rules were registered, running and shipping
findings on three real repositories.

The old checker could not have caught it. It compared the plan against the
plan and nothing else, so it agreed with any self-consistent lie. These tests
are mostly about the checks that reach outside the document: a phase's status
against the registry, a phase's status against its own write-ups, and the
registry against the plan.

Each test builds the smallest plan that exhibits one problem, rather than
mutating the real one, so a failure names the rule that broke rather than the
document that happens to be open.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_plan import done_substeps, introducing_phase, review  # noqa: E402

CHECKER = ROOT / "scripts" / "check_plan.py"


def plan(
    *,
    phase0_status: str = "**Complete**",
    phase1_status: str = "Not started",
    phase1_body: str = "",
    djs: int = 1,
    implemented_sentence: str = "**1 rules are implemented**",
) -> str:
    """A two-phase plan in the real document's shape.

    Phase 0 introduces ``DJS-001`` and is written up as done; Phase 1
    introduces ``DJA-001`` and is not. Callers move one thing at a time.
    """
    return f"""# Plan

## 6. Rule ID taxonomy

# Phase 0 — Settings

### Step 0.1 — Settings

- **0.1.1** — Implement `DJS-001`.

  **Done.** It works.

# Phase 1 — API

### Step 1.1 — API

- **1.1.1** — Implement `DJA-001`.
{phase1_body}
## 7. Risk register

Nothing yet.

## 8. Progress tracking

| Phase | Title | Steps | Substeps | Status |
|---|---|---|---|---|
| 0 | Settings | 1 | 1 | {phase0_status} |
| 1 | API | 1 | 1 | {phase1_status} |
| | **Total** | **2** | **2** | |

Rule count on completion: **2 rules** — `DJS` {djs}, `DJA` 1.
Of those, {implemented_sentence} today.
"""


class TestTheRealPlan:
    def test_it_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CHECKER)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_it_reports_planned_and_implemented_separately(self) -> None:
        # A bare rule total reads as a shipped total. Most of this plan is
        # still specification and the summary line has to say so.
        result = subprocess.run(
            [sys.executable, str(CHECKER)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "planned" in result.stdout
        assert "implemented" in result.stdout


class TestAgainstTheRegistry:
    def test_a_clean_plan_passes(self) -> None:
        report = review(plan(), {"DJS-001"})
        assert report.ok, report.problems
        assert report.planned_rules == 2
        assert report.implemented_rules == 1

    def test_a_not_started_phase_whose_rules_exist_fails(self) -> None:
        # Phase 2's actual failure: the code shipped, the table did not move.
        report = review(
            plan(implemented_sentence="**2 rules are implemented**"),
            {"DJS-001", "DJA-001"},
        )
        assert not report.ok
        assert any("DJA-001" in p and "Not started" in p for p in report.problems)

    def test_a_complete_phase_missing_a_rule_fails(self) -> None:
        report = review(
            plan(phase0_status="**Complete**", implemented_sentence="**0 rules are implemented**"),
            set(),
        )
        assert not report.ok
        assert any("marked complete" in p and "DJS-001" in p for p in report.problems)

    def test_a_registered_rule_the_plan_never_mentions_fails(self) -> None:
        report = review(
            plan(implemented_sentence="**2 rules are implemented**"),
            {"DJS-001", "DJX-009"},
        )
        assert not report.ok
        assert any("described nowhere in the plan" in p for p in report.problems)

    def test_a_wrong_implemented_count_fails(self) -> None:
        report = review(plan(implemented_sentence="**7 rules are implemented**"), {"DJS-001"})
        assert not report.ok
        assert any("claims 7 rules are implemented" in p for p in report.problems)

    def test_a_missing_implemented_count_fails(self) -> None:
        # Silence is the failure mode being guarded against, so an absent
        # sentence has to fail rather than pass by default.
        report = review(plan(implemented_sentence="some are done"), {"DJS-001"})
        assert not report.ok
        assert any("never states how many rules are implemented" in p for p in report.problems)

    def test_the_implemented_count_survives_a_line_break(self) -> None:
        text = plan(implemented_sentence="**1\nrules are implemented**")
        assert review(text, {"DJS-001"}).ok


class TestAgainstTheWriteUps:
    def test_a_not_started_phase_with_a_done_substep_fails(self) -> None:
        report = review(plan(phase1_body="\n  **Done.** Shipped it.\n"), {"DJS-001"})
        assert not report.ok
        assert any("written up as done" in p for p in report.problems)

    def test_every_shape_of_done_marker_counts(self) -> None:
        # The plan writes qualified closures -- "**Done, narrowed
        # deliberately.**" -- and a checker that only knew "**Done.**" would
        # read those substeps as untouched.
        for marker in ("**Done.**", "*Done.*", "**Done, narrowed deliberately.**"):
            counts = done_substeps(plan(phase1_body=f"\n  {marker} Shipped.\n"))
            assert counts["1"] == 1, marker

    def test_done_when_is_not_a_done_marker(self) -> None:
        # "Done when:" states an exit condition; it is the opposite of a claim
        # that the work happened.
        counts = done_substeps(plan(phase1_body="\n  **Done when:** the tests pass.\n"))
        assert counts["1"] == 0


class TestAttribution:
    def test_a_rule_belongs_to_the_phase_that_first_names_it(self) -> None:
        intro = introducing_phase(plan())
        assert intro == {"DJS-001": 0, "DJA-001": 1}

    def test_a_later_cross_reference_does_not_move_a_rule(self) -> None:
        # Phase 1 citing DJS-001 must not take ownership of it, or marking
        # Phase 1 not-started would flag a Phase 0 rule.
        intro = introducing_phase(plan(phase1_body="\n  Builds on `DJS-001`.\n"))
        assert intro["DJS-001"] == 0

    def test_the_progress_table_does_not_re_attribute_rules(self) -> None:
        # The table sits below the last phase heading, so a rule named there
        # would otherwise be attributed to the final phase by position alone --
        # and the progress table names each completed phase's rules. Verified
        # with an id that appears nowhere else, since a rule already introduced
        # earlier would be unaffected either way and prove nothing.
        text = plan().replace("| 1 | API | 1 | 1 |", "| 1 | API `DJX-001` | 1 | 1 |")
        assert "DJX-001" not in introducing_phase(text)


class TestStatusVocabulary:
    def test_in_progress_is_accepted(self) -> None:
        assert review(plan(phase1_status="In progress"), {"DJS-001"}).ok

    def test_an_unrecognised_status_fails(self) -> None:
        report = review(plan(phase1_status="Mostly there"), {"DJS-001"})
        assert not report.ok
        assert any("unrecognised status" in p for p in report.problems)


class TestInternalConsistency:
    def test_a_miscounted_substep_still_fails(self) -> None:
        text = plan().replace("| 0 | Settings | 1 | 1 |", "| 0 | Settings | 1 | 4 |")
        report = review(text, {"DJS-001"})
        assert not report.ok
        assert any("claims 4 substeps" in p for p in report.problems)

    def test_a_family_breakdown_that_disagrees_fails(self) -> None:
        report = review(plan(djs=3), {"DJS-001"})
        assert not report.ok
        assert any("breakdown claims 3 rules" in p for p in report.problems)

    def test_a_missing_progress_table_fails(self) -> None:
        report = review("# Plan\n\nNo table here.\n", set())
        assert not report.ok
        assert any("could not find the progress table" in p for p in report.problems)
