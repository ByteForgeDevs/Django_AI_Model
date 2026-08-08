"""Whether a proposed suppression is honest, and whether it works.

Two failures are possible here and they point in opposite directions.

A proposal that does not suppress is the cheap one: the reviewer commits it,
the finding keeps firing, and they conclude the syntax is broken. That is
caught by round-tripping every proposal through the parser that will read it,
and by applying one to real source and re-running the engine.

A proposal that suppresses too easily is the expensive one, because it is a
real defect that a tool talked someone into hiding. That is what the
`Source.MODEL` requirement is for, and it is asserted from both sides: the
model verdict is accepted, and the borrowed one is refused with a reason.
"""

from __future__ import annotations

import importlib
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from djaudit import engine
from djaudit.llm.evaluate import Verdict
from djaudit.llm.suggest import (
    MAXIMUM_LINE,
    MINIMUM_JUSTIFICATION,
    REDACTION_MARKER,
    Proposal,
    SuppressionError,
    comment_for,
    propose,
    render,
    source_line_of,
    suggest,
)
from djaudit.llm.triage import Judgement, Source, TriageRun
from djaudit.models import (
    Confidence,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)
from djaudit.suppression import line_suppresses

REASON = "the endpoint is behind an authenticated gateway"
LINE = "    DEBUG = True"


def offer(judgement: Judgement, source_line: str = LINE) -> Proposal:
    """propose() against a real source line, which is the only kind there is."""
    return propose(judgement, source_line)


def make_judgement(  # noqa: PLR0913 - a builder; every field is set by some test
    *,
    verdict: Verdict = Verdict.ACCEPTED_RISK,
    source: Source = Source.MODEL,
    reason: str = REASON,
    snippet: str = "    DEBUG = True",
    rule_id: str = "DJS-001",
    fingerprint: str = "f0",
) -> Judgement:
    finding = Finding(
        rule_id=rule_id,
        title="A finding",
        family=Family.DJS,
        severity=Severity.HIGH,
        confidence=Confidence.FIRM,
        tier=Tier.STATIC,
        location=Location(file="conf/settings.py", line=12, snippet=snippet),
        message="Something is wrong here.",
        rationale="Because it is.",
        fingerprint=fingerprint,
    )
    return Judgement(finding=finding, verdict=verdict, source=source, reason=reason)


class TestWhatMayJustifyASuppression:
    def test_a_model_verdict_may(self) -> None:
        proposal = offer(make_judgement())
        assert proposal.rule_id == "DJS-001"
        assert REASON in proposal.line

    def test_a_borrowed_verdict_may_not(self) -> None:
        """The dangerous one.

        Ranking a finding low because other codebases waved the rule through is
        reasonable. Writing a permanent comment into this codebase on the same
        grounds is not, and the difference is that ranking is reversible.
        """
        with pytest.raises(SuppressionError, match="corpus verdict cannot justify"):
            offer(make_judgement(source=Source.CORPUS))

    def test_an_unanswered_call_may_not(self) -> None:
        with pytest.raises(SuppressionError, match="unavailable verdict cannot justify"):
            offer(make_judgement(source=Source.UNAVAILABLE, verdict=Verdict.ACCEPTED_RISK))

    def test_a_real_defect_may_not(self) -> None:
        with pytest.raises(SuppressionError, match="only an accepted risk"):
            offer(make_judgement(verdict=Verdict.TRUE_POSITIVE))

    def test_an_abstention_may_not(self) -> None:
        with pytest.raises(SuppressionError, match="only an accepted risk"):
            offer(make_judgement(verdict=Verdict.ABSTAINED))


class TestTheJustification:
    def test_it_is_required(self) -> None:
        with pytest.raises(SuppressionError, match="no justification"):
            offer(make_judgement(reason=""))

    def test_a_token_one_is_not_enough(self) -> None:
        with pytest.raises(SuppressionError, match="no justification"):
            offer(make_judgement(reason="ok"))

    def test_it_reaches_the_comment_verbatim(self) -> None:
        proposal = offer(make_judgement(reason="deliberate, tracked in PROJ-412"))
        assert "deliberate, tracked in PROJ-412" in proposal.line

    def test_a_multiline_reason_is_flattened(self) -> None:
        """A newline in a comment would end it and comment out nothing."""
        proposal = offer(make_judgement(reason="first line\nsecond line here"))
        assert "\n" not in proposal.line
        assert "first line second line here" in proposal.line

    def test_the_floor_is_the_one_that_is_documented(self) -> None:
        just_under = "x" * (MINIMUM_JUSTIFICATION - 1)
        with pytest.raises(SuppressionError):
            offer(make_judgement(reason=just_under))
        assert offer(make_judgement(reason="x" * MINIMUM_JUSTIFICATION))


