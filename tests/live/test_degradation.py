"""What the audit could not check, and whether it says so.

The property under test is negative: a run with the live tier absent emits
fewer findings, and fewer findings are indistinguishable from a cleaner
codebase unless something says otherwise. Every test here is really asking the
same question -- can a reader tell "checked and clean" from "not checked".
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from djaudit import engine
from djaudit.context import ProjectContext
from djaudit.degradation import Degradation, Skipped, assess
from djaudit.models import Confidence, Family, Finding, Severity, Tier
from djaudit.registry import Rule, RuleMeta, all_rules, select

LIVE_SENTENCE = (
    "Nothing static replaces reading the SQL Django actually emits, so a "
    "rewrite this rule would prove can only be suspected without it."
)


def _meta(rule_id: str, *, tier: Tier = Tier.LIVE, **kwargs: object) -> RuleMeta:
    defaults: dict[str, object] = {
        "id": rule_id,
        "title": "a rule",
        "family": Family.DJM,
        "severity": Severity.HIGH,
        "confidence": Confidence.CERTAIN,
        "tier": tier,
        "rationale": "because",
        "remediation": "fix it",
    }
    if tier is Tier.LIVE:
        defaults["fallback"] = LIVE_SENTENCE
    return RuleMeta(**{**defaults, **kwargs})  # type: ignore[arg-type]


@pytest.fixture
def live_rule(monkeypatch: pytest.MonkeyPatch) -> type[Rule]:
    """A registered live rule. There are none in the catalogue yet, and a gate
    that has nothing to check is not a gate."""

    class _Live(Rule):
        meta = _meta("DJM-900", fallback_rules=("DJM-001", "DJM-002"))

        def check(self, ctx: ProjectContext) -> Iterator[Finding]:
            return iter(())

    from djaudit import registry

    monkeypatch.setattr(registry, "all_rules", lambda: [*all_rules(), _Live])
    import djaudit.degradation as deg

    monkeypatch.setattr(deg, "all_rules", registry.all_rules)
    return _Live


class TestEveryLiveRuleDeclaresAFallback:
    """Enforced now, before the first live rule exists, so `DJM-010` cannot land
    without one. The catalogue is all-static today, so these run against a
    synthesised rule as well as the real ones."""

    @staticmethod
    def gate(catalogue: list[type[Rule]]) -> None:
        for rule in catalogue:
            if rule.meta.tier is Tier.LIVE:
                assert rule.meta.fallback, f"{rule.meta.id} is live with no fallback declared"

    def test_the_shipped_catalogue_complies(self) -> None:
        self.gate(all_rules())

    def test_the_gate_rejects_a_live_rule_without_one(self) -> None:
        """The control. The catalogue is all-static today, so the test above
        passes over an empty set and on its own is not evidence of anything."""

        class _Offender(Rule):
            meta = _meta("DJM-901", fallback="")

            def check(self, ctx: ProjectContext) -> Iterator[Finding]:
                return iter(())

        with pytest.raises(AssertionError, match="DJM-901 is live with no fallback"):
            self.gate([_Offender])

    def test_the_gate_passes_a_live_rule_with_one(self, live_rule: type[Rule]) -> None:
        self.gate([live_rule])

    def test_the_declaration_is_a_written_sentence(self, live_rule: type[Rule]) -> None:
        # A placeholder like "none" satisfies a truthiness check and tells the
        # reader nothing about what they lost.
        text = live_rule.meta.fallback
        assert len(text) >= 60
        assert text.endswith(".")
        assert text[0].isupper()

    def test_named_static_rules_have_to_exist(self, live_rule: type[Rule]) -> None:
        """A typo'd fallback id would promise cover that does not exist."""
        known = {rule.meta.id for rule in all_rules()}
        for rule_id in live_rule.meta.fallback_rules:
            assert rule_id in known, f"{rule_id} is named as a fallback but is not a rule"

    def test_a_static_rule_needs_no_fallback(self) -> None:
        assert _meta("DJS-900", tier=Tier.STATIC).fallback == ""


