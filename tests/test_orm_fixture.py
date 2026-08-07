"""The orm_project fixture: where the performance family's recall is measured.

The benchmarks measure precision and cannot measure recall — NetBox and pretix
are mature, so almost anything djaudit says about them is a false positive, and
nothing tells us what it missed. That leaves recall entirely to this project.

The manifest already asserts that all fifteen planted defects are found and
that `inventory/controls.py` is silent, and `djaudit eval` runs in CI. What
this module adds is the part a manifest cannot express: that the defect and its
control differ by exactly one fetch, that the rules tell those apart, and which
rule is responsible for which half of a pair.
"""

import pytest

from djaudit import engine
from djaudit.models import Confidence, Severity


@pytest.fixture
def result(orm_project):
    found = engine.run(
        orm_project,
        min_severity=Severity.INFO,
        min_confidence=Confidence.TENTATIVE,
    )
    # A crashed rule reports nothing, and nothing is also what a rule with
    # nothing to say reports. Most of this fixture is control cases, so
    # without this check a whole broken family reads as a pass.
    assert not found.rule_errors, found.rule_errors
    return found


def ids(result, rule_id):
    return sorted(f.location.line for f in result.findings if f.rule_id == rule_id)


def files(result, rule_id):
    return sorted({f.location.file for f in result.findings if f.rule_id == rule_id})


def test_every_performance_rule_fires_here(result):
    """The headline: this fixture is the recall floor for DJP."""
    fired = {f.rule_id for f in result.findings}
    expected = {f"DJP-{n:03d}" for n in range(1, 11)}
    assert expected <= fired, expected - fired


def test_nothing_in_the_controls_file_is_ever_reported(result):
    """The other half of the headline, and the one that gets tools uninstalled.

    Reported by file rather than by rule, because the interesting failure is a
    rule nobody thought to check appearing here.
    """
    reported = sorted(
        (f.rule_id, f.location.line)
        for f in result.findings
        if f.location.file.endswith("controls.py")
    )
    assert reported == []


def test_djd_003_finally_has_an_end_to_end_case(result):
    """It shipped at the end of Phase 2 firing in no fixture at all.

    A rule whose zero has never been examined is not a passing control; it is
    an untested rule with a reassuring output. Two paginated viewsets list an
    unordered model here, and both are reported.
    """
    assert len(ids(result, "DJD-003")) == 2
    assert files(result, "DJD-003") == ["inventory/views.py"]


def test_the_serializer_pair_differs_only_by_the_view(result):
    """DJP-003's whole difficulty, in two identical getters.

    `DeviceSerializer.get_region` and `FetchedDeviceSerializer.get_region` are
    the same three tokens. Nothing in the serializer file decides this — the
    rule has to find the view that uses each one and read its queryset.
    """
    assert files(result, "DJP-003") == ["inventory/serializers.py"]
    assert len(ids(result, "DJP-003")) == 1


def test_a_prefetch_object_counts_as_a_prefetch(result):
    """`Prefetch("tags")` is `prefetch_related("tags")` with more typing."""
    assert 45 not in ids(result, "DJP-002")


def test_a_nested_prefetch_queryset_covers_the_second_level(result):
    """Reading `interface.device.site` is covered by the inner select_related.

    A rule reading only the outer relation name reports this and is wrong.
    """
    assert 62 not in ids(result, "DJP-001")


def test_to_attr_is_read_by_the_name_the_loop_uses(result):
    """Measured in scripts/prefetch_cache_probe.py: a `to_attr` prefetch does
    not serve a read of the original relation, and does serve the new name."""
    assert 78 not in ids(result, "DJP-002")


def test_the_whole_table_read_is_about_context_not_code(result):
    """DJP-008's only control is the identical read somewhere else.

    The same comprehension over the same unnarrowed queryset appears in a
    management command and in a request handler. The rule speaks about the
    first and not the second, on the reasoning that a table small enough to
    render is small enough to hold — so this is the one control in the fixture
    that cannot be un-fixed, because what guards it is not in the code.
    """
    assert files(result, "DJP-008") == ["inventory/management/commands/export_events.py"]


def test_counting_and_asking_are_told_apart(result):
    """DJP-006: `.count() > 0` is reported, `.exists()` is not."""
    assert files(result, "DJP-006") == ["inventory/views.py"]
    assert len(ids(result, "DJP-006")) == 1


def test_len_is_reported_only_when_the_rows_go_unread(result):
    """DJP-005: the same `len()` is fine if the queryset is used afterwards."""
    assert files(result, "DJP-005") == ["inventory/views.py"]
    assert len(ids(result, "DJP-005")) == 1


def test_only_is_reported_by_which_columns_the_loop_reads(result):
    """DJP-009: `only("name")` then reading `serial`, against `only()` naming
    both. Measured in the cache probe: the deferred read costs a query a row,
    and assigning to a deferred field does not."""
    assert files(result, "DJP-009") == ["inventory/views.py"]
    assert len(ids(result, "DJP-009")) == 1


def test_the_growing_table_is_reported_and_the_indexed_sort_is_not(result):
    """DJP-010: both viewsets list the same model; they differ in one column.

    `AuditEventViewSet` lets the caller sort by `detail`, an unindexed
    `TextField`; `RecentEventViewSet` allows only `created`, which is indexed.
    """
    assert len(ids(result, "DJP-010")) == 1


def test_the_control_viewset_is_not_excused_its_other_defect(result):
    """Being a control for one rule does not make code correct.

    `RecentEventViewSet` pages an unordered model, so DJD-003 reports it, and
    the manifest expects that. An earlier draft turned pagination off to keep
    the count tidy and traded DJD-003 for DJA-013 — an endpoint returning every
    row is not a fix, and a fixture that hides one rule behind another is lying
    about both.
    """
    assert "DJA-013" not in {f.rule_id for f in result.findings}


def test_the_authorization_family_stays_out_of_this(result):
    """Both viewsets scope with `get_queryset`, and permissions are set.

    A fixture that trips two families at once cannot show which one found what.
    """
    assert not [f for f in result.findings if f.rule_id.startswith("DJA")]


def test_a_default_run_still_surfaces_the_expensive_ones(orm_project):
    """The findings that matter survive the CLI's default thresholds."""
    found = engine.run(orm_project, min_severity=Severity.LOW, min_confidence=Confidence.FIRM)
    assert {f.rule_id for f in found.findings} >= {"DJP-003", "DJP-004", "DJP-010"}
