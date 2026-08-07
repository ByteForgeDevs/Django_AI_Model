"""Tests for the generation-2 collection policy.

These assert the *mechanism*: that the threshold is raised inside the block,
restored on every exit path, and left alone when the caller opts out. They
deliberately do not assert timings. The saving is real and is recorded with
measurements in the module docstring, but a wall-clock assertion on a shared CI
runner measures the runner's mood, and a flaky gate is worse than no gate.
"""

from __future__ import annotations

import gc
from pathlib import Path

import pytest

from djaudit import engine, gcpolicy


class TestRaisingTheThreshold:
    """Inside the block, full collections must be out of reach."""

    def test_generation_two_is_suppressed_inside(self) -> None:
        with gcpolicy.deferred_full_collection():
            assert gc.get_threshold()[2] == gcpolicy.SUPPRESSED

    def test_generations_zero_and_one_are_untouched(self) -> None:
        """Short-lived cycles must still be reclaimed while a run is going."""
        before = gc.get_threshold()
        with gcpolicy.deferred_full_collection():
            during = gc.get_threshold()
        assert during[:2] == before[:2]
        assert during[0] > 0, "generation 0 still collects"

    def test_the_suppressed_value_is_unreachable_in_practice(self) -> None:
        """A threshold a long run could reach would not be a suppression."""
        assert gcpolicy.SUPPRESSED > 2_000_000_000


class TestRestoring:
    """Global interpreter state must not leak out of the block."""

    def test_threshold_is_restored_on_normal_exit(self) -> None:
        before = gc.get_threshold()
        with gcpolicy.deferred_full_collection():
            pass
        assert gc.get_threshold() == before

    def test_threshold_is_restored_when_the_block_raises(self) -> None:
        """A failed run leaking a modified collector setting is the worst case.

        It would silently change garbage collection for the rest of the host
        process, and the traceback would point at the rule that crashed rather
        than at us.
        """
        before = gc.get_threshold()
        with pytest.raises(ValueError, match="rule exploded"), gcpolicy.deferred_full_collection():
            raise ValueError("rule exploded")
        assert gc.get_threshold() == before

    def test_nesting_restores_the_outer_value(self) -> None:
        before = gc.get_threshold()
        with gcpolicy.deferred_full_collection():
            with gcpolicy.deferred_full_collection():
                assert gc.get_threshold()[2] == gcpolicy.SUPPRESSED
            assert gc.get_threshold()[2] == gcpolicy.SUPPRESSED
        assert gc.get_threshold() == before

    def test_restores_a_non_default_threshold_exactly(self) -> None:
        """We restore what we found, not what CPython ships with."""
        original = gc.get_threshold()
        try:
            gc.set_threshold(123, 4, 5)
            with gcpolicy.deferred_full_collection():
                pass
            assert gc.get_threshold() == (123, 4, 5)
        finally:
            gc.set_threshold(*original)


class TestOptingOut:
    """``DJAUDIT_GC_POLICY=default`` must mean exactly that."""

    def test_opt_out_leaves_the_threshold_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(gcpolicy.ENV_VAR, "default")
        before = gc.get_threshold()
        with gcpolicy.deferred_full_collection():
            assert gc.get_threshold() == before

    @pytest.mark.parametrize("value", ["DEFAULT", " default ", "Default"])
    def test_opt_out_is_case_and_space_insensitive(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(gcpolicy.ENV_VAR, value)
        assert not gcpolicy.suppression_enabled()

    @pytest.mark.parametrize("value", ["", "suppress", "yes", "off"])
    def test_anything_else_means_suppress(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Only the documented word opts out. An unrecognised value is not a
        licence to silently pick the slower behaviour."""
        monkeypatch.setenv(gcpolicy.ENV_VAR, value)
        assert gcpolicy.suppression_enabled()

    def test_unset_means_suppress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(gcpolicy.ENV_VAR, raising=False)
        assert gcpolicy.suppression_enabled()


class TestACallerWhoAlreadyTurnedCollectionOff:
    """Threshold 0 disables collection. We must not quietly re-enable it."""

    def test_we_do_not_touch_a_disabled_collector(self) -> None:
        original = gc.get_threshold()
        try:
            gc.set_threshold(0)
            with gcpolicy.deferred_full_collection():
                assert gc.get_threshold()[0] == 0, "still disabled inside the block"
            assert gc.get_threshold()[0] == 0, "still disabled after it"
        finally:
            gc.set_threshold(*original)


class TestTheEngineUsesIt:
    """The policy is worthless if the engine does not actually apply it.

    Suppressing collection changes no output whatsoever, so every other test in
    the suite passes identically with the policy wired in or ripped out. These
    two are the only thing standing between the saving and a silent regression.
    """

    def test_run_suppresses_during_the_analysis(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Observed from inside the audit, not around it.

        Checking the threshold before and after ``run`` cannot distinguish
        "suppressed throughout" from "never touched" -- both leave it restored.
        The only honest observation point is during the work itself.
        """
        seen: list[int] = []
        real = engine._audit

        def spy(root: Path, **kwargs: object) -> engine.RunResult:
            seen.append(gc.get_threshold()[2])
            return real(root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("djaudit.engine._audit", spy)
        engine.run(_tiny_project(tmp_path))
        assert seen == [gcpolicy.SUPPRESSED]

    def test_engine_restores_the_threshold_after_a_run(self, tmp_path: Path) -> None:
        before = gc.get_threshold()
        engine.run(_tiny_project(tmp_path))
        assert gc.get_threshold() == before


def _tiny_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "cfg").mkdir(parents=True)
    (root / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cfg.settings')\n"
    )
    (root / "cfg" / "__init__.py").write_text("")
    (root / "cfg" / "settings.py").write_text("DEBUG = False\nSECRET_KEY = 'x'\n")
    return root
