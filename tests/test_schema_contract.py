"""The contract ledger keeps its promises.

Two things are being tested, and they are different. Most of these check that
the gate notices a change to the published contract. The last class checks the
gate's own reasoning about git history, which is the part that decides whether
"just regenerate the ledger" is a way around it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gen_schema

from djaudit import fingerprint as fp
from djaudit.models import SCHEMA_VERSION

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _audit(project: Path, out: Path) -> None:
    """Run the real CLI in a separate process, the way a user would."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from djaudit.cli import app; app()",
            "run",
            str(project),
            "--format",
            "json",
            "--output",
            str(out),
        ],
        check=False,
        capture_output=True,
    )


def complaint(capsys: pytest.CaptureFixture[str]) -> str:
    """Run the gate, require it to fail, and hand back what it said.

    Asserting the message rather than the exit code matters here: several
    checks in this gate can fail for overlapping reasons, and a test happy with
    any non-zero exit would pass while the check it names is disconnected.
    """
    code = gen_schema.check()
    said = capsys.readouterr().out
    assert code == 1, f"gate passed when it should have failed; it said: {said}"
    return said


def rewrite(path: Path, old: str, new: str) -> None:
    """Edit via a unique anchor, so a control cannot silently do nothing."""
    text = path.read_text()
    found = text.count(old)
    assert found == 1, f"anchor appears {found} times, need exactly 1: {old[:60]!r}"
    path.write_text(text.replace(old, new, 1))


