"""The gate that keeps 5.3.3's decline honest.

Step 5.3.3 declined to build a bandit adapter because ruff's `S` rules are a
port of bandit and the port is complete enough that bandit's only unshared
output was noise or a duplicate. That is a claim about a third-party tool's
inventory, and third-party inventories change, so the reasoning is re-checked
rather than remembered. These tests are about the checker: that it notices a
new check, that it notices a stale exception, and that it cannot pass by
comparing two empty sets.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load() -> Any:
    """Import the script by path, the way the other gate tests do."""
    path = ROOT / "scripts" / "check_bandit_subsumed.py"
    spec = importlib.util.spec_from_file_location("check_bandit_subsumed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate() -> Any:
    return load()


class TestTheClaimItself:
    def test_the_real_inventories_still_agree(self, gate: Any) -> None:
        # The gate as CI runs it. If this fails, bandit has changed and 5.3.3
        # needs re-reading -- which is the entire point of the script.
        assert gate.main() == 0

    def test_bandit_reads_both_of_its_registries(self, gate: Any) -> None:
        # Plugins and blacklist are separate, and both produce findings.
        # Reading only the first understates bandit by a third.
        checks = gate.bandit_checks()
        assert "B113" in checks, "a plugin check"
        assert "B413" in checks, "a blacklist check"
        assert len(checks) >= 75

    def test_every_exception_is_still_a_check_bandit_has(self, gate: Any) -> None:
        # An exception naming a check bandit dropped would sit in the list
        # forever, excusing nothing.
        checks = gate.bandit_checks()
        for code, (name, _) in gate.EXCEPTIONS.items():
            assert checks.get(code) == name

    def test_every_exception_says_why(self, gate: Any) -> None:
        for code, (_, reason) in gate.EXCEPTIONS.items():
            assert len(reason) > 30, code


class TestWhatMakesItFail:
    """Each test withholds one thing and checks the gate objects.

    Without these the script could return 0 unconditionally and every claim
    above would still pass.
    """

    def test_an_unaccounted_check_fails(self, gate: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(gate, "bandit_checks", lambda: {**_checks(), "B999": "brand_new"})
        assert gate.main() == 1

    def test_an_exception_ruff_has_caught_up_with_fails(self, gate: Any, monkeypatch: Any) -> None:
        # If ruff ports `B614`, keeping it on the exception list would hide the
        # fact that the comparison now covers it.
        monkeypatch.setattr(gate, "ruff_security_rules", lambda: _ruff() | {"S614"})
        assert gate.main() == 1

    def test_a_renamed_check_fails(self, gate: Any, monkeypatch: Any) -> None:
        # The exception is recorded against a name as well as a number, so a
        # check repurposed under the same id does not inherit its excuse.
        monkeypatch.setattr(
            gate, "bandit_checks", lambda: {**_checks(), "B703": "something_else_entirely"}
        )
        assert gate.main() == 1

    def test_an_empty_inventory_fails_rather_than_passes(self, gate: Any, monkeypatch: Any) -> None:
        # The failure this gate exists to avoid having itself. Two empty sets
        # agree perfectly, and a subsumption check that reads nothing would
        # report success forever.
        monkeypatch.setattr(gate, "bandit_checks", dict)
        assert gate.main() == 1

    def test_an_empty_ruff_inventory_fails_too(self, gate: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(gate, "ruff_security_rules", set)
        assert gate.main() == 1

    def test_a_missing_bandit_fails_rather_than_skips(self, gate: Any, monkeypatch: Any) -> None:
        def absent() -> dict[str, str]:
            raise ImportError("no bandit here")

        monkeypatch.setattr(gate, "bandit_checks", absent)
        assert gate.main() == 1

    def test_a_ruff_that_will_not_answer_fails_rather_than_skips(
        self, gate: Any, monkeypatch: Any
    ) -> None:
        def absent() -> set[str]:
            raise OSError("no ruff here")

        monkeypatch.setattr(gate, "ruff_security_rules", absent)
        assert gate.main() == 1


def _checks() -> dict[str, str]:
    checks: dict[str, str] = load().bandit_checks()
    return checks


def _ruff() -> set[str]:
    rules: set[str] = load().ruff_security_rules()
    return rules
