"""Tests for theme grouping.

The property that matters is not compression. It is that a theme is one
decision -- so the tests that carry weight here are the ones about a group
whose members were judged *differently*, and the one that re-derives the
grouping key's purity from the recorded corpus rather than trusting the table
in the module docstring.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from djaudit.llm.evaluate import Verdict
from djaudit.llm.group import Theme, collapsed, group
from djaudit.llm.triage import Judgement, Source
from djaudit.models import Confidence, Family, Finding, Location, Severity, Tier

BENCHMARKS = ("healthchecks", "netbox", "pretix")


def make_judgement(  # noqa: PLR0913 - a builder; every field is set by some test
    *,
    rule_id: str = "DJP-001",
    file: str = "app/views.py",
    line: int = 10,
    severity: Severity = Severity.HIGH,
    verdict: Verdict = Verdict.TRUE_POSITIVE,
    source: Source = Source.MODEL,
    title: str = "Query in a loop",
) -> Judgement:
    finding = Finding(
        rule_id=rule_id,
        title=title,
        family=Family.DJP,
        severity=severity,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        location=Location(file=file, line=line),
        message="Something is wrong here.",
        rationale="Because it is.",
        remediation="Fix it.",
    )
    return Judgement(finding, verdict, source, "because")


class TestGrouping:
    def test_the_same_rule_in_the_same_file_is_one_theme(self) -> None:
        themes = group(
            [
                make_judgement(line=10),
                make_judgement(line=40),
                make_judgement(line=61),
            ]
        )

        assert len(themes) == 1
        assert themes[0].count == 3
        assert themes[0].lines == (10, 40, 61)

    def test_the_same_rule_in_another_file_is_another_theme(self) -> None:
        """The measured boundary. Directory grouping merged these and was wrong."""
        themes = group(
            [
                make_judgement(file="app/views.py"),
                make_judgement(file="app/models.py"),
            ]
        )

        assert len(themes) == 2

    def test_another_rule_in_the_same_file_is_another_theme(self) -> None:
        themes = group(
            [
                make_judgement(rule_id="DJP-001"),
                make_judgement(rule_id="DJP-007"),
            ]
        )

        assert len(themes) == 2

    def test_nothing_is_lost_and_nothing_is_invented(self) -> None:
        """The invariant the whole llm package rests on.

        This layer reorders and summarises. If grouping could drop a finding it
        would be editing the finding set, which nothing under `llm` may do.
        """
        judgements = [
            make_judgement(rule_id="DJP-001", file="a.py", line=1),
            make_judgement(rule_id="DJP-001", file="a.py", line=2),
            make_judgement(rule_id="DJP-007", file="a.py", line=3),
            make_judgement(rule_id="DJP-001", file="b.py", line=4),
        ]

        themes = group(judgements)

        regrouped = [j for theme in themes for j in theme.judgements]
        assert sorted(id(j) for j in regrouped) == sorted(id(j) for j in judgements)

    def test_an_empty_run_groups_to_nothing(self) -> None:
        assert group([]) == []

    def test_collapsed_counts_what_was_saved(self) -> None:
        themes = group([make_judgement(line=n) for n in range(9)])
        assert collapsed(themes) == 8

    def test_collapsed_is_zero_when_nothing_grouped(self) -> None:
        themes = group([make_judgement(file=f"{n}.py") for n in range(4)])
        assert collapsed(themes) == 0


class TestATheme:
    def test_it_takes_the_worst_severity_in_the_group(self) -> None:
        theme = group(
            [
                make_judgement(line=1, severity=Severity.LOW),
                make_judgement(line=2, severity=Severity.CRITICAL),
                make_judgement(line=3, severity=Severity.MEDIUM),
            ]
        )[0]

        assert theme.severity is Severity.CRITICAL

    def test_an_agreed_verdict_is_reported(self) -> None:
        theme = group(
            [
                make_judgement(line=1, verdict=Verdict.ACCEPTED_RISK),
                make_judgement(line=2, verdict=Verdict.ACCEPTED_RISK),
            ]
        )[0]

        assert theme.verdict is Verdict.ACCEPTED_RISK

    def test_a_disagreement_is_not_resolved_by_majority(self) -> None:
        """The failure that ruled out the more compressive grouping key.

        Two findings judged acceptable and one judged a real defect is not a
        theme that is "mostly fine". Showing the majority verdict would let a
        group silently overrule the finding somebody flagged.
        """
        theme = group(
            [
                make_judgement(line=1, verdict=Verdict.ACCEPTED_RISK),
                make_judgement(line=2, verdict=Verdict.ACCEPTED_RISK),
                make_judgement(line=3, verdict=Verdict.TRUE_POSITIVE),
            ]
        )[0]

        assert theme.verdict is None

    def test_a_mixed_source_is_not_resolved_either(self) -> None:
        theme = group(
            [
                make_judgement(line=1, source=Source.CORPUS),
                make_judgement(line=2, source=Source.MODEL),
            ]
        )[0]

        assert theme.source is None

    def test_an_empty_theme_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no findings"):
            Theme(rule_id="DJP-001", file="a.py", judgements=())

    def test_one_place_is_named_precisely(self) -> None:
        assert group([make_judgement(line=12)])[0].where() == "app/views.py:12"

    def test_a_few_places_are_all_listed(self) -> None:
        theme = group([make_judgement(line=n) for n in (61, 10, 40)])[0]
        assert theme.where() == "app/views.py:10, 40, 61"

    def test_many_places_are_counted_rather_than_listed(self) -> None:
        theme = group([make_judgement(line=n) for n in range(9)])[0]
        assert theme.where() == "app/views.py (9 places)"


class TestOrdering:
    def test_real_defects_come_before_accepted_risks(self) -> None:
        themes = group(
            [
                make_judgement(file="a.py", verdict=Verdict.ACCEPTED_RISK),
                make_judgement(file="b.py", verdict=Verdict.TRUE_POSITIVE),
            ]
        )

        assert [t.file for t in themes] == ["b.py", "a.py"]

    def test_severity_breaks_a_tie(self) -> None:
        themes = group(
            [
                make_judgement(file="a.py", severity=Severity.LOW),
                make_judgement(file="b.py", severity=Severity.CRITICAL),
            ]
        )

        assert [t.file for t in themes] == ["b.py", "a.py"]

    def test_the_bigger_group_comes_first_when_all_else_is_equal(self) -> None:
        themes = group(
            [
                make_judgement(file="a.py", line=1),
                make_judgement(file="b.py", line=1),
                make_judgement(file="b.py", line=2),
            ]
        )

        assert [t.file for t in themes] == ["b.py", "a.py"]

    def test_the_order_is_stable_across_runs(self) -> None:
        """Output that is diffed must not reorder for no reason."""
        judgements = [make_judgement(file=f"{n}.py") for n in range(6)]
        first = [t.file for t in group(judgements)]
        assert [t.file for t in group(reversed(judgements))] == first


class TestTheKeyIsStillTheRightOne:
    """Re-derives the choice from the corpus instead of trusting the docstring.

    6.2.2 shipped a table that a later measurement contradicted, so the table in
    `group.py` is checked rather than asserted. If a future rule change makes
    `rule + file` mix verdicts, this fails and the key has to be re-measured.
    """

    @staticmethod
    def verdicts() -> list[tuple[str, str, str, str]]:
        rows = []
        for name in BENCHMARKS:
            path = Path("benchmarks") / f"{name}.json"
            for finding in json.loads(path.read_text())["findings"]:
                rows.append((name, finding["rule_id"], finding["file"], finding["verdict"]))
        return rows

    def test_the_corpus_is_actually_loaded(self) -> None:
        """Lesson: a purity check over zero rows is 100% pure."""
        rows = self.verdicts()

        assert len(rows) >= 200, "the recorded corpus did not load; the check below is vacuous"
        assert len({r[3] for r in rows}) > 1, "one verdict everywhere makes any key look pure"

    def test_rule_and_file_never_merges_a_disagreement(self) -> None:
        groups: dict[tuple[str, str, str], set[str]] = collections.defaultdict(set)
        for benchmark, rule_id, file, verdict in self.verdicts():
            groups[(benchmark, rule_id, file)].add(verdict)

        mixed = {key for key, verdicts in groups.items() if len(verdicts) > 1}

        assert mixed == set(), f"rule+file now merges differently-judged findings: {mixed}"

    def test_it_still_groups_enough_to_be_worth_doing(self) -> None:
        rows = self.verdicts()
        keys = {(benchmark, rule_id, file) for benchmark, rule_id, file, _ in rows}

        assert len(keys) < len(rows) * 0.75, "grouping no longer removes a quarter of the rows"

    def test_the_directory_key_is_still_the_wrong_one(self) -> None:
        """Assert the contrast, not just the absence.

        `rule + file` being pure means nothing on its own -- a key that grouped
        nothing would also be pure. This pins the reason the compressive
        alternative was rejected, so if it ever stops being true the choice can
        be revisited on evidence.
        """
        groups: dict[tuple[str, str, str], set[str]] = collections.defaultdict(set)
        for benchmark, rule_id, file, verdict in self.verdicts():
            groups[(benchmark, rule_id, str(Path(file).parent))].add(verdict)

        mixed = [key for key, verdicts in groups.items() if len(verdicts) > 1]

        assert mixed, "directory grouping no longer merges disagreements; re-measure the key"
