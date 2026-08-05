"""Generation-2 garbage collection policy for the span of an analysis run.

A run holds every parsed module's AST live from the moment it is parsed until
the run ends, because rules are free to revisit any module at any point. On
NetBox that is roughly 2.3 million tracked objects, and they are the *working
set* rather than garbage: essentially nothing in that heap is collectable while
the run is still using it.

CPython's generation-2 collector traverses the entire heap. During a run it
therefore performs a full traversal of those 2.3 million live objects and
reclaims approximately nothing -- four or five times per run. Measured on the
three benchmark targets, those handful of traversals cost:

    healthchecks  1.58s -> 1.35s   (-15%)
    netbox        9.22s -> 7.43s   (-19%)
    pretix        8.57s -> 6.65s   (-22%)

Suppressing generation 2 for the duration of a run removes that cost while
leaving generations 0 and 1 running, so short-lived cycles are still reclaimed
as they are created and peak memory is bounded by the working set rather than
by everything the run has ever allocated.

**Why not disable the collector outright.** It is faster still -- 6.43s on
NetBox, another 14% -- but it reclaims nothing at all for the duration, so peak
memory becomes a function of total allocation. That is an acceptable trade for
a CLI that exits afterwards and a bad one for a library embedded in a
long-lived host process, which is exactly what Phase 6's LLM layer is. We take
the smaller, safe win.

**This is mostly not deferred work.** The traversals removed were largely
waste: they walked a live heap and freed little. Checked end-to-end against
total process wall time, which no internal timer can flatter:

    netbox        10.74s -> 9.12s  (-15%)
    pretix         9.60s -> 8.43s  (-12%)

Those margins are smaller than the in-run ones because process wall also
includes interpreter startup and teardown, which this does not touch. On
NetBox the in-run saving is 1.79s and 1.62s of it survives to process wall, so
roughly 90% is work genuinely removed and roughly 10% is paid back when the
process tears down. The tests below assert the mechanism -- threshold raised,
restored on both exits, opt-out honoured -- and not the timings, because a
wall-clock assertion on a shared CI runner is a flaky test rather than a
measurement.

The GC threshold is global interpreter state, so this is a context manager that
restores exactly what it found, and ``DJAUDIT_GC_POLICY=default`` disables the
adjustment entirely for anyone who needs stock behaviour.
"""

from __future__ import annotations

import gc
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator

ENV_VAR: Final = "DJAUDIT_GC_POLICY"
"""Set to ``default`` to leave CPython's collector thresholds untouched."""

STOCK: Final = "default"

SUPPRESSED: Final = 2**31 - 1
"""Generation-2 threshold high enough that a run never reaches it."""


def suppression_enabled() -> bool:
    """Whether this process wants generation-2 suppression."""
    return os.environ.get(ENV_VAR, "").strip().lower() != STOCK


@contextmanager
def deferred_full_collection() -> Iterator[None]:
    """Suppress generation-2 collections for the duration of the block.

    Generations 0 and 1 continue to run. The previous thresholds are restored on
    exit, including when the block raises -- leaking a modified global collector
    setting out of a failed run would be far worse than the cost it saves.

    Nesting is safe. Concurrent runs in separate threads are not: the inner
    block's restore would clobber the outer one's. Nothing in djaudit runs
    analyses concurrently, and a caller that does can opt out with
    :data:`ENV_VAR`.
    """
    if not suppression_enabled():
        yield
        return

    gen0, gen1, gen2 = gc.get_threshold()
    if gen0 == 0:
        # Threshold 0 means the caller has already switched collection off.
        # Raising generation 2 from there would be meaningless, and restoring
        # it afterwards risks turning collection back on behind their back.
        yield
        return

    gc.set_threshold(gen0, gen1, SUPPRESSED)
    try:
        yield
    finally:
        gc.set_threshold(gen0, gen1, gen2)
