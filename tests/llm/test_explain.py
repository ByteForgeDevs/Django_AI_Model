"""Tests for `djaudit explain`.

The test that carries the weight here is the one that forbids disk access.
Everything else is presentation; that one is the reason a live SECRET_KEY does
not reach a terminal, a CI log, or a prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.llm.explain import (
    MINIMUM_PREFIX,
    RELATED_SHOWN,
    Explanation,
    FingerprintError,
    explain,
    find,
    render,
)
from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)


def make_finding(
    *,
    rule_id: str = "DJS-001",
    file: str = "conf/settings.py",
    line: int = 12,
    snippet: str = "DEBUG = True",
    fingerprint: str = "a1b2c3d4e5f60718",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        title="Debug is on",
        family=Family.DJS,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        location=Location(file=file, line=line, snippet=snippet),
        message="DEBUG is True in a module that looks deployable.",
        rationale="Debug pages disclose settings and stack traces.",
        remediation="Set DEBUG = False and drive it from the environment.",
        evidence=(Evidence(kind=EvidenceKind.AST, content="DEBUG = True", source=file),),
        references=("https://example.invalid/debug",),
        fingerprint=fingerprint,
    )


class TestFindingByFingerprint:
    def test_a_full_fingerprint_finds_it(self) -> None:
        wanted = make_finding(fingerprint="a1b2c3d4e5f60718")
        others = [make_finding(fingerprint="ffffffffffffffff")]

        assert find([*others, wanted], "a1b2c3d4e5f60718") is wanted

    def test_a_prefix_finds_it(self) -> None:
        wanted = make_finding(fingerprint="a1b2c3d4e5f60718")

        assert find([wanted], "a1b2c3") is wanted

    def test_case_does_not_matter(self) -> None:
        wanted = make_finding(fingerprint="a1b2c3d4e5f60718")

        assert find([wanted], "A1B2C3") is wanted

    def test_an_ambiguous_prefix_is_refused_rather_than_guessed(self) -> None:
        """Explaining the wrong finding under another's id looks entirely right.

        A first match would render a complete, well-formatted, confident
        explanation of something the reader did not ask about, with no signal
        that anything went wrong. Refusing is the only safe answer.
        """
        findings = [
            make_finding(fingerprint="a1b2c3d4e5f60718"),
            make_finding(fingerprint="a1b2c3ffffffffff"),
        ]

        with pytest.raises(FingerprintError, match="matches 2 findings"):
            find(findings, "a1b2c3")

    def test_a_prefix_too_short_to_mean_anything_is_refused(self) -> None:
        with pytest.raises(FingerprintError, match="too short"):
            find([make_finding()], "a" * (MINIMUM_PREFIX - 1))

    def test_the_floor_is_the_number_it_says_it_is(self) -> None:
        """The test above builds its input from the constant, so it cannot see
        a wrong one: lowering 6 to 5 survived it. Pinned against a literal."""
        assert MINIMUM_PREFIX == 6
        finding = make_finding(fingerprint="abcdef0123456789")

        assert find([finding], "abcdef") is finding
        with pytest.raises(FingerprintError, match="too short"):
            find([finding], "abcde")

    def test_a_match_must_start_the_fingerprint(self) -> None:
        """A prefix, not a search.

        Matching anywhere in the string would make `def012` resolve to this
        finding, which reads like a fingerprint and is not one. Nothing else
        here distinguishes the two, so a substring match survived the round.
        """
        finding = make_finding(fingerprint="abcdef0123456789")

        with pytest.raises(FingerprintError, match="may have been fixed"):
            find([finding], "def012")

    def test_an_unknown_fingerprint_says_it_may_have_been_fixed(self) -> None:
        with pytest.raises(FingerprintError, match="may have been fixed"):
            find([make_finding()], "0123456789abcdef")

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        wanted = make_finding(fingerprint="a1b2c3d4e5f60718")

        assert find([wanted], "  a1b2c3d4e5f60718\n") is wanted


class TestNeighbours:
    def test_the_same_rule_in_the_same_file_is_the_same_cause(self) -> None:
        target = make_finding(line=10, fingerprint="0000000000000001")
        sibling = make_finding(line=40, fingerprint="0000000000000002")

        result = explain(target, [target, sibling])

        assert result.same_cause == (sibling,)
        assert result.same_file == ()

    def test_another_rule_in_the_same_file_is_a_neighbour(self) -> None:
        target = make_finding(rule_id="DJS-001", fingerprint="0000000000000001")
        other = make_finding(rule_id="DJS-002", fingerprint="0000000000000002")

        result = explain(target, [target, other])

        assert result.same_cause == ()
        assert result.same_file == (other,)

    def test_another_file_is_neither(self) -> None:
        """Both halves, because each ignores a different field.

        Two findings of the *same* rule elsewhere only exercise `same_cause`'s
        file check; dropping the file check from `same_file` survived that.
        A different rule in a different file is what reaches it.
        """
        target = make_finding(rule_id="DJS-001", file="a.py", fingerprint="0000000000000001")
        same_rule = make_finding(rule_id="DJS-001", file="b.py", fingerprint="0000000000000002")
        other_rule = make_finding(rule_id="DJS-002", file="c.py", fingerprint="0000000000000003")

        result = explain(target, [target, same_rule, other_rule])

        assert result.same_cause == ()
        assert result.same_file == ()

    def test_the_finding_is_not_its_own_neighbour(self) -> None:
        target = make_finding()

        result = explain(target, [target])

        assert result.same_cause == ()
        assert result.same_file == ()

    def test_it_works_with_no_context_at_all(self) -> None:
        result = explain(make_finding())

        assert result.same_cause == ()
        assert result.same_file == ()


class TestRendering:
    def test_it_says_what_where_why_and_what_to_do(self) -> None:
        text = render(explain(make_finding()))

        assert "DJS-001 Debug is on" in text
        assert "conf/settings.py:12" in text
        assert "DEBUG is True in a module that looks deployable." in text
        assert "Debug pages disclose settings and stack traces." in text
        assert "Set DEBUG = False" in text
        assert "https://example.invalid/debug" in text

    def test_evidence_is_labelled_in_words_not_by_enum_name(self) -> None:
        text = render(explain(make_finding()))

        assert "the code this was read from" in text

    def test_a_finding_with_no_snippet_still_renders(self) -> None:
        text = render(explain(make_finding(snippet="")))

        assert "The code" not in text
        assert "What to do" in text

    def test_related_findings_are_capped_and_counted(self) -> None:
        target = make_finding(line=1, fingerprint="0000000000000000")
        siblings = [make_finding(line=n, fingerprint=f"{n:016x}") for n in range(1, 12)]

        text = render(explain(target, [target, *siblings]))

        assert "11 more" in text
        assert text.count("  line ") == RELATED_SHOWN

    def test_it_counts_the_neighbours_it_does_not_list(self) -> None:
        target = make_finding(rule_id="DJS-001", fingerprint="0000000000000000")
        others = [
            make_finding(rule_id=f"DJS-{n:03}", line=n, fingerprint=f"{n:016x}")
            for n in range(2, 12)
        ]

        text = render(explain(target, [target, *others]))

        assert "Also in this file (10 findings)" in text


class TestItNeverReadsTheTargetsSource:
    """The security property, tested by making a read impossible.

    A settings rule masks the value it reports, so the finding for a live key
    carries `SECRET_KEY = "*x<redacted:50 chars>"`. An explanation that re-read
    the line from disk "for context" would print the real key to the terminal,
    into CI logs, and into a prompt. Asserting the key is absent from the output
    is not enough -- that passes for a version that reads the file and happens
    not to print it. So reading is made to raise.
    """

    def test_explaining_a_masked_finding_touches_no_file(
        self, monkeypatch, vulnerable_project: Path
    ) -> None:
        result = engine.run(vulnerable_project, min_confidence=Confidence.TENTATIVE)
        target = next(f for f in result.findings if "<redacted:" in (f.location.snippet or ""))

        def refuse(*args: object, **kwargs: object) -> str:
            raise AssertionError("explain read a file")

        monkeypatch.setattr(Path, "read_text", refuse)
        monkeypatch.setattr(Path, "read_bytes", refuse)
        monkeypatch.setattr(Path, "open", refuse)

        text = render(explain(target, result.findings))

        assert "<redacted:" in text
        assert target.rule_id in text

    def test_the_real_secret_never_appears(self, vulnerable_project: Path) -> None:
        """The contrast. The guard above is only meaningful if a secret exists."""
        source = (vulnerable_project / "config" / "settings" / "base.py").read_text()
        secret = next(
            line.split("=", 1)[1].strip().strip("\"'")
            for line in source.splitlines()
            if line.startswith("SECRET_KEY")
        )
        assert len(secret) > 20, "the fixture has no real-looking secret; this proves nothing"

        result = engine.run(vulnerable_project, min_confidence=Confidence.TENTATIVE)
        everything = "\n".join(render(explain(f, result.findings)) for f in result.findings)

        assert secret not in everything


class TestTheExplanationIsAssembledNotInvented:
    def test_every_sentence_comes_from_the_finding(self) -> None:
        """No model, so nothing in the output may be absent from the input.

        This is what makes `explain` safe to run offline by default: the output
        is a rearrangement of the finding, not a claim about it.
        """
        finding = make_finding()
        text = render(explain(finding))

        for field in (finding.message, finding.rationale, finding.remediation):
            assert field in text

    def test_an_explanation_is_immutable(self) -> None:
        result = explain(make_finding())

        with pytest.raises((AttributeError, TypeError)):
            result.finding = make_finding()  # type: ignore[misc]

    def test_it_is_the_dataclass_it_claims_to_be(self) -> None:
        assert isinstance(explain(make_finding()), Explanation)