class TestNamingWhatDidNotRun:
    def test_a_live_rule_that_did_not_run_is_reported(self, live_rule: type[Rule]) -> None:
        result = assess("off", ran=set())
        assert [item.rule_id for item in result.skipped] == ["DJM-900"]

    def test_a_live_rule_that_ran_is_not(self, live_rule: type[Rule]) -> None:
        assert assess("on", ran={"DJM-900"}).skipped == ()

    def test_static_rules_are_never_reported_as_skipped(self, live_rule: type[Rule]) -> None:
        """Excluding `DJS-001` is a choice the reader made; skipping a live rule
        is a capability they may not know they lack."""
        result = assess("off", ran=set())
        assert all(item.rule_id == "DJM-900" for item in result.skipped)

    def test_it_carries_the_fallback_sentence(self, live_rule: type[Rule]) -> None:
        assert assess("off", ran=set()).skipped[0].fallback == LIVE_SENTENCE

    def test_the_reason_is_carried_through(self, live_rule: type[Rule]) -> None:
        assert assess("no virtualenv", ran=set()).reason == "no virtualenv"


class TestAFallbackThatDidNotRunCoversNothing:
    """The failure this module exists to prevent, in miniature: telling a reader
    they are covered by a rule that also did not run."""

    def test_it_names_the_fallbacks_that_ran(self, live_rule: type[Rule]) -> None:
        result = assess("off", ran={"DJM-001", "DJM-002"})
        assert result.skipped[0].covered_by == ("DJM-001", "DJM-002")

    def test_an_excluded_fallback_is_not_claimed(self, live_rule: type[Rule]) -> None:
        result = assess("off", ran={"DJM-001"})
        assert result.skipped[0].covered_by == ("DJM-001",)

    def test_no_fallback_ran_at_all(self, live_rule: type[Rule]) -> None:
        assert assess("off", ran=set()).skipped[0].covered_by == ()


class TestReadingTheReport:
    def test_nothing_skipped_is_falsy(self) -> None:
        assert not Degradation("on")

    def test_something_skipped_is_truthy(self) -> None:
        assert Degradation("off", (Skipped("DJM-900", "t", "f"),))

    def test_the_heading_counts_them(self) -> None:
        two = Degradation("off", (Skipped("A", "t", "f"), Skipped("B", "t", "f")))
        assert two.explain() == "2 live-tier rules did not run (off)"

    def test_one_rule_is_singular(self) -> None:
        one = Degradation("off", (Skipped("A", "t", "f"),))
        assert one.explain() == "1 live-tier rule did not run (off)"

    def test_a_clean_run_says_so_rather_than_nothing(self) -> None:
        assert Degradation("live ran").explain() == "no live-tier rules were skipped (live ran)"

    def test_a_line_per_rule_under_the_heading(self) -> None:
        report = Degradation("off", (Skipped("DJM-900", "locks", "Nothing static replaces it."),))
        assert report.report() == (
            "1 live-tier rule did not run (off)",
            "DJM-900 locks: Nothing static replaces it.",
        )

    def test_the_cover_is_named_in_the_line(self) -> None:
        item = Skipped("DJM-900", "locks", "Nothing static replaces it.", ("DJM-001", "DJM-002"))
        assert item.explain() == (
            "DJM-900 locks: Nothing static replaces it. Still checked by DJM-001, DJM-002."
        )

    def test_no_cover_leaves_no_dangling_clause(self) -> None:
        assert Skipped("DJM-900", "locks", "Nothing static replaces it.").explain() == (
            "DJM-900 locks: Nothing static replaces it."
        )


