"""Merging Django's deployment check with our own settings findings.

The claim under test is not "duplicates are removed". It is that each half
keeps what only it knows: our rule keeps the `file:line` Django cannot produce,
and gains the runtime verdict our static tier cannot reach.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from djaudit.context import ProjectContext
from djaudit.live.checks import Report, parse_report
from djaudit.live.checks import Unknown as ChecksUnknown
from djaudit.live.corroborate import CORROBORATES, corroborate
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

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")


def report_of(*lines: str) -> Report | ChecksUnknown:
    body = "\n".join(lines)
    return parse_report(
        f"System check identified some issues:\n\nWARNINGS:\n{body}\n"
        f"\nSystem check identified {len(lines)} issues (0 silenced).\n"
    )


def finding(rule_id: str, *, confidence: Confidence = Confidence.TENTATIVE) -> Finding:
    return Finding(
        rule_id=rule_id,
        title="a title",
        family=Family.DJS,
        severity=Severity.HIGH,
        confidence=confidence,
        tier=Tier.STATIC,
        location=Location(file="proj/settings.py", line=12),
        message="Something is set wrong.",
        evidence=(Evidence(kind=EvidenceKind.AST, content="DEBUG = True", source="settings.py"),),
    )


DEBUG_CHECK = "?: (security.W018) You should not have DEBUG set to True in deployment."


class TestWhenBothTiersAgree:
    def test_the_finding_becomes_certain(self) -> None:
        """Our rule read the source and could not settle it. Django resolved
        the value for real. That is new information, so confidence moves."""
        result = corroborate([finding("DJS-001")], report_of(DEBUG_CHECK))
        (merged,) = result.findings
        assert merged.confidence is Confidence.CERTAIN

    def test_the_control_is_that_it_started_tentative(self) -> None:
        assert finding("DJS-001").confidence is Confidence.TENTATIVE

    def test_djangos_sentence_is_quoted_as_evidence(self) -> None:
        (merged,) = corroborate([finding("DJS-001")], report_of(DEBUG_CHECK)).findings
        quoted = [e for e in merged.evidence if e.source == "manage.py check --deploy"]
        assert len(quoted) == 1
        assert quoted[0].content == (
            "security.W018: You should not have DEBUG set to True in deployment."
        )

    def test_our_own_evidence_is_not_replaced(self) -> None:
        """The AST excerpt is the half Django cannot produce."""
        (merged,) = corroborate([finding("DJS-001")], report_of(DEBUG_CHECK)).findings
        assert merged.evidence[0].kind is EvidenceKind.AST
        assert len(merged.evidence) == 2

    def test_the_location_survives(self) -> None:
        """Django reports `?` for an object. Ours is the only half with a line
        number, so a merge that preferred Django's would lose the fix."""
        (merged,) = corroborate([finding("DJS-001")], report_of(DEBUG_CHECK)).findings
        assert merged.location.file == "proj/settings.py"
        assert merged.location.line == 12

    def test_the_message_says_django_agrees(self) -> None:
        (merged,) = corroborate([finding("DJS-001")], report_of(DEBUG_CHECK)).findings
        assert merged.message == (
            "Something is set wrong. Django's own deployment check agrees, reporting security.W018."
        )

    def test_it_is_still_one_finding(self) -> None:
        assert len(corroborate([finding("DJS-001")], report_of(DEBUG_CHECK)).findings) == 1

    def test_the_check_is_counted_as_confirmed(self) -> None:
        result = corroborate([finding("DJS-001")], report_of(DEBUG_CHECK))
        assert result.confirmed == {"security.W018"}
        assert result.merged == 1


class TestWhenDjangoSaysNothing:
    """Silence is not disagreement. Django's checks are narrower than ours, and
    a silenced check leaves no trace at all."""

    def test_an_unmatched_finding_is_untouched(self) -> None:
        (merged,) = corroborate([finding("DJS-015")], report_of(DEBUG_CHECK)).findings
        assert merged.confidence is Confidence.TENTATIVE
        assert len(merged.evidence) == 1
        assert merged.message == "Something is set wrong."

    def test_an_unreadable_report_changes_nothing(self) -> None:
        original = finding("DJS-001")
        result = corroborate([original], ChecksUnknown("no interpreter"))
        assert result.findings == (original,)
        assert result.confirmed == frozenset()

    def test_and_claims_no_gap_either(self) -> None:
        """A check that never ran found no gap in our coverage. Reporting one
        would make `DJS-028` fire on an unreachable project."""
        assert corroborate([], ChecksUnknown("no")).unclaimed == frozenset()

    def test_an_empty_report_confirms_nothing(self) -> None:
        result = corroborate(
            [finding("DJS-001")], parse_report("System check identified no issues (0 silenced).\n")
        )
        assert result.confirmed == frozenset()
        assert result.findings[0].confidence is Confidence.TENTATIVE


