"""The near_miss_project fixture: nothing here is a defect.

The recall fixtures answer "does the rule fire?". This one answers the question
that decides whether a security tool survives contact with a real team: on a
project that already did the work, but did not write it the way the textbook
does, does the tool shut up?

Every setting in that fixture is correct and every one of them is one sloppy
check away from a finding -- a leading dot read as a wildcard, MD5 found in a
list without noticing it is last, ATOMIC_REQUESTS seen without noticing it is
inside the alias. The headline assertion is the first one: a default run
reports nothing and exits zero.
"""

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity

# What the CLI actually uses. An `info`/`tentative` finding exists in the
# result and is invisible here by design, which is the distinction this module
# is built around.
CLI_SEVERITY = Severity.LOW
CLI_CONFIDENCE = Confidence.FIRM


@pytest.fixture
def result(near_miss_project):
    found = engine.run(
        near_miss_project,
        min_severity=Severity.INFO,
        min_confidence=Confidence.TENTATIVE,
    )
    assert not found.rule_errors, found.rule_errors
    return found


def test_a_default_run_is_silent(near_miss_project):
    """The headline. Nothing correct-but-unusual reaches a default run."""
    found = engine.run(near_miss_project, min_severity=CLI_SEVERITY, min_confidence=CLI_CONFIDENCE)
    assert found.findings == []


def test_only_the_two_informational_branches_speak_at_all(result):
    """Both describe a risk whose size depends on something outside the source.

    DJS-012 cannot know who terminates TLS; DJS-014 cannot know who is allowed
    to create a subdomain. Neither is wrong to mention it, and neither has any
    business interrupting a default run to do so.
    """
    assert sorted(f.rule_id for f in result.findings) == ["DJS-012", "DJS-014"]
    assert all(f.confidence is Confidence.TENTATIVE for f in result.findings)


@pytest.mark.parametrize(
    ("rule_id", "why"),
    [
        ("DJS-001", "DEBUG is a comparison against the environment, not a literal True"),
        ("DJS-002", "SECRET_KEY holds a path that is read at startup, not a key"),
        ("DJS-004", "an empty PASSWORD is client-certificate auth, not a leak"),
        ("DJS-013", "a leading dot is Django's subdomain syntax, not the wildcard"),
        ("DJS-016", "credentials are allowed against an explicit origin list"),
        ("DJS-017", "SAMEORIGIN is Django's default, not protection switched off"),
        ("DJS-019", "MD5 is last, which is legacy verification on an Argon2 project"),
        ("DJS-023", "ATOMIC_REQUESTS is inside the alias, where Django reads it"),
        ("DJS-024", "the toolbar is installed only under `if DEBUG`"),
        ("DJS-025", "the dev tooling is in requirements-dev.txt"),
        ("DJS-027", "the reporter filter extends Django's rather than replacing it"),
    ],
)
def test_the_shape_that_looks_like_the_defect_is_not_reported(result, rule_id, why):
    reported = [f for f in result.findings if f.rule_id == rule_id]
    assert reported == [], f"{rule_id} fired, but {why}"


@pytest.mark.parametrize(
    ("rule_id", "why"),
    [
        ("DJA-001", "the project default names a permission class the project wrote"),
        ("DJA-002", "every routed view declares permission_classes itself"),
        ("DJA-003", "no writable endpoint is left open"),
        ("DJA-004", "all three unscoped-looking querysets are narrowed elsewhere"),
        ("DJA-005", "get_object filters on the request and checks object permissions"),
        ("DJA-006", "the function view states its permission through the decorator"),
        ("DJA-007", "the empty authentication list is paired with AllowAny"),
        ("DJA-008", "no serializer uses `__all__`"),
        ("DJA-009", "no serializer uses `exclude`"),
        ("DJA-010", "password and api_key are both write-only, by different spellings"),
        ("DJA-011", "owner is in read_only_fields"),
        ("DJA-012", "the nested serializer returns none of the secrets it can reach"),
        ("DJA-013", "SizedPagination assigns its own page_size"),
        ("DJA-014", "filterset_fields names two harmless columns"),
        ("DJA-015", "the credential endpoints are throttled and the rates are set"),
        ("DJD-001", "the retained record is PROTECT and the disposable one is CASCADE"),
        ("DJD-002", "one nullable column is blank=True and the other is unique"),
        ("DJD-003", "ordering comes from an abstract base or from the manager"),
    ],
)
def test_the_api_shape_that_looks_like_the_defect_is_not_reported(result, rule_id, why):
    reported = [f for f in result.findings if f.rule_id == rule_id]
    assert reported == [], f"{rule_id} fired, but {why}"


def test_the_catalog_app_was_actually_read(result):
    """The silence has to be earned.

    A fixture the discovery pass never reached would satisfy every assertion
    above and prove nothing at all, which is the failure mode this whole module
    exists to catch elsewhere.
    """
    surface = result.context.api_surface
    assert len(surface.routes.routed()) == 9
    assert len(surface.serializers) == 5
    assert len(result.context.model_graph.models) == 6


@pytest.mark.parametrize(
    ("view", "why"),
    [
        ("BookmarkViewSet", "ScopedMixin.initial rebinds self.queryset from the request"),
        ("InvoiceViewSet", "the unscoped return is reached only under an is_staff test"),
        ("SubscriptionViewSet", "for_user is passed the request user"),
        ("RecentAuditViewSet", "super().get_queryset() carries the parent's scoping"),
    ],
)
def test_each_scoping_shape_is_read_rather_than_skipped(result, view, why):
    """Which is not the same as DJA-004 staying quiet.

    A queryset whose model never resolved is skipped before scoping is even
    considered, and looks identical in the report to one the rule read and
    cleared. `RecentAuditViewSet` was exactly that until the delegation fix:
    silent, and silent for the wrong reason.
    """
    node = result.context.api_surface.querysets[f"catalog.views.{view}"]
    assert node.model is not None, f"{view}: model unresolved, so DJA-004 never looked"
    assert not node.unfiltered, f"{view}: read as unscoped, but {why}"