class TestTheGeneratedComment:
    def test_it_names_the_rule_rather_than_silencing_the_line(self) -> None:
        """A bare `# djaudit: ignore` is honoured and hides every rule there.

        Nobody who has judged one finding means that, so the generated form is
        always scoped -- and this asserts the contrast: the scoped comment
        suppresses its own rule and leaves a different one reporting.
        """
        line = "    DEBUG = True" + comment_for("DJS-001", REASON)
        assert line_suppresses(line, "DJS-001")
        assert not line_suppresses(line, "DJS-002")

    def test_the_parser_that_will_read_it_accepts_it(self) -> None:
        proposal = offer(make_judgement())
        assert line_suppresses(proposal.line, "DJS-001")

    def test_the_original_code_survives_on_the_line(self) -> None:
        proposal = offer(make_judgement(), "    DEBUG = True")
        assert proposal.line.startswith("    DEBUG = True")

    def test_indentation_is_preserved(self) -> None:
        proposal = offer(make_judgement(), "        DEBUG = True")
        assert proposal.line.startswith("        DEBUG = True")

    def test_a_line_already_suppressed_is_left_alone(self) -> None:
        with pytest.raises(SuppressionError, match="already suppressed"):
            offer(make_judgement(), "DEBUG = True  # djaudit: ignore[DJS-001] known")

    def test_a_line_that_would_grow_too_long_is_refused(self) -> None:
        with pytest.raises(SuppressionError, match="baseline instead"):
            offer(make_judgement(), " " * MAXIMUM_LINE + "DEBUG = True")

    def test_the_cap_is_the_number_it_says_it_is(self) -> None:
        """Pinned, because the test above cannot see a wrong cap.

        It builds its input from `MAXIMUM_LINE`, so raising the constant raises
        the input with it and the refusal still fires. Mutating 120 to 100000
        survived that test. The boundary has to be checked against a literal.
        """
        assert MAXIMUM_LINE == 120

        reason = "deliberate here"
        comment = comment_for("DJS-001", reason)
        judgement = make_judgement(reason=reason)

        exact = "x" * (120 - len(comment))
        assert len(offer(judgement, exact).line) == 120

        with pytest.raises(SuppressionError, match="121-character"):
            offer(judgement, exact + "x")

    def test_the_justification_floor_is_the_number_it_says_it_is(self) -> None:
        """Same trap on the other constant: 12 -> 11 survived without this."""
        assert MINIMUM_JUSTIFICATION == 12

        with pytest.raises(SuppressionError, match="no justification"):
            offer(make_judgement(reason="x" * 11))
        assert offer(make_judgement(reason="y" * 12)).justification == "y" * 12

    def test_a_comment_djaudit_would_not_honour_is_refused(self, monkeypatch) -> None:
        """The guard that no correct code path can reach.

        `propose` round-trips its own comment through the real parser before
        offering it. With `comment_for` correct that check never fires, so
        deleting it survives every other test here. It earns its place only
        against a future change to `comment_for` -- so that is what this
        simulates: a bare `# noqa`, which djaudit deliberately does not honour.
        """
        # Not `from djaudit.llm import suggest`: that name is the *function*,
        # because the package re-exports it over its own submodule. See
        # TestTheSubmoduleIsShadowed below.
        module = importlib.import_module("djaudit.llm.suggest")
        monkeypatch.setattr(module, "comment_for", lambda rule_id, reason: "  # noqa")

        with pytest.raises(SuppressionError, match="does not honour"):
            offer(make_judgement())

    def test_a_finding_with_no_source_line_is_refused(self) -> None:
        with pytest.raises(SuppressionError, match="no source line"):
            offer(make_judgement(), "   ")