class TestOneDefectReportedThreeWays:
    """Whether an insecure session cookie is `W010`, `W011` or `W012` depends
    on how sessions were enabled. All three are one line to fix."""

    THREE = (
        "?: (security.W010) You have 'django.contrib.sessions' in your INSTALLED_APPS.",
        "?: (security.W011) You have SessionMiddleware in your MIDDLEWARE.",
        "?: (security.W012) SESSION_COOKIE_SECURE is not set to True.",
    )

    def test_they_all_land_on_one_rule(self) -> None:
        assert {CORROBORATES[c] for c in ("security.W010", "security.W011", "security.W012")} == {
            "DJS-009"
        }

    def test_and_produce_one_finding(self) -> None:
        result = corroborate([finding("DJS-009")], report_of(*self.THREE))
        assert len(result.findings) == 1

    def test_carrying_every_id(self) -> None:
        result = corroborate([finding("DJS-009")], report_of(*self.THREE))
        assert result.confirmed == {"security.W010", "security.W011", "security.W012"}

    def test_and_quoting_every_message(self) -> None:
        (merged,) = corroborate([finding("DJS-009")], report_of(*self.THREE)).findings
        (quoted,) = [e for e in merged.evidence if e.source == "manage.py check --deploy"]
        assert quoted.content.count("\n") == 2
        assert "security.W010" in quoted.content
        assert "security.W012" in quoted.content

    def test_the_message_lists_them_readably(self) -> None:
        (merged,) = corroborate([finding("DJS-009")], report_of(*self.THREE)).findings
        assert "security.W010, security.W011 and security.W012." in merged.message

    def test_two_are_joined_with_and_alone(self) -> None:
        (merged,) = corroborate([finding("DJS-009")], report_of(*self.THREE[:2])).findings
        assert "reporting security.W010 and security.W011." in merged.message

    def test_and_one_is_named_alone(self) -> None:
        (merged,) = corroborate([finding("DJS-009")], report_of(*self.THREE[:1])).findings
        assert "reporting security.W010." in merged.message


class TestWhatWeDoNotCover:
    def test_a_check_with_no_rule_is_a_gap(self) -> None:
        """`security.W022` is the referrer policy, which no `DJS` rule reads.
        `DJS-028` reports exactly this set."""
        result = corroborate([], report_of("?: (security.W022) You have not set it."))
        assert result.unclaimed == {"security.W022"}

    def test_a_check_we_do_cover_is_not_a_gap(self) -> None:
        assert corroborate([], report_of(DEBUG_CHECK)).unclaimed == frozenset()

    def test_a_gap_is_reported_even_with_no_finding_to_merge(self) -> None:
        """The whole point: we found nothing, Django found something."""
        result = corroborate([], report_of("?: (security.W021) No preload."))
        assert result.findings == ()
        assert result.unclaimed == {"security.W021"}

    def test_a_non_security_check_is_not_our_gap(self) -> None:
        """`models.E015` is a broken model, not a settings check we missed.
        Claiming it as a recall gap would make `DJS-028` a bug tracker for
        Django's entire check framework."""
        result = corroborate([], report_of("blog.Post: (models.E015) bad ordering."))
        assert result.unclaimed == frozenset()

    def test_a_check_with_no_id_is_not_a_gap(self) -> None:
        result = corroborate([finding("DJS-001")], report_of("?: something with no id"))
        assert result.unclaimed == frozenset()
        assert result.confirmed == frozenset()