def tamper(ledger: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    data = json.loads(ledger.read_text())
    mutate(data)
    ledger.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _install_doc(root: Path) -> Path:
    """Recreate everything the doc gate needs, inside a scratch tree.

    Derived from the doc's own citations rather than a hardcoded list, so a
    newly cited file cannot quietly turn these fixtures into a broken tree that
    fails every test for the wrong reason.
    """
    doc = root / "docs" / "versioning.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    text = gen_schema.DOC.read_text()
    doc.write_text(text)
    for cited in re.findall(r"(?:src|docs|scripts|tests|schema)/[\w./-]+", text):
        path = root / cited.rstrip(".,;:")
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    for name, home in gen_schema.CONSTANTS:
        (root / home).write_text(f"{name} = 1\n")
    return doc


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the real ledger the gate will read instead of the committed one."""
    target = tmp_path / "schema" / "contract.json"
    target.parent.mkdir(parents=True)
    target.write_text(gen_schema.LEDGER.read_text())
    monkeypatch.setattr(gen_schema, "ROOT", tmp_path)
    monkeypatch.setattr(gen_schema, "LEDGER", target)
    monkeypatch.setattr(gen_schema, "DOC", _install_doc(tmp_path))
    return target


class TestItPassesOnTheRealTree:
    def test_the_committed_ledger_matches_the_code(self) -> None:
        assert gen_schema.check() == 0

    def test_the_generator_is_idempotent(self) -> None:
        assert gen_schema.build() == json.loads(gen_schema.LEDGER.read_text())


class TestTheShapeIsFrozen:
    def test_a_removed_field_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(ledger, lambda d: d["schemas"][str(SCHEMA_VERSION)]["shape"].pop("summary.reported"))
        assert "added:   summary.reported" in complaint(capsys)

    def test_an_added_field_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(
            ledger,
            lambda d: d["schemas"][str(SCHEMA_VERSION)]["shape"].update(
                {"summary.invented": "integer"}
            ),
        )
        assert "removed: summary.invented" in complaint(capsys)

    def test_a_retyped_field_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(
            ledger,
            lambda d: d["schemas"][str(SCHEMA_VERSION)]["shape"].update(
                {"summary.reported": "string"}
            ),
        )
        assert "retyped: summary.reported (string -> integer)" in complaint(capsys)

    def test_it_names_the_next_version(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(ledger, lambda d: d["schemas"][str(SCHEMA_VERSION)]["shape"].pop("summary.reported"))
        assert f"bump SCHEMA_VERSION to {SCHEMA_VERSION + 1}" in complaint(capsys)

    def test_a_missing_entry_for_the_current_version_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(ledger, lambda d: d["schemas"].pop(str(SCHEMA_VERSION)))
        assert f"SCHEMA_VERSION is {SCHEMA_VERSION} and the ledger has no entry" in complaint(
            capsys
        )


class TestTheEnumsAreContract:
    """A new enum value is a breaking change, and the easiest one to wave through."""

    def test_a_dropped_member_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(
            ledger, lambda d: d["schemas"][str(SCHEMA_VERSION)]["enums"]["Severity"].remove("info")
        )
        said = complaint(capsys)
        assert "enum Severity changed" in said
        assert "will not recognise the new one" in said

    def test_a_reordered_enum_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Order is contract too: consumers index into severity ladders."""
        tamper(
            ledger,
            lambda d: d["schemas"][str(SCHEMA_VERSION)]["enums"].update(
                {"Severity": list(reversed(d["schemas"][str(SCHEMA_VERSION)]["enums"]["Severity"]))}
            ),
        )
        assert "enum Severity changed" in complaint(capsys)

    def test_a_missing_enum_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(ledger, lambda d: d["schemas"][str(SCHEMA_VERSION)]["enums"].pop("Family"))
        assert "enum Family is not recorded" in complaint(capsys)

    def test_every_enum_in_the_report_is_recorded(self) -> None:
        """The list of guarded enums is itself a claim worth checking."""
        recorded = set(gen_schema.contract()["enums"])
        assert recorded == {e.__name__ for e in gen_schema.ENUMS}
        assert "Severity" in recorded, "the enum most likely to gain a member"


class TestTheFingerprintIsTheBaselineContract:
    def test_a_changed_digest_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def bend(d: dict[str, Any]) -> None:
            vectors = d["fingerprints"][fp.FINGERPRINT_VERSION]["vectors"]
            vectors[next(iter(vectors))] = "0" * 16

        tamper(ledger, bend)
        said = complaint(capsys)
        assert "the fingerprint algorithm changed" in said
        assert "every baseline committed downstream stops matching" in said

    def test_a_dropped_vector_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def bend(d: dict[str, Any]) -> None:
            vectors = d["fingerprints"][fp.FINGERPRINT_VERSION]["vectors"]
            vectors["('DJZ-000', 'gone.py', 'x', 0)"] = "0" * 16

        tamper(ledger, bend)
        assert "was dropped; published vectors are frozen" in complaint(capsys)

    def test_a_missing_version_is_caught(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(ledger, lambda d: d["fingerprints"].pop(fp.FINGERPRINT_VERSION))
        assert "the ledger has no vectors for it" in complaint(capsys)

    def test_the_vectors_actually_exercise_normalisation(self) -> None:
        """A vector set that never varies whitespace cannot see a normalisation change."""
        vectors = gen_schema.vectors()
        collapsed = fp.compute("DJS-001", "settings.py", "DEBUG   =\n\tTrue", 0)
        plain = fp.compute("DJS-001", "settings.py", "DEBUG = True", 0)
        assert collapsed == plain, "normalisation should make these identical"
        assert plain in vectors.values(), "and that identity must be pinned"

    def test_the_vectors_exercise_the_occurrence_index(self) -> None:
        first = fp.compute("DJS-001", "settings.py", "DEBUG = True", 0)
        second = fp.compute("DJS-001", "settings.py", "DEBUG = True", 1)
        assert first != second
        assert {first, second} <= set(gen_schema.vectors().values())

    def test_vector_keys_are_unique(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two vectors sharing a key would leave one silently unpinned."""
        duplicated = (*gen_schema.VECTORS, gen_schema.VECTORS[0])
        monkeypatch.setattr(gen_schema, "VECTORS", duplicated)
        with pytest.raises(AssertionError, match="vector keys collide"):
            gen_schema.vectors()


class TestPublishedEntriesAreAppendOnly:
    """The interesting case: regenerating the ledger must not launder a change.

    Without this, the whole gate is advisory. A developer changes the schema,
    the check fails, they run the generator because that is what the message
    told them to do, and the ledger now agrees with the new code under the old
    version number. Every one of these tests runs against a real git repo,
    because the rule is defined by what is already committed.
    """

    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        target = tmp_path / "schema" / "contract.json"
        target.parent.mkdir(parents=True)
        target.write_text(gen_schema.LEDGER.read_text())
        monkeypatch.setattr(gen_schema, "DOC", _install_doc(tmp_path))
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.invalid"],
            ["config", "user.name", "t"],
            ["add", "-A"],
            ["commit", "-qm", "ledger"],
        ):
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
        monkeypatch.setattr(gen_schema, "ROOT", tmp_path)
        monkeypatch.setattr(gen_schema, "LEDGER", target)
        return target

    def test_the_committed_state_passes(self, repo: Path) -> None:
        assert gen_schema.check() == 0

    def test_regenerating_over_a_published_entry_is_caught(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """This is the bypass the gate exists to close."""
        tamper(
            repo,
            lambda d: d["schemas"][str(SCHEMA_VERSION)]["shape"].update({"snuck.in": "string"}),
        )
        said = complaint(capsys)
        assert "was edited in place" in said
        assert "never rewritten" in said

    def test_deleting_a_published_entry_is_caught(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tamper(repo, lambda d: d["schemas"].pop(str(SCHEMA_VERSION)))
        assert "was deleted from the ledger" in complaint(capsys)

    def test_a_new_version_without_a_release_bump_is_caught(
        self, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A schema bump is breaking, so it cannot ride along on the same release."""

        def bend(d: dict[str, Any]) -> None:
            d["schemas"][str(SCHEMA_VERSION + 1)] = dict(d["schemas"][str(SCHEMA_VERSION)])

        tamper(repo, bend)
        monkeypatch.setattr(gen_schema, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
        assert "without a release bump" in complaint(capsys)

    def test_a_new_version_with_a_release_bump_passes(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contrast: the same change is fine once the release moves."""

        def bend(d: dict[str, Any]) -> None:
            entry = dict(d["schemas"][str(SCHEMA_VERSION)])
            entry["tool_version"] = "0.2.0"
            d["schemas"][str(SCHEMA_VERSION + 1)] = entry

        tamper(repo, bend)
        monkeypatch.setattr(gen_schema, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
        monkeypatch.setattr(gen_schema, "__version__", "0.2.0")
        assert gen_schema.check() == 0

    def test_an_untracked_ledger_is_not_treated_as_tampering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh checkout with no git history must still be checkable."""
        target = tmp_path / "schema" / "contract.json"
        target.parent.mkdir(parents=True)
        target.write_text(gen_schema.LEDGER.read_text())
        monkeypatch.setattr(gen_schema, "ROOT", tmp_path)
        monkeypatch.setattr(gen_schema, "LEDGER", target)
        monkeypatch.setattr(gen_schema, "DOC", _install_doc(tmp_path))
        assert gen_schema._committed() is None
        assert gen_schema.check() == 0


class TestTheReleaseSeriesRule:
    """Below 1.0 the minor carries breakage; at and above it, the major does."""

    @pytest.mark.parametrize(
        ("version", "series"),
        [
            ("0.1.0", "0.1"),
            ("0.1.9", "0.1"),
            ("0.2.0", "0.2"),
            ("1.0.0", "1"),
            ("1.4.2", "1"),
            ("2.0.0", "2"),
        ],
    )
    def test_the_series_is_what_a_breaking_change_must_move(
        self, version: str, series: str
    ) -> None:
        assert gen_schema._release_series(version) == series

    def test_a_patch_does_not_change_the_series(self) -> None:
        assert gen_schema._release_series("0.1.0") == gen_schema._release_series("0.1.7")

    def test_a_minor_changes_the_series_before_one_point_oh(self) -> None:
        assert gen_schema._release_series("0.1.0") != gen_schema._release_series("0.2.0")

    def test_a_minor_does_not_change_the_series_after_one_point_oh(self) -> None:
        assert gen_schema._release_series("1.1.0") == gen_schema._release_series("1.2.0")


class TestTheContractDescribesRealOutput:
    """A specimen is only evidence while it still resembles the real thing.

    The contract is derived from two constructed runs. If those drift from what
    djaudit actually emits, the ledger freezes a shape nobody ships.
    """

    @pytest.mark.parametrize(
        "project",
        ["vulnerable_project", "drf_project", "orm_project", "injection_project"],
    )
    def test_a_real_run_conforms(self, project: str, tmp_path: Path) -> None:
        out = tmp_path / "report.json"
        _audit(FIXTURES / project, out)
        assert out.exists(), "the run produced no report"
        emitted = gen_schema.shape(json.loads(out.read_text()))
        recorded = json.loads(gen_schema.LEDGER.read_text())["schemas"][str(SCHEMA_VERSION)][
            "shape"
        ]
        assert emitted, "a report with no paths would make this test vacuous"
        unknown = sorted(set(emitted) - set(recorded))
        assert not unknown, f"emitted paths absent from the contract: {unknown}"
        for path, kind in emitted.items():
            assert kind in recorded[path].split("|"), (
                f"{path} emitted {kind}, contract says {recorded[path]}"
            )

    def test_the_run_actually_produced_findings(self, tmp_path: Path) -> None:
        """Guards the test above: no findings would mean no finding paths to check."""
        out = tmp_path / "report.json"
        _audit(FIXTURES / "vulnerable_project", out)
        assert json.loads(out.read_text())["findings"], "fixture stopped producing findings"


class TestTheOpenMapRule:
    def test_data_keys_do_not_become_contract(self) -> None:
        """A filename from one specimen must not demand a version bump later."""
        shape = gen_schema.contract()["shape"]
        assert shape["parse_errors"] == "map"
        assert not [k for k in shape if k.startswith("parse_errors.")]
        assert not [k for k in shape if k.startswith("rule_errors.")]

    def test_severity_keys_are_contract(self) -> None:
        """by_severity is deliberately not collapsed: its keys are the enum."""
        shape = gen_schema.contract()["shape"]
        assert shape["summary.by_severity.critical"] == "integer"

    def test_the_shape_has_no_uninhabited_containers(self) -> None:
        """An empty list records no child paths, so its fields escape the contract."""
        shape = gen_schema.contract()["shape"]
        for path, kind in shape.items():
            if kind == "array":
                children = [k for k in shape if k.startswith(f"{path}[]")]
                assert children, f"{path} is an array the specimens never populated"


class TestTheDocDescribesTheCode:
    """A policy document is a claim about the code, and claims rot."""

    @pytest.fixture
    def doc(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        target = _install_doc(tmp_path)
        monkeypatch.setattr(gen_schema, "ROOT", tmp_path)
        monkeypatch.setattr(gen_schema, "DOC", target)
        return target

    def _problems(self, doc: Path) -> list[str]:
        problems: list[str] = []
        gen_schema._check_doc(problems)
        return problems

    def test_the_real_doc_is_consistent(self) -> None:
        assert self._problems(gen_schema.DOC) == []

    def test_the_fixture_is_a_valid_control(self, doc: Path) -> None:
        """Without this, every test below could pass on a broken fixture."""
        assert self._problems(doc) == []

    def test_a_missing_file_is_caught(self, doc: Path) -> None:
        (doc.parent.parent / "scripts" / "validate_sarif.py").unlink()
        assert any("validate_sarif.py, which does not exist" in p for p in self._problems(doc))

    def test_a_constant_that_moved_is_caught(
        self, doc: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The doc's table is right until someone moves the constant."""
        home = doc.parent.parent / "src" / "djaudit" / "models.py"
        home.write_text("# SCHEMA_VERSION used to live here\n")
        problems = self._problems(doc)
        assert any("says SCHEMA_VERSION lives in" in p for p in problems), problems

    def test_an_indented_definition_does_not_count(self, doc: Path) -> None:
        """A name mentioned inside a function is not where the constant lives."""
        home = doc.parent.parent / "src" / "djaudit" / "models.py"
        home.write_text("def f():\n    SCHEMA_VERSION = 1\n")
        assert any("says SCHEMA_VERSION lives in" in p for p in self._problems(doc))

    def test_an_undocumented_open_map_is_caught(
        self, doc: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gen_schema, "OPEN_MAPS", frozenset({"summary.invented"}))
        assert any("summary.invented collapses to an open map" in p for p in self._problems(doc))

    def test_an_unnamed_enum_is_caught(self, doc: Path) -> None:
        doc.write_text(doc.read_text().replace("`Severity`", "the severity field"))
        assert any("never names the Severity enum" in p for p in self._problems(doc))

    def test_a_missing_doc_is_caught(self, doc: Path) -> None:
        doc.unlink()
        assert any("is missing" in p for p in self._problems(doc))
