"""Tests for the Django defaults table.

The values are checked against Django itself when it is installed, because a
hand-maintained copy of another project's defaults rots silently and a wrong
default turns into a false positive in every rule that reads it.
"""

from __future__ import annotations

import pytest

from djaudit.settings import (
    DATABASE_DEFAULTS,
    DEFAULTS_BY_VERSION,
    DJANGO_DEFAULTS,
    INTRODUCED_IN,
    Origin,
    django_default,
    parse_version,
)
from djaudit.values import Value

django = pytest.importorskip("django", reason="defaults are cross-checked only when installed")


def required(value: Value | None) -> Value:
    """Assert a default exists and hand back the value, keeping mypy happy."""
    assert value is not None
    return value


class TestAgainstRealDjango:
    def test_every_default_matches_django(self) -> None:
        from django.conf import global_settings

        mismatched = {
            name: (expected, getattr(global_settings, name))
            for name, expected in DJANGO_DEFAULTS.items()
            if hasattr(global_settings, name) and getattr(global_settings, name) != expected
        }
        assert not mismatched

    def test_every_name_in_the_table_is_a_real_setting(self) -> None:
        from django.conf import global_settings

        assert [n for n in DJANGO_DEFAULTS if not hasattr(global_settings, n)] == []

    def test_version_specific_defaults_match_the_installed_release(self) -> None:
        from django.conf import global_settings

        release = parse_version(django.get_version())
        assert release is not None
        for name, expected in DEFAULTS_BY_VERSION.get(release, {}).items():
            assert getattr(global_settings, name) == expected

    def test_settings_marked_as_new_really_are_absent_before_their_release(self) -> None:
        from django.conf import global_settings

        release = parse_version(django.get_version())
        assert release is not None
        for name, introduced in INTRODUCED_IN.items():
            if release < introduced:
                assert not hasattr(global_settings, name), name
            else:
                assert hasattr(global_settings, name), name

    def test_per_database_defaults_match_django(self) -> None:
        from django.db.utils import ConnectionHandler

        handler = ConnectionHandler(
            {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "x"}}
        )
        configured = handler.databases["default"]
        for name, expected in DATABASE_DEFAULTS.items():
            assert configured[name] == expected, name

    def test_no_settings_are_shared_between_the_flat_and_versioned_tables(self) -> None:
        # A name in both would make the version-specific value unreachable.
        for versioned in DEFAULTS_BY_VERSION.values():
            assert not set(versioned) & set(DJANGO_DEFAULTS)


class TestLookup:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [("5.2", (5, 2)), ("5.2.1", (5, 2)), ("6.0.7", (6, 0)), ("6", (6, 0))],
    )
    def test_parse_version(self, version: str, expected: tuple[int, int]) -> None:
        assert parse_version(version) == expected

    @pytest.mark.parametrize("version", [None, "", "latest", ">=4.2", "5.x"])
    def test_unparseable_versions(self, version: str | None) -> None:
        assert parse_version(version) is None

    def test_a_version_independent_default_needs_no_version(self) -> None:
        assert required(django_default("DEBUG")).literal is False

    def test_a_version_dependent_default_needs_a_version(self) -> None:
        # Guessing here is the version drift the plan lists as a risk.
        assert django_default("DEFAULT_AUTO_FIELD") is None
        assert required(django_default("DEFAULT_AUTO_FIELD", "5.2")).literal == (
            "django.db.models.AutoField"
        )
        assert required(django_default("DEFAULT_AUTO_FIELD", "6.0")).literal == (
            "django.db.models.BigAutoField"
        )

    def test_a_setting_that_postdates_the_release_has_no_default(self) -> None:
        assert django_default("SECURE_CSP", "5.2") is None

    def test_a_new_setting_we_hold_no_value_for_is_still_none(self) -> None:
        # SECURE_CSP is tracked so rules can gate on the release, but we hold
        # no value for it until a rule needs one.
        assert django_default("SECURE_CSP", "6.0") is None

    def test_an_unknown_setting_has_no_default(self) -> None:
        assert django_default("MY_PROJECT_FEATURE_FLAG", "6.0") is None

    def test_a_new_setting_with_an_undetectable_version_has_no_default(self) -> None:
        assert django_default("SECURE_CSP", None) is None


class TestViewFallback:
    def test_an_unset_setting_reports_the_django_default(self, overridden_project) -> None:
        from djaudit.discovery import build_context
        from djaudit.settings import resolve_all

        ctx = build_context(overridden_project)
        resolved = resolve_all(ctx)["config.settings.production"].get("SECURE_HSTS_SECONDS")
        assert resolved.origin is Origin.DJANGO_DEFAULT
        assert resolved.value.literal == 0
        assert resolved.definition is None

    def test_an_explicit_assignment_still_wins(self, overridden_project) -> None:
        from djaudit.discovery import build_context
        from djaudit.settings import resolve_all

        ctx = build_context(overridden_project)
        resolved = resolve_all(ctx)["config.settings.production"].get("DEBUG")
        assert resolved.origin is Origin.EXPLICIT

    def test_a_setting_we_hold_no_default_for_is_absent(self, overridden_project) -> None:
        from djaudit.discovery import build_context
        from djaudit.settings import resolve_all

        ctx = build_context(overridden_project)
        resolved = resolve_all(ctx)["config.settings.production"].get("STRIPE_SECRET_KEY")
        assert resolved.origin is Origin.ABSENT
