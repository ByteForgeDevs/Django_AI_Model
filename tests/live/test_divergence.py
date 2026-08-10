"""SQLite and PostgreSQL, asked Healthchecks' own questions, disagreeing.

Every other `DJX` test proves we *report* a construct. This module proves the
construct is worth reporting, by running it. The expressions are transcribed
from the four Healthchecks lines the family flags, the columns are declared as
Healthchecks declares them, and both engines are reached from one settings
module so a difference in the answers cannot be a difference between two
projects.

Measured on PostgreSQL 18.1 and the SQLite bundled with CPython 3.13, with one
`Project` row holding `api_key = 'ABCDEFGHij'` and one `Check` row holding
`tags = 'PROD staging'`:

| expression | flagged at | sqlite | postgres |
|---|---|---|---|
| `filter(api_key__startswith='abcdefgh')` | `accounts/models.py:411` | 1 | **0** |
| `filter(api_key_readonly__startswith='abcdefgh')` | `accounts/models.py:417` | 1 | **0** |
| `filter(tags__contains='prod')` | `api/views.py:750` | 1 | **0** |
| `filter(api_key__startswith='ABCDEFGH')` | control | 1 | 1 |
| `filter(tags__contains='PROD')` | control | 1 | 1 |

and `select_for_update()`, flagged at `api/models.py:510` and
`api/views.py:515`, compiles to SQL ending `FOR UPDATE` on PostgreSQL and to
the same statement with no locking clause at all on SQLite.

The two controls are the point of the table. A harness that reported a
divergence for every pair would produce exactly the first three rows and could
not be told apart from a correct one; the last two are the rows that say the
probe can also see two engines agree.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from djaudit import engine
from djaudit.models import Family

from .conftest import _isolated
from .divergence import ask, build_divergence

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")

pytestmark = postgres


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """One project for the module: it builds a virtualenv and two schemas."""
    yield from _isolated(tmp_path_factory.mktemp("divergence"), build_divergence)


@pytest.fixture(scope="module")
def answers(built: Path) -> dict[str, dict[str, Any]]:
    """What each engine says, gathered once, over the same rows."""
    return {alias: ask(built, alias) for alias in ("default", "sqlite")}


@pytest.fixture(scope="module")
def lite(answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return answers["sqlite"]


@pytest.fixture(scope="module")
def pg(answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return answers["default"]


class TestTheProbeReachedTwoEngines:
    """Asked twice and answered by one engine, every test below would pass."""

    def test_the_first_alias_is_postgres(self, pg: dict[str, Any]) -> None:
        assert pg["vendor"] == "postgresql"

    def test_the_second_alias_is_sqlite(self, lite: dict[str, Any]) -> None:
        assert lite["vendor"] == "sqlite"

    def test_they_are_not_the_same_engine(self, pg: dict[str, Any], lite: dict[str, Any]) -> None:
        assert pg["vendor"] != lite["vendor"]


class TestTheProbeCanSeeAgreement:
    """The control. Without it, the divergences below prove only that it lies."""

    def test_a_prefix_spelled_as_stored_is_found_on_sqlite(self, lite: dict[str, Any]) -> None:
        assert lite["startswith_same_case"] == 1

    def test_a_prefix_spelled_as_stored_is_found_on_postgres(self, pg: dict[str, Any]) -> None:
        assert pg["startswith_same_case"] == 1

    def test_a_substring_spelled_as_stored_is_found_on_sqlite(self, lite: dict[str, Any]) -> None:
        assert lite["contains_same_case"] == 1

    def test_a_substring_spelled_as_stored_is_found_on_postgres(self, pg: dict[str, Any]) -> None:
        assert pg["contains_same_case"] == 1

    def test_the_engines_agree_about_the_rows_that_exist(
        self, pg: dict[str, Any], lite: dict[str, Any]
    ) -> None:
        assert pg["startswith_same_case"] == lite["startswith_same_case"]
        assert pg["contains_same_case"] == lite["contains_same_case"]


class TestDJX004TheApiKeyLookup:
    """`hc/accounts/models.py:411`, and its read-only twin at 417."""

    def test_sqlite_matches_the_other_spelling(self, lite: dict[str, Any]) -> None:
        assert lite["startswith_other_case"] == 1

    def test_postgres_does_not(self, pg: dict[str, Any]) -> None:
        assert pg["startswith_other_case"] == 0

    def test_the_read_only_twin_diverges_the_same_way(
        self, pg: dict[str, Any], lite: dict[str, Any]
    ) -> None:
        assert (lite["readonly_startswith_other_case"], pg["readonly_startswith_other_case"]) == (
            1,
            0,
        )

    def test_the_development_engine_searches_wider(
        self, pg: dict[str, Any], lite: dict[str, Any]
    ) -> None:
        """The direction matters: SQLite selects rows PostgreSQL will not."""
        assert lite["startswith_other_case"] > pg["startswith_other_case"]


class TestDJX004TheTagLookup:
    """`hc/api/views.py:750` -- a badge for `prod` against a check tagged `PROD`."""

    def test_sqlite_matches(self, lite: dict[str, Any]) -> None:
        assert lite["contains_other_case"] == 1

    def test_postgres_does_not(self, pg: dict[str, Any]) -> None:
        assert pg["contains_other_case"] == 0

    def test_which_is_a_badge_populated_in_development_and_empty_in_production(
        self, pg: dict[str, Any], lite: dict[str, Any]
    ) -> None:
        assert lite["contains_other_case"] and not pg["contains_other_case"]


class TestDJX007TheLockThatIsNotTaken:
    """`hc/api/models.py:510` and `hc/api/views.py:515`.

    Both sites sit inside `transaction.atomic()` and both carry a comment
    naming the concurrent write they mean to serialise. The rule's claim is not
    that SQLite refuses the call -- it accepts it silently -- but that nothing
    is locked, which is the shape of failure nobody notices in development.
    """

    def test_postgres_asks_for_the_lock(self, pg: dict[str, Any]) -> None:
        assert pg["for_update_sql"].endswith("FOR UPDATE")

    def test_sqlite_does_not(self, lite: dict[str, Any]) -> None:
        assert "FOR UPDATE" not in lite["for_update_sql"]

    def test_sqlite_takes_no_lock_of_any_other_name_either(self, lite: dict[str, Any]) -> None:
        statement = lite["for_update_sql"].upper()
        assert not any(word in statement for word in ("LOCK", "UPDATE", "SHARE", "NOWAIT"))

    def test_the_call_is_accepted_rather_than_refused(self, lite: dict[str, Any]) -> None:
        """A raised error would be a bug found; silence is what makes it a rule."""
        assert lite["for_update_sql"].startswith("SELECT ")

    def test_the_two_statements_are_otherwise_the_same_query(
        self, pg: dict[str, Any], lite: dict[str, Any]
    ) -> None:
        assert lite["for_update_sql"] == pg["for_update_sql"].removesuffix(" FOR UPDATE")


@pytest.fixture(scope="module")
def reported(built: Path) -> set[str]:
    """What djaudit says about the very project the probe just ran."""
    return {f.rule_id for f in engine.run(built, families={Family.DJX}).findings}


class TestWeReportWhatTheEnginesDo:
    """The loop closed: the engines diverge here, and we say so here.

    Without this the module proves a fact about two databases that the analyser
    might have no opinion about, and the static tests prove an opinion that
    might be about nothing.
    """

    def test_the_case_folding_lookups_are_reported(self, reported: set[str]) -> None:
        assert "DJX-004" in reported

    def test_the_unlocked_select_is_reported(self, reported: set[str]) -> None:
        assert "DJX-007" in reported

    def test_every_divergence_the_probe_measured_is_one_we_report(self, reported: set[str]) -> None:
        assert {"DJX-004", "DJX-007"} <= reported

    def test_the_family_gate_opened_because_the_project_reaches_both(
        self, reported: set[str]
    ) -> None:
        """`DivergenceRule` gates on `reaches`, so an empty set means the gate.

        This is the difference between the rules being right and the rules
        being unreachable, and it is the failure mode that would make every
        assertion above vacuous rather than false.
        """
        assert reported

    def test_djx001_is_absent_and_that_is_correct(self, reported: set[str]) -> None:
        """The one flagged Healthchecks line this fixture cannot carry.

        `DJX-001` reports development and production running *different*
        engines, which Healthchecks does by branching on a `DB` environment
        variable. This fixture reaches both engines from one settings module
        through two aliases instead, because the probe has to open both
        connections in one process to compare answers over the same rows. Two
        aliases are not two environments, so the rule correctly says nothing,
        and asserting it here would have meant reshaping the fixture around a
        rule rather than around the measurement.
        """
        assert "DJX-001" not in reported