class TestNamingRuleIsNotConsent:
    """`--rule DJM-010` must not be a way to make djaudit execute the target's
    code on a run that never asked for the live tier. `include` deliberately
    overrides every other filter, and a tier is not a filter -- it is a
    capability the run either has or does not."""

    def test_naming_a_live_rule_does_not_run_it(self, live_rule: type[Rule]) -> None:
        chosen = select(tiers={Tier.STATIC}, include={"DJM-900"})
        assert [rule.meta.id for rule in chosen] == []

    def test_the_control_it_runs_when_live_is_requested(self, live_rule: type[Rule]) -> None:
        chosen = select(tiers={Tier.STATIC, Tier.LIVE}, include={"DJM-900"})
        assert [rule.meta.id for rule in chosen] == ["DJM-900"]

    def test_include_still_beats_a_family_filter(self, live_rule: type[Rule]) -> None:
        """The override that was wanted is intact; only the tier is exempt."""
        chosen = select(families={Family.DJS}, include={"DJM-001"})
        assert [rule.meta.id for rule in chosen] == ["DJM-001"]

    def test_include_still_beats_an_exclude(self, live_rule: type[Rule]) -> None:
        chosen = select(include={"DJM-001"}, exclude={"DJM-001"})
        assert [rule.meta.id for rule in chosen] == ["DJM-001"]


class TestTheEngineReportsIt:
    def test_a_static_run_says_the_live_tier_was_not_requested(self, tmp_path: Path) -> None:
        result = engine.run(tmp_path)
        assert result.degraded is not None
        assert result.degraded.reason == "the live tier was not requested"

    def test_a_skipped_live_rule_reaches_the_result(
        self, tmp_path: Path, live_rule: type[Rule]
    ) -> None:
        result = engine.run(tmp_path)
        assert result.degraded is not None
        assert [item.rule_id for item in result.degraded.skipped] == ["DJM-900"]

    def test_a_run_with_no_live_rules_is_not_degraded(self, tmp_path: Path) -> None:
        result = engine.run(tmp_path)
        assert not result.degraded

    def test_consent_without_an_environment_is_distinguished(self, tmp_path: Path) -> None:
        """Three causes, three different actions: consent, install, nothing."""
        from djaudit.discovery import build_context

        ctx = build_context(tmp_path)
        result = engine.run(tmp_path, tiers={Tier.STATIC, Tier.LIVE}, context=ctx)
        assert result.degraded is not None
        assert result.degraded.reason == (
            "the live tier was requested but the target's environment is unavailable"
        )

    def test_an_available_live_tier_is_named_as_such(self, tmp_path: Path) -> None:
        from dataclasses import replace

        from djaudit.discovery import build_context

        ctx = replace(build_context(tmp_path), live=True)
        result = engine.run(tmp_path, tiers={Tier.STATIC, Tier.LIVE}, context=ctx)
        assert result.degraded is not None
        assert result.degraded.reason == "the live tier ran"


class TestTheReaderActuallySeesIt:
    """A degradation recorded in a field nobody prints is the same silence this
    module exists to prevent."""

    @staticmethod
    def rendered(tmp_path: Path) -> str:
        from io import StringIO

        from rich.console import Console

        from djaudit.reporters import terminal

        buffer = StringIO()
        terminal.report(engine.run(tmp_path), Console(file=buffer, width=100, no_color=True))
        return buffer.getvalue()

    def test_the_skipped_rule_is_printed(self, tmp_path: Path, live_rule: type[Rule]) -> None:
        assert "DJM-900" in self.rendered(tmp_path)

    def test_the_reason_is_printed(self, tmp_path: Path, live_rule: type[Rule]) -> None:
        assert "the live tier was not requested" in self.rendered(tmp_path)

    def test_the_fallback_sentence_is_printed(self, tmp_path: Path, live_rule: type[Rule]) -> None:
        assert "can only be suspected without it." in self.rendered(tmp_path)

    def test_a_clean_static_run_is_not_nagged(self, tmp_path: Path) -> None:
        """The control: with no live rules registered there is nothing missing,
        and a warning printed on every run is a warning nobody reads."""
        assert "not checked" not in self.rendered(tmp_path)