class TestTheMappingIsRealDjango:
    """Every key is a check id Django actually emits, and every value a rule we
    actually ship. A mapping to a rule id nobody registered would silently
    never fire, and mutation testing cannot see that."""

    def test_every_key_is_a_security_check(self) -> None:
        assert all(c.startswith("security.W") for c in CORROBORATES)

    def test_every_value_is_a_registered_rule(self) -> None:
        from djaudit import registry

        known = {r.meta.id for r in registry.all_rules()}
        assert set(CORROBORATES.values()) <= known, set(CORROBORATES.values()) - known

    def test_every_value_is_a_settings_rule(self) -> None:
        assert all(rule.startswith("DJS-") for rule in CORROBORATES.values())

    def test_the_mapping_is_not_empty(self) -> None:
        """The control for the three tests above, each of which is satisfied by
        an empty mapping."""
        assert len(CORROBORATES) >= 16


@postgres
class TestAgainstRealDjango:
    def test_a_real_check_confirms_a_real_finding(self, live_project: Path) -> None:
        """End to end: Django's own binary, its own message, landing on a
        finding built by our own rule id."""
        from djaudit.live.checks import run_deployment_check
        from djaudit.live.sqlmigrate import Target

        from .test_sqlmigrate import interpreter

        target = Target(
            interpreter(live_project / ".venv" / "bin" / "python"),
            live_project / "manage.py",
            "django.db.backends.postgresql",
        )
        report = run_deployment_check(target)
        assert report.available, report.explain()
        assert "security.W009" in report.ids(), "the premise: the fixture has a weak SECRET_KEY"

        result = corroborate([finding("DJS-003")], report)
        (merged,) = result.findings
        assert merged.confidence is Confidence.CERTAIN
        assert "security.W009" in result.confirmed
        assert any("SECRET_KEY" in e.content for e in merged.evidence)

    def test_and_leaves_an_unrelated_finding_alone(self, live_project: Path) -> None:
        from djaudit.live.checks import run_deployment_check
        from djaudit.live.sqlmigrate import Target

        from .test_sqlmigrate import interpreter

        target = Target(
            interpreter(live_project / ".venv" / "bin" / "python"),
            live_project / "manage.py",
            "django.db.backends.postgresql",
        )
        result = corroborate([finding("DJS-015")], run_deployment_check(target))
        assert result.findings[0].confidence is Confidence.TENTATIVE


@postgres
class TestTheEngineDoesIt:
    """Corroboration lives in the engine, not in a rule, because no rule may
    edit another rule's output. These tests run the real pipeline."""

    @staticmethod
    def context(project: Path) -> ProjectContext:
        from dataclasses import replace as dc_replace

        from djaudit.discovery import build_context
        from djaudit.live.context import LiveContext

        from .test_sqlmigrate import interpreter

        ctx = build_context(project)
        return dc_replace(
            ctx,
            live=True,
            live_context=LiveContext(
                interpreter=interpreter(project / ".venv" / "bin" / "python"),
                django_version="6.1",
                settings_module="proj.settings",
                databases=(("default", "django.db.backends.postgresql"),),
            ),
        )

    def test_a_live_audit_confirms_settings_findings(self, live_project: Path) -> None:
        from djaudit.engine import run

        result = run(live_project, context=self.context(live_project))
        assert result.corroborated > 0, "the deployment check landed on nothing"

        upgraded = [f for f in result.findings if "deployment check agrees" in f.message]
        assert upgraded, [f.rule_id for f in result.findings]
        assert all(f.confidence is Confidence.CERTAIN for f in upgraded)

    def test_a_static_audit_asks_nobody(self, live_project: Path) -> None:
        """The control. Same project, same rules, no live context: nothing is
        confirmed, and no subprocess is run to find that out."""
        from djaudit.discovery import build_context
        from djaudit.engine import run

        result = run(live_project, context=build_context(live_project))
        assert result.corroborated == 0
        assert not [f for f in result.findings if "deployment check agrees" in f.message]

    def test_the_gaps_are_recorded_for_djs_028(self, live_project: Path) -> None:
        """Django flags settings we have no rule for. The engine stores them on
        the context; nothing reports them until 4.5.3."""
        ctx = self.context(live_project)
        from djaudit.engine import run

        run(live_project, context=ctx)
        assert ctx.deployment_gaps, "a bare project trips checks we do not cover"
        assert all(c.startswith("security.") for c in ctx.deployment_gaps)
