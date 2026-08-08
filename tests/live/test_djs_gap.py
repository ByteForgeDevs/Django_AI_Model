"""`DJS-028` — the rule that audits us.

The interesting property is not that it fires. It is that it fires on things we
have a rule for and missed, and that it reports nothing at all when nobody
asked Django -- because "we found no gaps" and "we never looked" must not read
the same.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from djaudit.context import ProjectContext, SettingsModule, SettingsRole
from djaudit.live.checks import Report, parse_report
from djaudit.live.checks import Unknown as ChecksUnknown
from djaudit.models import Confidence, Severity, Tier
from djaudit.rules.djs_deployment_check_gap import DeploymentCheckGap

from .test_sqlmigrate import interpreter

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")

REFERRER = "?: (security.W022) You have not set the SECURE_REFERRER_POLICY setting."
HSTS = "?: (security.W004) You have not set a value for the SECURE_HSTS_SECONDS setting."


def report_of(*lines: str) -> Report | ChecksUnknown:
    body = "\n".join(lines)
    return parse_report(
        f"System check identified some issues:\n\nWARNINGS:\n{body}\n"
        f"\nSystem check identified {len(lines)} issues (0 silenced).\n"
    )


def context(root: Path, *, gaps: set[str], lines: tuple[str, ...] = (REFERRER,)) -> ProjectContext:
    (root / "proj").mkdir(parents=True, exist_ok=True)
    settings = root / "proj" / "settings.py"
    settings.write_text("SECRET_KEY = 'x'\n")
    return ProjectContext(
        root=root,
        live=True,
        settings_modules=(
            SettingsModule(
                path=settings,
                dotted="proj.settings",
                role=SettingsRole.PRODUCTION,
                is_entrypoint=True,
            ),
        ),
        deployment_report=report_of(*lines),
        deployment_gaps=frozenset(gaps),
    )


class TestReportingWhatWeMissed:
    def test_a_gap_becomes_a_finding(self, tmp_path: Path) -> None:
        (found,) = DeploymentCheckGap().check(context(tmp_path, gaps={"security.W022"}))
        assert found.rule_id == "DJS-028"

    def test_it_names_the_check(self, tmp_path: Path) -> None:
        (found,) = DeploymentCheckGap().check(context(tmp_path, gaps={"security.W022"}))
        assert "`security.W022`" in found.message
        assert "no rule of ours did" in found.message

    def test_it_quotes_django_rather_than_paraphrasing(self, tmp_path: Path) -> None:
        (found,) = DeploymentCheckGap().check(context(tmp_path, gaps={"security.W022"}))
        (evidence,) = found.evidence
        assert evidence.source == "manage.py check --deploy"
        assert evidence.content == (
            "security.W022: You have not set the SECURE_REFERRER_POLICY setting."
        )

    def test_it_points_at_the_settings_module(self, tmp_path: Path) -> None:
        """Django reports no file and no line. The entrypoint settings module
        is the nearest true thing there is to point at."""
        (found,) = DeploymentCheckGap().check(context(tmp_path, gaps={"security.W022"}))
        assert found.location.file == "proj/settings.py"

    def test_it_is_certain(self, tmp_path: Path) -> None:
        """Django resolved the value and said so. There is nothing tentative
        about it."""
        (found,) = DeploymentCheckGap().check(context(tmp_path, gaps={"security.W022"}))
        assert found.confidence is Confidence.CERTAIN

    def test_the_severity_is_djangos_level(self, tmp_path: Path) -> None:
        (found,) = DeploymentCheckGap().check(context(tmp_path, gaps={"security.W022"}))
        assert found.severity is Severity.MEDIUM

    def test_every_gap_is_reported(self, tmp_path: Path) -> None:
        ctx = context(tmp_path, gaps={"security.W022", "security.W004"}, lines=(REFERRER, HSTS))
        found = list(DeploymentCheckGap().check(ctx))
        assert {f.message.split("`")[1] for f in found} == {"security.W022", "security.W004"}

    def test_and_in_a_stable_order(self, tmp_path: Path) -> None:
        """Two findings on the same line of the same file would otherwise sort
        by whatever order a set iterated in that day."""
        ctx = context(tmp_path, gaps={"security.W022", "security.W004"}, lines=(REFERRER, HSTS))
        found = list(DeploymentCheckGap().check(ctx))
        assert [f.message.split("`")[1] for f in found] == ["security.W004", "security.W022"]

    def test_a_hint_is_carried_into_the_evidence(self, tmp_path: Path) -> None:
        ctx = context(tmp_path, gaps={"security.W001"})
        ctx = replace(
            ctx,
            deployment_report=parse_report(
                "System check identified some issues:\n\nWARNINGS:\n"
                "?: (security.W001) No SecurityMiddleware.\n"
                "\tHINT: add it.\n\nSystem check identified 1 issue (0 silenced).\n"
            ),
        )
        (found,) = DeploymentCheckGap().check(ctx)
        assert found.evidence[0].content.endswith("\n\tHINT: add it.")


class TestSilenceMeansSilence:
    def test_a_static_run_reports_nothing(self, tmp_path: Path) -> None:
        """Not "no gaps found" -- nobody was asked. The rule is `Tier.LIVE` and
        its fallback sentence says exactly this."""
        ctx = replace(context(tmp_path, gaps={"security.W022"}), live=False)
        assert list(DeploymentCheckGap().check(ctx)) == []

    def test_the_control_is_the_same_context_live(self, tmp_path: Path) -> None:
        """Identical in every respect but `live`, so the test above is about
        the flag rather than about a context that reports nothing anyway."""
        ctx = context(tmp_path, gaps={"security.W022"})
        assert len(list(DeploymentCheckGap().check(ctx))) == 1
        assert list(DeploymentCheckGap().check(replace(ctx, live=False))) == []

    def test_no_gaps_is_no_findings(self, tmp_path: Path) -> None:
        assert list(DeploymentCheckGap().check(context(tmp_path, gaps=set()))) == []

    def test_a_gap_with_nothing_to_quote_is_not_reported(self, tmp_path: Path) -> None:
        """A finding with no evidence is the one thing this tool must not
        emit. The gap set is built from the report, so this needs a
        hand-assembled context to reach at all."""
        ctx = context(tmp_path, gaps={"security.W999"})
        assert list(DeploymentCheckGap().check(ctx)) == []

    def test_no_report_at_all_reports_nothing(self, tmp_path: Path) -> None:
        ctx = replace(context(tmp_path, gaps={"security.W022"}), deployment_report=None)
        assert list(DeploymentCheckGap().check(ctx)) == []


class TestTheRuleDeclaresItself:
    def test_it_is_a_live_rule(self) -> None:
        assert DeploymentCheckGap.meta.tier is Tier.LIVE

    def test_it_declares_what_is_lost_without_the_live_tier(self) -> None:
        assert "unmeasured" in DeploymentCheckGap.meta.fallback

    def test_it_is_registered(self) -> None:
        from djaudit import registry

        assert "DJS-028" in {r.meta.id for r in registry.all_rules()}

    def test_it_runs_after_corroboration(self) -> None:
        """Its subject is the other rules' output, so running it in the main
        pass would read an empty gap set every time."""
        from djaudit.engine import AFTER_CORROBORATION

        assert "DJS-028" in AFTER_CORROBORATION


class TestLocatingWithoutASettingsModule:
    def test_it_falls_back_to_manage_py(self, tmp_path: Path) -> None:
        """A project whose settings we never discovered still gets the finding,
        because Django found the settings even if we did not."""
        ctx = ProjectContext(
            root=tmp_path,
            live=True,
            deployment_report=report_of(REFERRER),
            deployment_gaps=frozenset({"security.W022"}),
        )
        (found,) = DeploymentCheckGap().check(ctx)
        assert found.location.file == "manage.py"

    def test_a_non_entrypoint_module_is_used_when_that_is_all_there_is(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "proj").mkdir()
        path = tmp_path / "proj" / "base.py"
        path.write_text("X = 1\n")
        ctx = ProjectContext(
            root=tmp_path,
            live=True,
            settings_modules=(
                SettingsModule(path=path, dotted="proj.base", role=SettingsRole.BASE),
            ),
            deployment_report=report_of(REFERRER),
            deployment_gaps=frozenset({"security.W022"}),
        )
        (found,) = DeploymentCheckGap().check(ctx)
        assert found.location.file == "proj/base.py"

    def test_the_entrypoint_wins_over_a_base_module(self, tmp_path: Path) -> None:
        (tmp_path / "proj").mkdir()
        for name in ("base.py", "prod.py"):
            (tmp_path / "proj" / name).write_text("X = 1\n")
        ctx = ProjectContext(
            root=tmp_path,
            live=True,
            settings_modules=(
                SettingsModule(
                    path=tmp_path / "proj" / "base.py",
                    dotted="proj.base",
                    role=SettingsRole.BASE,
                ),
                SettingsModule(
                    path=tmp_path / "proj" / "prod.py",
                    dotted="proj.prod",
                    role=SettingsRole.PRODUCTION,
                    is_entrypoint=True,
                ),
            ),
            deployment_report=report_of(REFERRER),
            deployment_gaps=frozenset({"security.W022"}),
        )
        (found,) = DeploymentCheckGap().check(ctx)
        assert found.location.file == "proj/prod.py"


@postgres
class TestAgainstRealDjango:
    """The whole point of the rule is that it measures us against something we
    did not write, so the test that matters runs Django."""

    @staticmethod
    def live(project: Path) -> ProjectContext:
        from djaudit.discovery import build_context
        from djaudit.live.context import LiveContext

        return replace(
            build_context(project),
            live=True,
            live_context=LiveContext(
                interpreter=interpreter(project / ".venv" / "bin" / "python"),
                django_version="6.1",
                settings_module="proj.settings",
                databases=(("default", "django.db.backends.postgresql"),),
            ),
        )

    def test_a_real_audit_measures_our_own_recall(self, live_project: Path) -> None:
        from djaudit.engine import run

        result = run(live_project, context=self.live(live_project))
        gaps = [f for f in result.findings if f.rule_id == "DJS-028"]
        assert gaps, "a bare project trips checks we do not cover"
        assert all(f.confidence is Confidence.CERTAIN for f in gaps)
        assert all(f.evidence for f in gaps), "every finding cites Django's own sentence"

    def test_the_checks_we_do_cover_are_not_reported_as_gaps(self, live_project: Path) -> None:
        """`security.W009` is a weak SECRET_KEY, which `DJS-003` finds. It must
        appear as a corroboration, not as a hole in our coverage."""
        from djaudit.engine import run

        result = run(live_project, context=self.live(live_project))
        named = " ".join(f.message for f in result.findings if f.rule_id == "DJS-028")
        assert "security.W009" not in named
        assert result.corroborated > 0

    def test_a_static_run_of_the_same_project_finds_no_gaps(self, live_project: Path) -> None:
        """The control, and the property that keeps the rule honest: without
        the second opinion the number is zero, and zero here means unmeasured."""
        from djaudit.discovery import build_context
        from djaudit.engine import run

        result = run(live_project, context=build_context(live_project))
        assert [f for f in result.findings if f.rule_id == "DJS-028"] == []
