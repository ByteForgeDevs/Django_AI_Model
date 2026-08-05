"""The DRF defaults table, against DRF.

Django's defaults have been cross-checked against ``django.conf.global_settings``
since Phase 0, on the reasoning that a hand-maintained copy of another
project's defaults rots silently and a wrong default becomes a false positive
in every rule that reads it. DRF's defaults were transcribed with the same
risk and never checked at all.

``pyproject.toml`` said otherwise. It justified installing DRF as a dev
dependency "so tests can check our defaults table against the real
``api_settings.DEFAULTS`` instead of trusting a transcription", and no such
test existed. The only assertions naming ``DRF_DEFAULT_PERMISSIONS`` compared
it with itself -- the same closed loop the plan checker had, in a table where
being wrong means reporting every unguarded endpoint in a project as safe, or
every guarded one as open.

Which makes the version floor load bearing rather than cosmetic. The plan
supports DRF 3.17; ``pyproject.toml`` asked for ``>=3.15``, so the environment
these values are validated against was whatever the resolver felt like. That
is asserted here too, because a floor in a manifest is a request and a floor
in a test is a fact.

Imported directly rather than through ``pytest.importorskip``: DRF is a
declared dev dependency, so its absence is a broken environment and not an
optional extra. Skipping would make this file one more check that passes by
not running -- which is the failure it was written to close.
"""

from __future__ import annotations

import rest_framework
from rest_framework import permissions as drf_permissions
from rest_framework.settings import DEFAULTS

from djaudit.api.permissions import (
    DRF_DEFAULT_AUTHENTICATION,
    DRF_DEFAULT_PERMISSIONS,
    DRF_PERMISSIONS,
    SAFE_METHODS,
)

SUPPORTED = (3, 17)
"""The version in the plan's support matrix. See docs/PROJECT_PLAN.md section 2."""


class TestVersion:
    def test_the_installed_version_is_the_one_we_claim_to_support(self) -> None:
        installed = tuple(int(part) for part in rest_framework.VERSION.split(".")[:2])
        assert installed >= SUPPORTED, (
            f"DRF {rest_framework.VERSION} is installed but the support matrix says "
            f"{'.'.join(map(str, SUPPORTED))}; the values below were validated against "
            f"the wrong release"
        )


class TestAgainstRealDRF:
    def test_default_permission_classes_match(self) -> None:
        # The single line this whole analysis pass exists for: an installed
        # DRF that nobody configured is open to anonymous callers.
        assert list(DRF_DEFAULT_PERMISSIONS) == DEFAULTS["DEFAULT_PERMISSION_CLASSES"]

    def test_default_authentication_classes_match(self) -> None:
        assert list(DRF_DEFAULT_AUTHENTICATION) == DEFAULTS["DEFAULT_AUTHENTICATION_CLASSES"]

    def test_safe_methods_match(self) -> None:
        # Lowercased on our side to match how routers and ViewNode spell
        # methods, so this checks the set rather than the spelling.
        assert {m.lower() for m in drf_permissions.SAFE_METHODS} == SAFE_METHODS

    def test_the_permission_table_covers_every_class_drf_ships(self) -> None:
        # The table's docstring claims to be exhaustive, and a class missing
        # from it resolves to UNKNOWN -- which suppresses the rules that
        # require certainty, so a new DRF permission class would quietly
        # narrow the analysis rather than break it.
        shipped = {
            f"rest_framework.permissions.{name}"
            for name in dir(drf_permissions)
            if isinstance(obj := getattr(drf_permissions, name), type)
            and issubclass(obj, drf_permissions.BasePermission)
        }
        assert set(DRF_PERMISSIONS) == shipped

    def test_pagination_is_off_by_default(self) -> None:
        # DJD-003 and the availability rules all reason from "DRF paginates
        # nothing unless told to". If that ever changed, they would be
        # answering a question nobody asked.
        assert DEFAULTS["DEFAULT_PAGINATION_CLASS"] is None
        assert DEFAULTS["PAGE_SIZE"] is None

    def test_throttling_and_filtering_are_off_by_default(self) -> None:
        assert DEFAULTS["DEFAULT_THROTTLE_CLASSES"] == []
        assert DEFAULTS["DEFAULT_FILTER_BACKENDS"] == []
