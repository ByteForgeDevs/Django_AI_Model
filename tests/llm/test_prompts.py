"""What must not leave the machine, and what must not be lost on the way.

A redactor is trivial to get right in one direction. Blanking everything passes
every test about secrets and destroys the question; blanking nothing passes
every test about fidelity and posts a credential to a third party. Both
directions are asserted here, and the fidelity side is measured against all 245
findings djaudit really produces rather than against a fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from djaudit import engine
from djaudit.llm.prompts import (
    MAX_EVIDENCE_CHARACTERS,
    TRIAGE_PROMPT_VERSION,
    TRIAGE_SCHEMA,
    build_triage_prompt,
    redact,
    render_finding,
    worth_asking,
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

BENCHMARK_ROOT = Path("~/.cache/djaudit-benchmarks").expanduser()


def make_finding(
    *,
    snippet: str = "SECRET_KEY = 'x'",
    evidence: tuple[Evidence, ...] = (),
    rule_id: str = "DJS-002",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        title="Secret key is a literal",
        family=Family.DJS,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        tier=Tier.STATIC,
        location=Location(file="settings.py", line=14, snippet=snippet),
        message="SECRET_KEY is assigned a string literal.",
        rationale="A committed key signs every session cookie.",
        evidence=evidence,
        fingerprint="abc123",
    )


@pytest.fixture(scope="module")
def real_findings() -> list[Finding]:
    if not BENCHMARK_ROOT.is_dir():
        pytest.skip("benchmark corpora not fetched")
    findings: list[Finding] = []
    for name in ("hc", "nb", "px"):
        findings.extend(engine.run(BENCHMARK_ROOT / name).findings)
    return findings


class TestNoCredentialLeaves:
    """The failure mode this module exists for.

    djaudit's own subject matter is projects that keep secrets in source. A
    prompt builder that forwarded what it found would be the leak the tool
    reports.
    """

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwxyz0123456789",
            "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
            "xoxb-1234567890-abcdefghijklmno",
            "cGFzc3dvcmRwYXNzd29yZHBhc3N3b3JkcGFzc3dvcmQ=",
            "AKIAIOSFODNN7EXAMPLEAKIAIOSFODNN7EXAMPLE",
            "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        ],
    )
    def test_a_secret_in_the_snippet_is_blanked(self, secret: str) -> None:
        rendered = render_finding(make_finding(snippet=f'SECRET_KEY = "{secret}"'))

        assert secret not in rendered

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwxyz0123456789",
            "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ],
    )
    def test_a_secret_in_evidence_is_blanked(self, secret: str) -> None:
        """Evidence is where a rule puts what it read, so it is the more
        likely carrier of the two."""
        finding = make_finding(
            evidence=(Evidence(EvidenceKind.SOURCE, f"line 3: token = {secret}"),)
        )

        assert secret not in render_finding(finding)

    def test_a_private_key_header_is_blanked(self) -> None:
        body = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n"

        assert "-----BEGIN RSA PRIVATE KEY-----" not in redact(body)

    def test_the_whole_prompt_is_checked_not_just_the_snippet(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
        finding = make_finding(snippet=f"KEY = '{secret}'")

        prompt = build_triage_prompt(finding)

        assert secret not in prompt.user
        assert secret not in prompt.system

    def test_the_redaction_keeps_the_shape(self) -> None:
        """A model told only that something was removed cannot tell a 40
        character key from an empty string."""
        redacted = redact("token = sk-abcdefghijklmnopqrstuvwxyz0123456789")

        assert "redacted:39 chars" in redacted
        assert redacted.startswith("token = sk")


class TestNothingElseIsLost:
    """The contrast, without which the tests above are satisfied by ``return ''``."""

    @pytest.mark.parametrize(
        "text",
        [
            "hc/accounts/management/commands/sendinactivitynotices.py",
            "src/pretix/base/migrations/0077_auto_20171124_1629_squashed_0088_auto_20180328_1217.py",
            "0076_orderfee_squashed_0082_invoiceaddress_internal_reference",
            "src/pretix/plugins/ticketoutputpdf/signals.py",
        ],
    )
    def test_the_shapes_that_broke_the_first_version(self, text: str) -> None:
        """Real strings from the corpora, kept as regression cases.

        The first redactor treated any forty-character run of `[A-Za-z0-9_/-]`
        as a credential and fired on 99 of 245 findings -- all of them file
        paths and squashed migration names. Separators divide the two
        populations: every false match held a `/` or a `_`, and none of the
        credential shapes did.
        """
        assert redact(text) == text

    def test_ordinary_code_survives_untouched(self) -> None:
        code = 'DEBUG = True\nfor obj in qs:\n    print(obj.related.name)\nx = "%s" % v'

        assert redact(code) == code

    def test_the_finding_still_reads_as_a_question(self) -> None:
        rendered = render_finding(make_finding())

        assert "DJS-002" in rendered
        assert "settings.py:14" in rendered
        assert "SECRET_KEY is assigned a string literal." in rendered
        assert rendered.rstrip().endswith("Is this a real defect in this codebase?")

    def test_no_redaction_fires_on_any_real_finding(self, real_findings: list[Finding]) -> None:
        """Measured, not assumed.

        This runs against the *rendered prompt*, not against snippets and
        evidence. That distinction is the whole lesson: probing the narrower
        surface returned a comforting zero, and the full text -- which also
        carries the file path and the message -- fired on 99 of 245.
        """
        assert len(real_findings) == 245

        fired = [f for f in real_findings if "<redacted:" in render_finding(f)]

        assert fired == []

    def test_evidence_is_carried_into_the_prompt(self) -> None:
        finding = make_finding(
            evidence=(Evidence(EvidenceKind.CONFIG, "length=50 classes=lower,digit", "resolver"),)
        )

        rendered = render_finding(finding)

        assert "length=50 classes=lower,digit" in rendered
        assert "resolver" in rendered


class TestTheQuestionIsBounded:
    def test_enormous_evidence_is_clipped(self) -> None:
        finding = make_finding(evidence=(Evidence(EvidenceKind.SOURCE, "x" * 50_000),))

        rendered = render_finding(finding)

        assert len(rendered) < MAX_EVIDENCE_CHARACTERS + 2_000

    def test_clipping_says_it_clipped(self) -> None:
        """Silently truncated evidence reads as complete evidence, and a model
        judging half a function without being told will judge it confidently."""
        finding = make_finding(evidence=(Evidence(EvidenceKind.SOURCE, "x" * 50_000),))

        assert "truncated" in render_finding(finding)

    def test_short_evidence_is_not_clipped(self) -> None:
        finding = make_finding(evidence=(Evidence(EvidenceKind.SOURCE, "def f(): pass"),))

        assert "truncated" not in render_finding(finding)


class TestTheSameFindingRendersTheSameWay:
    """The prompt is a cache key. Drift makes the cache silently useless."""

    def test_rendering_twice_is_byte_identical(self) -> None:
        finding = make_finding(
            evidence=(
                Evidence(EvidenceKind.SOURCE, "a"),
                Evidence(EvidenceKind.CONFIG, "b"),
            )
        )

        assert render_finding(finding) == render_finding(finding)

    def test_two_equal_findings_render_the_same(self) -> None:
        assert render_finding(make_finding()) == render_finding(make_finding())

    def test_a_different_finding_renders_differently(self) -> None:
        assert render_finding(make_finding()) != render_finding(make_finding(rule_id="DJP-004"))

    def test_the_prompt_is_versioned(self) -> None:
        assert build_triage_prompt(make_finding()).version == TRIAGE_PROMPT_VERSION


class TestTheAnswerIsClosed:
    def test_the_verdict_field_admits_exactly_three_words(self) -> None:
        verdict = next(f for f in TRIAGE_SCHEMA.fields if f.name == "verdict")

        assert verdict.choices == ("true_positive", "accepted_risk", "unsure")

    def test_a_fourth_word_is_refused(self) -> None:
        from djaudit.llm.provider import SchemaViolationError

        with pytest.raises(SchemaViolationError):
            TRIAGE_SCHEMA.validate({"verdict": "probably", "reason": "hedging"})

    def test_an_undeclared_field_is_refused(self) -> None:
        from djaudit.llm.provider import SchemaViolationError

        with pytest.raises(SchemaViolationError):
            TRIAGE_SCHEMA.validate(
                {"verdict": "true_positive", "reason": "ok", "severity": "critical"}
            )

    def test_unsure_is_offered_so_guessing_is_not_required(self) -> None:
        """A binary forced choice turns "I cannot tell" into a coin flip, and a
        coin flip on this corpus lands on accepted_risk one time in two."""
        assert TRIAGE_SCHEMA.validate({"verdict": "unsure", "reason": "excerpt too short"})


class TestNoReviewersNoteCanReachAModel:
    def test_a_finding_has_no_note_to_leak(self) -> None:
        """Structural. The verdicts in benchmarks/ carry prose explaining the
        judgement; a prompt built from one would be a model reading the answer.
        The builder takes a Finding, which has no such field."""
        assert not hasattr(make_finding(), "note")

    def test_the_rendered_prompt_holds_only_engine_output(self) -> None:
        rendered = render_finding(make_finding())

        assert "reviewer" not in rendered.lower()
        assert "verdict" not in rendered.lower()


class TestNotEveryFindingIsWorthACall:
    """From 6.1.5: 22 of 29 rules were judged identically every time they fired."""

    def test_a_contested_rule_is_asked_about(self) -> None:
        assert worth_asking(make_finding(rule_id="DJP-004"), frozenset({"DJP-004"}))

    def test_a_unanimous_rule_is_not(self) -> None:
        assert not worth_asking(make_finding(rule_id="DJS-002"), frozenset({"DJP-004"}))

    def test_an_empty_contested_set_asks_nothing(self) -> None:
        assert not worth_asking(make_finding(), frozenset())

    def test_it_saves_most_of_the_corpus(self, real_findings: list[Finding]) -> None:
        """The measurement that justifies the mechanism, run on real findings.

        Seven contested rules cover 110 of 245 findings, so filtering removes
        just over half the calls -- and 81 of those 110 are DJP-004 at 81:2,
        which a later step can narrow further.
        """
        contested = frozenset(
            {"DJA-011", "DJA-014", "DJA-015", "DJD-002", "DJP-004", "DJS-009", "DJS-010"}
        )

        asked = [f for f in real_findings if worth_asking(f, contested)]

        assert len(asked) == 110
        assert len(real_findings) - len(asked) == 135
