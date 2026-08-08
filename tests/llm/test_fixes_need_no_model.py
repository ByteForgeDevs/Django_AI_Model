"""Proof that no fix path can consult a model.

The settings fixes are mechanical. `DEBUG = False` is not a judgement call, and
nothing about it is improved by asking a language model -- a model could only
introduce a way for the answer to be wrong. Every fixer's value is decided by
its own rule and checked against that rule's remediation text before it is
written, so the intelligence is in the rule, where it can be reviewed.

Saying that in a docstring is cheap. This file makes it a property that fails
loudly if it stops being true, in three independent ways:

  1. statically, over the transitive import graph, so a model-facing import
     added three modules deep is caught;
  2. through the CLI, by sabotaging the provider factory and showing `fix`
     unaffected while `triage` -- which genuinely does use one -- breaks;
  3. at the library level, by closing the socket layer entirely -- which
     assumes nothing about *how* a model would be reached.

Any one alone would be weak. The static walk cannot see a call through an
already-imported module; the sabotage cannot see an import that is never
exercised by the fixture. Together they close each other's gap.
"""

from __future__ import annotations

import ast
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

import djaudit.rules  # noqa: F401  -- registers every rule
from djaudit import cli, engine
from djaudit.llm.fix import fixes, patch
from djaudit.llm.verify import verified
from djaudit.models import Confidence

SOURCE = Path(cli.__file__).parent
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vulnerable_project"

#: Modules that exist to talk to, pay for, or prompt a model. A fix must not
#: reach any of them, at any depth.
MODEL_FACING = {
    "djaudit.llm.budget",
    "djaudit.llm.cache",
    "djaudit.llm.config",
    "djaudit.llm.evaluate",
    "djaudit.llm.prompts",
    "djaudit.llm.provider",
    "djaudit.llm.triage",
}

#: The modules a fix is built from. `djaudit.llm` itself is excluded on
#: purpose: the package `__init__` re-exports everything, so importing it
#: pulls in the whole layer and would make this test vacuous.
FIX_MODULES = ("djaudit.llm.fix", "djaudit.llm.edit", "djaudit.llm.verify")


def module_path(name: str) -> Path:
    return SOURCE.parent / Path(*name.split(".")).with_suffix(".py")


def imports_of(name: str) -> Iterator[str]:
    """Every `djaudit.*` module imported by `name`, read from source.

    Read from source rather than `sys.modules`, which by this point in a test
    run holds the entire package and would report every module as reachable
    from every other one.
    """
    tree = ast.parse(module_path(name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("djaudit"):
                    yield alias.name
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("djaudit"):
            assert node.module is not None
            yield node.module
            for alias in node.names:
                if module_path(f"{node.module}.{alias.name}").exists():
                    yield f"{node.module}.{alias.name}"


def reachable(*roots: str) -> set[str]:
    """The transitive closure of `djaudit` imports from `roots`."""
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen or not module_path(name).exists():
            continue
        seen.add(name)
        pending.extend(imports_of(name))
    return seen


class TestTheImportGraphIsClean:
    def test_no_fix_module_reaches_a_model_facing_one(self) -> None:
        closure = reachable(*FIX_MODULES)
        reached = sorted(closure & MODEL_FACING)
        assert reached == [], f"a fix path now reaches {reached}"

    def test_the_walk_is_transitive_not_shallow(self) -> None:
        """A one-level check would pass on a defect three modules deep.

        `verify` imports `fix`, which imports `edit`. If the walk stopped at
        the first level it would never see `edit` at all, and an import added
        there would go unnoticed.
        """
        assert "djaudit.llm.edit" not in set(imports_of("djaudit.llm.verify"))
        assert "djaudit.llm.edit" in reachable("djaudit.llm.verify")

    def test_the_walk_would_notice(self, tmp_path: Path) -> None:
        """The guard must fail on the thing it claims to detect.

        A clean graph is also what a broken detector reports, so the detector
        is pointed at a module that really does reach a provider.
        """
        assert reachable("djaudit.llm.triage") & MODEL_FACING

    def test_every_model_facing_module_exists(self) -> None:
        """A typo in the list above would silently exempt a module."""
        for name in MODEL_FACING:
            assert module_path(name).exists(), f"{name} is not a module"


class TestTheCliFixPathBuildsNoProvider:
    @staticmethod
    def _sabotage(monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(config: object) -> object:
            raise AssertionError("the fix path built a provider")

        monkeypatch.setattr(cli, "_build_provider", explode)

    def test_fix_runs_with_the_provider_factory_sabotaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._sabotage(monkeypatch)
        result = CliRunner().invoke(cli.app, ["fix", str(FIXTURE), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "DEBUG" in " ".join(result.output.split())

    def test_fix_verify_runs_with_the_provider_factory_sabotaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verification is the part most likely to grow a model later."""
        self._sabotage(monkeypatch)
        result = CliRunner().invoke(cli.app, ["fix", str(FIXTURE), "--dry-run", "--verify"])
        assert result.exit_code == 0, result.output

    def test_the_sabotage_really_does_break_a_command_that_uses_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this, both tests above would pass on a broken sabotage."""
        self._sabotage(monkeypatch)
        result = CliRunner().invoke(cli.app, ["triage", str(FIXTURE)])
        assert result.exit_code != 0
        assert isinstance(result.exception, AssertionError)


class TestTheLibraryFixPathOpensNoSocket:
    """The strongest form of the claim, because it does not name anything.

    Sabotaging `djaudit.llm.provider` only proves a fix does not reach *that*
    module -- and since nothing imports it, that assertion cannot fail and so
    proves nothing (a test that cannot fail is not evidence). Closing the
    socket layer instead makes no assumption about how a model would be
    reached. Any provider, under any name, in any package, has to open one.
    """

    @staticmethod
    def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
        import socket

        def refuse(*args: object, **kwargs: object) -> object:
            raise AssertionError("a fix opened a socket")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)

    def test_the_whole_path_runs_with_the_network_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "project"
        shutil.copytree(FIXTURE, root)
        self._no_network(monkeypatch)

        run = engine.run(root, min_confidence=Confidence.TENTATIVE)
        proposed, _refusals = fixes(run, root)
        assert proposed, "the fixture must yield at least one fix to prove anything"
        assert patch(proposed)
        report = verified(proposed, root, run.findings, Confidence.TENTATIVE)
        assert report.accepted

    def test_the_closure_really_does_stop_a_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Otherwise the test above passes on a sabotage that does nothing."""
        import socket

        self._no_network(monkeypatch)
        with pytest.raises(AssertionError, match="opened a socket"):
            socket.socket()
        with pytest.raises(AssertionError, match="opened a socket"):
            socket.getaddrinfo("example.invalid", 443)

    def test_a_fix_is_byte_identical_across_repeated_runs(self, tmp_path: Path) -> None:
        """Determinism is the point of not having a model in here."""
        first = tmp_path / "a"
        second = tmp_path / "b"
        shutil.copytree(FIXTURE, first)
        shutil.copytree(FIXTURE, second)

        def produce(root: Path) -> str:
            run = engine.run(root, min_confidence=Confidence.TENTATIVE)
            proposed, _ = fixes(run, root)
            return patch(proposed).replace(str(root), "ROOT")

        assert produce(first) == produce(second)
