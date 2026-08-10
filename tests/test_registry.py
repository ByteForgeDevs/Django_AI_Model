"""Registry invariants keep the rule catalogue navigable as it grows."""

from collections.abc import Iterator

import pytest

from djaudit.context import ProjectContext
from djaudit.models import Confidence, Family, Finding, Severity, Tier
from djaudit.registry import Rule, RuleError, RuleMeta, all_rules, get, register, select


def meta(rule_id: str = "DJS-900", family: Family = Family.DJS) -> RuleMeta:
    return RuleMeta(
        id=rule_id,
        title="t",
        family=family,
        severity=Severity.LOW,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        rationale="r",
        remediation="fix",
    )


def make_rule(rule_meta: RuleMeta) -> type[Rule]:
    class Anonymous(Rule):
        def check(self, ctx: ProjectContext) -> Iterator[Finding]:
            yield from ()

    Anonymous.meta = rule_meta
    return Anonymous


class TestRegistration:
    def test_malformed_ids_are_rejected(self):
        for bad in ("DJS1", "djs-001", "XXX-001", "DJS-1"):
            with pytest.raises(RuleError, match="valid rule id"):
                register(make_rule(meta(bad)))

    def test_family_must_match_the_id_prefix(self):
        with pytest.raises(RuleError, match="family"):
            register(make_rule(meta("DJP-001", family=Family.DJS)))

    def test_missing_meta_is_rejected(self):
        class NoMeta(Rule):
            def check(self, ctx):
                yield from ()

        with pytest.raises(RuleError, match="RuleMeta"):
            register(NoMeta)

    def test_duplicate_ids_are_rejected(self):
        all_rules()  # force the built-in catalogue to load first
        with pytest.raises(RuleError, match="duplicate"):
            register(make_rule(meta("DJS-001")))


class TestLookup:
    def test_builtin_rules_are_loaded_lazily(self):
        assert any(r.meta.id == "DJS-001" for r in all_rules())

    def test_catalogue_is_ordered_by_id(self):
        ids = [r.meta.id for r in all_rules()]
        assert ids == sorted(ids)

    def test_unknown_id_raises(self):
        with pytest.raises(RuleError, match="unknown rule id"):
            get("DJS-999")

    def test_select_by_family(self):
        """`select(families={Family.DJX}) == []` held here until `DJX-001`
        shipped, and it was the same mistake `test_select_by_tier` records: a
        fact about the catalogue wearing the clothes of a rule about the
        filter. The filter's actual claim is that it partitions."""
        assert select(families={Family.DJS})
        assert select(families={Family.DJM})
        every = select()
        for family in Family:
            chosen = select(families={family})
            assert {r.meta.family for r in chosen} <= {family}
            assert chosen == [r for r in every if r.meta.family is family]
        assert sum(len(select(families={f})) for f in Family) == len(every)

    def test_select_by_tier(self):
        """`select(tiers={Tier.LIVE}) == []` held here until `DJM-010` shipped,
        and it was a fact about the catalogue wearing the clothes of a rule
        about the filter. The filter's actual claim is that it partitions."""
        static = select(tiers={Tier.STATIC})
        live = select(tiers={Tier.LIVE})
        assert static and live
        assert all(r.meta.tier is Tier.STATIC for r in static)
        assert all(r.meta.tier is Tier.LIVE for r in live)
        assert len(static) + len(live) == len(select())

    def test_include_overrides_other_filters(self):
        chosen = select(families={Family.DJM}, include={"DJS-001"})
        assert [r.meta.id for r in chosen] == ["DJS-001"]

    def test_exclude_removes_rules(self):
        assert "DJS-001" not in {r.meta.id for r in select(exclude={"DJS-001"})}


class TestFindingHelper:
    def test_finding_inherits_rule_metadata(self):
        from djaudit.models import Location

        rule = make_rule(meta())()
        finding = rule.finding(location=Location("a.py", 1), message="m")

        assert finding.rule_id == "DJS-900"
        assert finding.severity is Severity.LOW
        assert finding.rationale == "r"
        assert finding.remediation == "fix"

    def test_per_finding_overrides_win(self):
        from djaudit.models import Location

        rule = make_rule(meta())()
        finding = rule.finding(
            location=Location("a.py", 1),
            message="m",
            severity=Severity.CRITICAL,
            confidence=Confidence.CERTAIN,
        )

        assert finding.severity is Severity.CRITICAL
        assert finding.confidence is Confidence.CERTAIN
