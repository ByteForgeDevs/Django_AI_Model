"""The mutation harness, checked -- because it was reporting mutants as killed
that it had never actually run.

`scripts/mutate.py` is what every claim of the form "this rule's tests are
load-bearing" in the project plan rests on, and it had no tests of its own.
It turned out to have a bug that inflated its own score.

CPython decides a cached `.pyc` is still current by comparing the source
file's mtime *in whole seconds* and its size. The harness writes one mutant
after another into the same path, and mutants of one module are very often
exactly the same length as each other -- `True` to `False` in two different
decorators produces two files of identical size, and so does a `>` becoming
`<=` somewhere else. When two such mutants land inside the same second, the
second one is indistinguishable from the first to that check, so the
interpreter loads the *first* one's bytecode. The tests then pass, and the
harness records a survivor for code it never executed.

That was found on `adapters/base.py`, where the mutant flipping the
duplicate-code guard was reported SURVIVED on two consecutive full runs and
died instantly when run by hand. `test_a_stale_cache_would_hide_a_mutant` is
the control: it reproduces the silent skip with the fix withheld, so these
tests fail if the hazard is ever no longer real, rather than passing quietly
because the mechanism has changed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mutate import drop_bytecode, mutants  # noqa: E402


def read_value(module: Path) -> str:
    """Import the module in a fresh interpreter and report what it saw.

    A subprocess, because the point is what a *newly started* Python resolves
    from disk -- which is what the harness does for every mutant, and is the
    only place the stale-cache bug is visible.
    """
    done = subprocess.run(
        [sys.executable, "-c", "import subject; print(subject.VALUE)"],
        cwd=module.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def write_same_size(module: Path, value: str, mtime: float) -> None:
    """Overwrite the module with a same-length body and a forced mtime.

    Both halves matter: equal size and equal whole-second mtime together are
    what make CPython consider its cached bytecode current.
    """
    module.write_text(f"VALUE = {value}\n")
    os.utime(module, (mtime, mtime))


class TestTheHarnessRunsTheMutantItReports:
    def test_a_stale_cache_would_hide_a_mutant(self, tmp_path: Path) -> None:
        # The control. With the cache left alone, a second module of the same
        # length written in the same second is never seen: Python answers from
        # the first one's bytecode. If this ever stops being true the fix below
        # is no longer load-bearing and these tests should say so.
        subject = tmp_path / "subject.py"
        write_same_size(subject, "'a'", mtime=1_700_000_000)
        assert read_value(subject) == "a"

        write_same_size(subject, "'b'", mtime=1_700_000_000)
        assert read_value(subject) == "a", "expected the stale cache to win"

    def test_dropping_the_cache_makes_the_mutant_run(self, tmp_path: Path) -> None:
        subject = tmp_path / "subject.py"
        write_same_size(subject, "'a'", mtime=1_700_000_000)
        assert read_value(subject) == "a"

        write_same_size(subject, "'b'", mtime=1_700_000_000)
        drop_bytecode(subject)
        assert read_value(subject) == "b"

    def test_it_is_not_an_error_when_nothing_was_cached(self, tmp_path: Path) -> None:
        # The harness calls this on the restore path too, where the module may
        # never have been imported. Raising there would turn a clean run into a
        # failure after every mutant had already been judged.
        subject = tmp_path / "subject.py"
        subject.write_text("VALUE = 'a'\n")
        drop_bytecode(subject)
        drop_bytecode(subject)

    def test_the_cache_is_gone_rather_than_merely_rewritten(self, tmp_path: Path) -> None:
        import importlib.util

        subject = tmp_path / "subject.py"
        write_same_size(subject, "'a'", mtime=1_700_000_000)
        read_value(subject)
        cached = Path(importlib.util.cache_from_source(str(subject)))
        assert cached.exists(), "expected the control to have produced a cache"

        drop_bytecode(subject)
        assert not cached.exists()


class TestTheMutantsItGenerates:
    def test_a_comparison_is_flipped_rather_than_deleted(self) -> None:
        # A deleted comparison usually fails to parse or fails every test, which
        # is a mutant no suite can be praised for killing. A flip is the one a
        # test has to be specific to notice.
        source = "def f(n):\n    return n > 1\n"
        labels = [m.label for m in mutants(source)]
        assert any("flip Gt" in label for label in labels)

    def test_every_mutant_is_a_different_module(self) -> None:
        source = "def f(n):\n    return n > 1 and n < 9\n"
        made = mutants(source)
        assert len({m.source for m in made}) == len(made)