class TestSuggest:
    """`suggest` reads the real lines, so these build a tiny project on disk."""

    @staticmethod
    def project(tmp_path: Path, line: str = "    DEBUG = True") -> Path:
        root = tmp_path / "proj"
        (root / "conf").mkdir(parents=True)
        # The judgements below sit on line 12, so the file has to reach it.
        body = [f"# filler {n}" for n in range(11)] + [line, "# after"]
        (root / "conf" / "settings.py").write_text("\n".join(body) + "\n")
        return root

    def test_it_offers_only_the_accepted_risks(self, tmp_path: Path) -> None:
        run = TriageRun(
            judgements=[
                make_judgement(verdict=Verdict.TRUE_POSITIVE, fingerprint="a"),
                make_judgement(fingerprint="b"),
                make_judgement(verdict=Verdict.ABSTAINED, fingerprint="c"),
            ]
        )
        proposals, _ = suggest(run, self.project(tmp_path))

        assert [p.judgement.finding.fingerprint for p in proposals] == ["b"]

    def test_it_reports_what_it_would_not_offer(self, tmp_path: Path) -> None:
        """Silence about a refusal reads as "there was nothing to refuse"."""
        run = TriageRun(judgements=[make_judgement(source=Source.CORPUS)])
        proposals, refused = suggest(run, self.project(tmp_path))

        assert proposals == []
        assert len(refused) == 1
        assert "corpus" in refused[0]

    def test_a_true_positive_is_not_reported_as_a_refusal(self, tmp_path: Path) -> None:
        """It was never a candidate; listing it would bury the real refusals."""
        run = TriageRun(judgements=[make_judgement(verdict=Verdict.TRUE_POSITIVE)])
        assert suggest(run, self.project(tmp_path)) == ([], [])

    def test_an_offline_run_proposes_nothing(self, tmp_path: Path) -> None:
        run = TriageRun(
            judgements=[
                make_judgement(source=Source.UNAVAILABLE, verdict=Verdict.ABSTAINED),
                make_judgement(source=Source.CORPUS),
            ]
        )
        proposals, _ = suggest(run, self.project(tmp_path))
        assert proposals == []

    def test_a_line_that_moved_since_the_run_is_refused(self, tmp_path: Path) -> None:
        """A patch against a line that is no longer there belongs nowhere."""
        root = tmp_path / "proj"
        (root / "conf").mkdir(parents=True)
        (root / "conf" / "settings.py").write_text("# only one line\n")

        proposals, refused = suggest(TriageRun(judgements=[make_judgement()]), root)

        assert proposals == []
        assert "no line 12" in refused[0]

    def test_a_file_that_is_gone_is_refused(self, tmp_path: Path) -> None:
        proposals, refused = suggest(TriageRun(judgements=[make_judgement()]), tmp_path / "nope")

        assert proposals == []
        assert "cannot read" in refused[0]


class TestTheDiff:
    def test_it_names_the_file_on_both_sides(self) -> None:
        diff = offer(make_judgement()).diff()
        assert "a/conf/settings.py" in diff
        assert "b/conf/settings.py" in diff

    def test_it_shows_the_line_arriving_and_the_old_one_leaving(self) -> None:
        diff = offer(make_judgement()).diff()
        assert "-    DEBUG = True" in diff
        assert "+    DEBUG = True  # djaudit: ignore[DJS-001]" in diff

    def test_rendering_nothing_produces_nothing(self) -> None:
        assert render([]) == ""

    def test_rendering_keeps_every_proposal(self) -> None:
        proposals = [
            offer(make_judgement(fingerprint="a", rule_id="DJS-001")),
            offer(make_judgement(fingerprint="b", rule_id="DJS-003")),
        ]
        text = render(proposals)
        assert "DJS-001" in text
        assert "DJS-003" in text


class TestItNeverApplies:
    def test_generating_a_proposal_does_not_touch_the_file(
        self, tmp_path: Path, vulnerable_project: Path
    ) -> None:
        project = tmp_path / "proj"
        shutil.copytree(vulnerable_project, project)
        before = {
            path: path.read_bytes() for path in sorted(project.rglob("*.py")) if path.is_file()
        }

        result = engine.run(project, min_confidence=Confidence.TENTATIVE)
        run = TriageRun(
            judgements=[
                Judgement(finding, Verdict.ACCEPTED_RISK, Source.MODEL, REASON)
                for finding in result.findings
            ]
        )
        proposals, _ = suggest(run, project)

        assert proposals, "nothing was proposed, so this proves nothing"
        after = {
            path: path.read_bytes() for path in sorted(project.rglob("*.py")) if path.is_file()
        }
        assert before == after

    def test_the_module_cannot_write(self) -> None:
        """Structural, not behavioural.

        A test that files were unchanged only covers the paths it exercised.
        This covers the ones it did not: there is no write in the source.
        """
        source = Path(__file__).resolve().parents[2] / "src" / "djaudit" / "llm" / "suggest.py"
        text = source.read_text(encoding="utf-8")
        for forbidden in ("write_text", "write_bytes", "open(", "os.replace", "mkstemp"):
            assert forbidden not in text, f"suggest.py can write: {forbidden}"


