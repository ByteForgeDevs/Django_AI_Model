"""Path scope and the severity demotion that follows from it.

The classifier is deliberately segment-based rather than substring-based, which
is the only interesting thing about it: `latest/` contains "test" and
`contest.py` starts with "contest", and both are production code.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from djaudit.models import Confidence, Family, Finding, Location, Severity, Tier
from djaudit.scope import Scope, apply, classify


def finding(file: str, severity: Severity = Severity.HIGH) -> Finding:
    return Finding(
        rule_id="DJP-004",
        title="t",
        severity=severity,
        confidence=Confidence.FIRM,
        family=Family.DJP,
        tier=Tier.STATIC,
        location=Location(file=file, line=1, column=0, end_line=1, snippet="x"),
        message="m",
        rationale="r",
        remediation="f",
    )


class TestClassify:
    @pytest.mark.parametrize(
        "path",
        [
            "hc/api/migrations/0067_last_error_values.py",
            "src/pretix/base/migrations/0001_initial.py",
            "app/migrations/__init__.py",
        ],
    )
    def test_a_migration_is_a_migration(self, path: str) -> None:
        assert classify(path) is Scope.MIGRATIONS

    @pytest.mark.parametrize(
        "path",
        [
            "hc/lib/tests/test_emails.py",
            "netbox/utilities/testing/base.py",
            "src/tests/conftest.py",
            "hc/logs/tests.py",
            "app/test_views.py",
        ],
    )
    def test_test_support_is_tests(self, path: str) -> None:
        assert classify(path) is Scope.TESTS

    @pytest.mark.parametrize(
        "path",
        [
            "netbox/dcim/utils.py",
            "hc/front/views.py",
            "app/latest/views.py",
            "app/contest.py",
            "app/protest/models.py",
        ],
    )
    def test_a_substring_match_is_not_a_segment_match(self, path: str) -> None:
        assert classify(path) is Scope.PRODUCTION

    def test_a_migration_inside_a_test_app_is_still_a_migration(self) -> None:
        """Frozen-once-applied is the property that makes it hard to fix."""
        assert classify("tests/app/migrations/0001_initial.py") is Scope.MIGRATIONS

    def test_a_directory_named_tests_does_not_capture_its_own_parent(self) -> None:
        assert classify("tests.py") is Scope.TESTS
        assert classify("testsuite.py") is Scope.PRODUCTION


class TestApply:
    def test_production_keeps_its_severity(self) -> None:
        out = apply(finding("app/views.py"))
        assert out.severity is Severity.HIGH
        assert out.properties["scope"] == "production"

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (Severity.CRITICAL, Severity.HIGH),
            (Severity.HIGH, Severity.MEDIUM),
            (Severity.MEDIUM, Severity.LOW),
            (Severity.LOW, Severity.INFO),
            (Severity.INFO, Severity.INFO),
        ],
    )
    def test_a_test_file_drops_one_rank(self, given: Severity, expected: Severity) -> None:
        assert apply(finding("app/tests/test_x.py", given)).severity is expected

    def test_info_is_a_floor(self) -> None:
        assert apply(finding("app/migrations/0001.py", Severity.INFO)).severity is Severity.INFO

    def test_the_scope_is_recorded_so_the_demotion_is_auditable(self) -> None:
        """A severity that differs from the rule's declared one has to say why."""
        assert apply(finding("app/migrations/0001.py")).properties["scope"] == "migrations"

    def test_existing_properties_survive(self) -> None:
        base = replace(finding("app/views.py"), properties={"model": "app.Book"})
        out = apply(base)
        assert out.properties == {"model": "app.Book", "scope": "production"}
