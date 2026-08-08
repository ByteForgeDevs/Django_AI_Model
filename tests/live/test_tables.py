"""Table sizing, against a real database wherever the claim needs one.

The severity thresholds here are read backwards out of measured rewrite times,
so the tests that matter most are the ones that run the query for real: a
constant nobody re-derives is a constant that quietly stops being true.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from djaudit.live.sqlmigrate import Target
from djaudit.live.tables import (
    MARKER,
    MEGABYTE,
    NOTICEABLE,
    SCRIPT,
    SUSTAINED,
    Size,
    Sizes,
    Unknown,
    estimate,
    parse_sizes,
    read_sizes,
)

from .test_sqlmigrate import interpreter

DSN = os.environ.get("DJAUDIT_TEST_POSTGRES", "")

postgres = pytest.mark.skipif(not DSN, reason="set DJAUDIT_TEST_POSTGRES to a libpq DSN")

POSTGRES = "django.db.backends.postgresql"


class TestReadingTheMarkedLine:
    """`shell -c` output is not ours alone: Django writes an auto-import banner
    to stdout before the script runs, and a settings module may print anything
    it likes. The marker is how our line is found among it."""

    def test_the_sizes_are_read(self) -> None:
        parsed = parse_sizes(MARKER + '[["blog_post", 5000, 1048576]]')
        assert parsed == {"blog_post": Size("blog_post", 1048576, 5000)}

    def test_surrounding_chatter_is_ignored(self) -> None:
        output = (
            "11 objects imported automatically (use -v 2 for details).\n"
            "settings module says hello\n" + MARKER + '[["t", 1, 8192]]\n'
        )
        assert parse_sizes(output)["t"].stored == 8192

    def test_a_never_analysed_table_has_no_row_count(self) -> None:
        """`reltuples` is -1, not 0. Reading it as zero would call every table
        in a freshly restored database empty."""
        assert parse_sizes(MARKER + '[["t", -1, 8192]]')["t"].rows is None

    def test_and_that_is_not_the_same_as_zero(self) -> None:
        """The contrast: an analysed, genuinely empty table is knowable."""
        assert parse_sizes(MARKER + '[["t", 0, 8192]]')["t"].rows == 0

    def test_no_marked_line_is_no_data(self) -> None:
        assert parse_sizes("11 objects imported automatically.\n") == {}

    def test_a_marked_line_that_is_not_json_is_no_data(self) -> None:
        assert parse_sizes(MARKER + "not json at all") == {}


class TestWhatTheNumbersMean:
    def test_a_table_past_a_second_is_described_in_seconds(self) -> None:
        assert "roughly 2s" in str(estimate(Size("t", 128 * MEGABYTE, 2_000_000)))

    def test_a_middling_table_is_described_in_milliseconds(self) -> None:
        text = str(estimate(Size("t", 9 * MEGABYTE, 100_000)))
        assert "roughly 144ms" in text

    def test_a_small_table_is_named_as_harmless(self) -> None:
        assert "measured in milliseconds" in str(estimate(Size("t", 96 * 1024, 1000)))

    def test_each_sentence_says_which_table_and_how_big(self) -> None:
        """Asserting only the tail of a sentence leaves its subject unchecked,
        and all three bands share that subject."""
        for stored in (128 * MEGABYTE, 9 * MEGABYTE, 96 * 1024):
            text = str(estimate(Size("t", stored, 4)))
            assert text.startswith("The table holds "), text
            assert "4 rows" in text

    def test_nothing_measured_says_nothing(self) -> None:
        assert estimate(None) is None

    def test_an_unanalysed_table_still_reports_its_size(self) -> None:
        """Size is exact even when the row count is not, so the sentence must
        not go silent just because `reltuples` was -1."""
        text = str(estimate(Size("t", 100 * MEGABYTE, None)))
        assert "row count not analysed" in text
        assert "100.0 MB" in text

    def test_the_size_is_rounded_to_one_decimal(self) -> None:
        """A round number cannot show rounding: 100 MB formats identically with
        and without the format spec. 95.367... is what a real table looks like."""
        assert Size("t", 100_000_000, 1).describe() == "95.4 MB, 1 rows"

    def test_a_row_count_is_grouped(self) -> None:
        assert Size("t", 1, 1_500_000).describe() == "0.0 MB, 1,500,000 rows"


class TestTheThresholdsMatchTheMeasurements:
    """Measured on PostgreSQL 18.1, rewriting varchar(200) to varchar(50):
    1k rows/96kB took 12ms, 10k/912kB 22ms, 100k/8.9MB 135ms, 1M/88.8MB 1422ms.
    The thresholds are that 16 ms/MB rate read backwards, so they have to agree
    with it rather than merely being round numbers."""

    def test_the_hundred_thousand_row_case_lands_in_the_middle_band(self) -> None:
        measured = 9 * MEGABYTE
        assert NOTICEABLE <= measured < SUSTAINED

    def test_the_million_row_case_lands_in_the_top_band(self) -> None:
        assert 88 * MEGABYTE >= SUSTAINED

    def test_the_ten_thousand_row_case_lands_in_the_bottom_band(self) -> None:
        assert NOTICEABLE > 912 * 1024

    def test_the_estimate_reproduces_the_measurement(self) -> None:
        """The 1M-row rewrite took 1422ms. The model must land within a factor
        of two of that, or the rate is wrong rather than approximate."""
        predicted = Size("t", 88_800_000, 1_000_000).milliseconds
        assert 711 <= predicted <= 2844, predicted


class TestNothingIsQuietedByIgnorance:
    def test_an_unreadable_database_answers_nothing(self) -> None:
        assert Unknown("no").get("blog_post") is None

    def test_and_says_why(self) -> None:
        assert Unknown("connection refused").explain() == (
            "table sizes are unknown: connection refused"
        )

    def test_an_unrecognised_table_answers_nothing(self) -> None:
        sizes = Sizes({"blog_post": Size("blog_post", 1, 1)})
        assert sizes.get("other") is None

    def test_the_count_of_measured_tables_is_reported(self) -> None:
        sizes = Sizes({"a": Size("a", 1, 1), "b": Size("b", 1, 1)})
        assert sizes.explain() == "2 tables measured"
        assert sizes.available

    def test_a_missing_table_name_answers_nothing(self) -> None:
        """`Verdict.table` is `None` for a statement the classifier matched but
        could not name a relation in."""
        assert Sizes({"t": Size("t", 1, 1)}).get(None) is None


class TestSucceedingWithoutSayingAnything:
    """`run_python` is replaced here so the two cases differ by exactly one
    line of output. Nothing in these tests needs a database, which is the
    point: the distinction has to hold even when one is unreachable."""

    @staticmethod
    def answering(stdout: str) -> Sizes | Unknown:
        from djaudit.live import tables as module
        from djaudit.live.runner import Outcome

        target = Target(interpreter(), Path("/p/manage.py"), POSTGRES)
        outcome = Outcome(("python",), 0, stdout, "", 0.1, False)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, "run_python", lambda *a, **k: outcome)
            return read_sizes(target)

    def test_a_command_that_says_nothing_readable_is_unknown(self) -> None:
        """Not `Sizes({})`. An empty mapping reports `available` and then
        answers `None` for every table -- a failure to measure, presented as a
        measurement, and never explained to anyone."""
        result = self.answering("11 objects imported automatically.\n")
        assert not result.available
        assert result.explain() == "table sizes are unknown: the size query returned nothing"

    def test_the_control_is_one_extra_line(self) -> None:
        result = self.answering("11 objects imported automatically.\n" + MARKER + '[["t", 5, 81]]')
        assert result.available
        assert result.get("t") == Size("t", 81, 5)


@postgres
class TestAgainstARealDatabase:
    """The query itself, run by the target's own interpreter."""

    @staticmethod
    def target(project: Path) -> Target:
        return Target(
            interpreter(project / ".venv" / "bin" / "python"),
            project / "manage.py",
            POSTGRES,
        )

    @staticmethod
    def sql(project: Path, statement: str) -> None:
        dsn = (project / "dsn.txt").read_text().strip()
        done = subprocess.run(
            ["psql", dsn, "-qc", statement],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert done.returncode == 0, done.stderr

    def test_it_reads_the_real_catalogue(self, live_project: Path) -> None:
        self.sql(live_project, "CREATE TABLE sized (id int)")
        result = read_sizes(self.target(live_project))
        assert result.available, result.explain()
        assert result.get("sized") is not None

    def test_a_fresh_table_reports_an_unknown_row_count(self, live_project: Path) -> None:
        """The `-1` convention, from the server rather than from a fixture.

        Autovacuum is disabled on this table because it would otherwise analyse
        it at a moment nobody chose, and the test would pass or fail depending
        on how busy the machine was. It first failed exactly that way: alone it
        was quick enough, in the full suite autovacuum got there first.
        """
        self.sql(live_project, "CREATE TABLE fresh (id int) WITH (autovacuum_enabled = false)")
        self.sql(live_project, "INSERT INTO fresh SELECT g FROM generate_series(1,500) g")
        size = read_sizes(self.target(live_project)).get("fresh")
        assert size is not None
        assert size.rows is None
        assert size.stored > 0

    def test_and_analysing_it_makes_the_count_known(self, live_project: Path) -> None:
        """The control for the test above: the same table, same query, after
        the one operation that changes the answer."""
        self.sql(live_project, "CREATE TABLE fresh (id int) WITH (autovacuum_enabled = false)")
        self.sql(live_project, "INSERT INTO fresh SELECT g FROM generate_series(1,500) g")
        self.sql(live_project, "ANALYZE fresh")
        size = read_sizes(self.target(live_project)).get("fresh")
        assert size is not None
        assert size.rows == 500

    def test_the_size_tracks_the_data(self, live_project: Path) -> None:
        """Bytes are exact from the first row, which is the whole reason they
        are the signal rather than `reltuples`."""
        self.sql(
            live_project,
            "CREATE TABLE grows (id int, body text) WITH (autovacuum_enabled = false)",
        )
        self.sql(
            live_project,
            "INSERT INTO grows SELECT g, repeat('x', 500) FROM generate_series(1,20000) g",
        )
        size = read_sizes(self.target(live_project)).get("grows")
        assert size is not None
        assert size.stored > 10 * MEGABYTE
        assert size.rows is None

    def test_a_database_that_cannot_be_reached_is_unknown(self, live_project: Path) -> None:
        """Not an exception, and not an empty answer that reads as `no tables`."""
        target = Target(
            interpreter(live_project / ".venv" / "bin" / "python"),
            live_project / "manage.py",
            POSTGRES,
        )
        result = read_sizes(target, alias="warehouse")
        assert not result.available
        assert "warehouse" in result.explain()

    def test_the_script_is_valid_python(self) -> None:
        """It is built by formatting a template, so a bad edit produces a
        syntax error in the target rather than here."""
        import ast

        ast.parse(SCRIPT.format(alias="default"))