class TestAppliedForReal:
    """The proof that a proposal does what it says.

    Every other test here checks the shape of the comment. This one writes it
    into a real project, runs the real engine over it, and requires the finding
    to be gone -- and requires the other findings to survive, because a
    suppression that silenced everything would also pass the first half.
    """

    def test_applying_one_removes_exactly_that_finding(
        self, tmp_path: Path, vulnerable_project: Path
    ) -> None:
        project = tmp_path / "proj"
        shutil.copytree(vulnerable_project, project)

        before = engine.run(project, min_confidence=Confidence.TENTATIVE)
        target = next(
            f for f in before.findings if f.location.snippet and f.location.line and f.fingerprint
        )
        judgement = Judgement(target, Verdict.ACCEPTED_RISK, Source.MODEL, "deliberate on this box")
        proposal = propose(judgement, source_line_of(project, judgement))

        path = project / target.location.file
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[target.location.line - 1] = proposal.line
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        after = engine.run(project, min_confidence=Confidence.TENTATIVE)
        remaining = {f.fingerprint for f in after.findings}

        assert target.fingerprint not in remaining
        # And it silenced one finding rather than the file.
        survivors = {f.fingerprint for f in before.findings} - {target.fingerprint}
        assert survivors & remaining, "the suppression took more than it was aimed at"


class TestTheMaskedSnippetBug:
    """The defect the "apply it for real" test found, pinned so it stays fixed.

    A settings rule masks the secret it reports, so the finding's snippet for
    `SECRET_KEY = "3t(5n^s..."` is `SECRET_KEY = "*x<redacted:50 chars>"`. The
    first version of `propose` built its patch from that snippet. Applying it
    would have replaced a live credential with the redaction marker, and the
    diff would have looked entirely reasonable. Eight tests about the shape of
    the comment passed.
    """

    def test_the_patch_is_built_from_disk_not_from_the_finding(
        self, tmp_path: Path, vulnerable_project: Path
    ) -> None:
        project = tmp_path / "proj"
        shutil.copytree(vulnerable_project, project)

        result = engine.run(project, min_confidence=Confidence.TENTATIVE)
        masked = [f for f in result.findings if REDACTION_MARKER in (f.location.snippet or "")]
        assert masked, "no masked finding in the fixture; this test proves nothing"

        for finding in masked:
            judgement = Judgement(
                finding, Verdict.ACCEPTED_RISK, Source.MODEL, "deliberate on this box"
            )
            proposal = propose(judgement, source_line_of(project, judgement))

            assert REDACTION_MARKER not in proposal.line
            assert REDACTION_MARKER not in proposal.diff()
            # And the real value survives the round trip.
            real = source_line_of(project, judgement)
            assert proposal.line.startswith(real.rstrip())

    def test_a_masked_line_is_refused_outright(self) -> None:
        """Defence in depth: if a masked line ever reaches propose, it stops."""
        with pytest.raises(SuppressionError, match="masked"):
            propose(make_judgement(), 'SECRET_KEY = "*x<redacted:50 chars>"')


class TestTheSubmoduleIsShadowed:
    """A property of the package worth pinning, because it reads as a bug.

    `djaudit.llm` re-exports `suggest` and `triage`, and both are also module
    names, so `from djaudit.llm import suggest` yields the function and not the
    module it lives in. Deliberate -- the flat surface is what callers want --
    but it means monkeypatching a module attribute needs the full path, and
    silently patching the wrong object would make a test pass for no reason.
    """

    def test_the_package_attribute_is_the_function(self) -> None:
        import djaudit.llm as package

        assert package.suggest is suggest
        assert not isinstance(package.suggest, ModuleType)

    def test_the_module_is_still_reachable_by_path(self) -> None:
        module = importlib.import_module("djaudit.llm.suggest")

        assert isinstance(module, ModuleType)
        assert module.suggest is suggest
