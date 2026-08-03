"""The drf_project fixture: where the API family's recall is measured.

The manifest already asserts that all eighteen planted defects are found, and
`djaudit eval` runs in CI. What this module adds is the reasoning the manifest
cannot express: which findings must *not* appear next to which, and why one
settings line is responsible for four of the eighteen.

The controls are the point. `MyTicketViewSet` and `TicketViewSet` differ by one
`filter(owner=self.request.user)`, and a rule that cannot tell them apart would
report every scoped API in the world. `CommentViewSet` scopes across a
relation, which is the same intent written a way a naive check misses.
"""

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity


@pytest.fixture
def result(drf_project):
    found = engine.run(
        drf_project,
        min_severity=Severity.INFO,
        min_confidence=Confidence.TENTATIVE,
    )
    # A crashed rule reports nothing, and nothing is also what a rule with
    # nothing to say reports. This fixture would hide that without the check.
    assert not found.rule_errors, found.rule_errors
    return found


def ids(result, rule_id):
    return sorted(f.location.line for f in result.findings if f.rule_id == rule_id)


def test_every_api_rule_that_can_fire_does(result):
    """The headline: this fixture is the recall floor for DJA and DJD."""
    fired = {f.rule_id for f in result.findings}
    expected = {
        "DJA-001",
        "DJA-002",
        "DJA-003",
        "DJA-004",
        "DJA-005",
        "DJA-006",
        "DJA-007",
        "DJA-008",
        "DJA-009",
        "DJA-010",
        "DJA-011",
        "DJA-012",
        "DJA-014",
        "DJA-015",
        "DJD-001",
        "DJD-002",
    }
    assert expected <= fired, expected - fired


def test_the_unscoped_list_is_reported_and_the_scoped_ones_are_not(result):
    """DJA-004's whole value is this distinction."""
    assert ids(result, "DJA-004") == [34]


def test_scoping_across_a_relation_counts_as_scoping(result):
    """CommentViewSet filters on ticket__owner, which is one join away."""
    reported = {f.location.line for f in result.findings if f.rule_id == "DJA-004"}
    assert 69 not in reported


def test_an_explicit_harmless_field_list_is_left_alone(result):
    """InvoiceSerializer names its fields and none of them are sensitive."""
    exposure = {"DJA-008", "DJA-009", "DJA-010", "DJA-011", "DJA-012"}
    on_invoice = [f for f in result.findings if f.rule_id in exposure and f.location.line == 79]
    assert on_invoice == []


def test_configured_pagination_is_not_reported(result):
    """DJA-013 control: a class *and* a PAGE_SIZE is the correct pairing."""
    assert ids(result, "DJA-013") == []


def test_ordered_models_are_not_reported(result):
    """DJD-003 control: every model here declares Meta.ordering."""
    assert ids(result, "DJD-003") == []


def test_one_settings_line_opens_four_views(result):
    """DJA-001 states the cause; DJA-002 and DJA-006 count the consequences."""
    assert ids(result, "DJA-001") == [69]
    assert ids(result, "DJA-002") == [25, 125]


def test_the_login_endpoint_is_both_open_and_unthrottled(result):
    """Two rules, one endpoint, and neither makes the other redundant."""
    assert 103 in ids(result, "DJA-003")
    assert 103 in ids(result, "DJA-015")


def test_the_nested_leak_is_attributed_to_the_outer_serializer(result):
    """DJA-012 reports where the nesting was written, not where api_key was."""
    assert ids(result, "DJA-012") == [72]
    assert ids(result, "DJA-010") == [50]


def test_a_default_run_still_surfaces_the_critical_ones(drf_project):
    """The findings that matter survive the CLI's default thresholds."""
    found = engine.run(drf_project, min_severity=Severity.LOW, min_confidence=Confidence.FIRM)
    assert {f.rule_id for f in found.findings} >= {"DJA-003", "DJA-004", "DJA-010"}
