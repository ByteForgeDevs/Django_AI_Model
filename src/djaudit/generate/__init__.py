"""Writing Django, then proving it.

Phases 0-7 built an auditor. Phase 8 puts it in the loop that writes the code,
which is the only place a static analyser can prevent a defect rather than
report one.

The measurement this exists for: a Django app written naturally by a language
model -- two models, two serializers, two viewsets, about forty lines --
produced six findings, two of them critical SQL injection. Not because the
model is bad at Django, but because reviewing your own output uses the faculty
that produced it, so the same blind spot applies twice. Fed the findings back,
the same model cleared all six in one iteration.

That is the whole design. The generator is a language model. The judge is 87
deterministic rules with evidence attached, and the judge is not a model, does
not negotiate, and cannot be talked out of a finding.

Three degenerate solutions exist for any audit-until-clean loop, and each is
closed structurally rather than by asking nicely:

- Write a sixth file, or ``settings.py``. Closed by the response schema: there
  is no field for it (``scaffold``).
- Delete the feature that carries the finding. Closed by comparing declared
  surfaces between iterations (``surface``).
- Suppress the rule. Closed by refusing output containing a suppression marker
  (``loop``).
"""

from __future__ import annotations

from djaudit.generate.loop import DEFAULT_MAX_ITERATIONS, Iteration, Loop, Outcome, Run, commit
from djaudit.generate.scaffold import SCHEMA, WRITABLE, Spec, first_prompt, repair_prompt, write_app
from djaudit.generate.surface import Regression, Surface, surface_of

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "SCHEMA",
    "WRITABLE",
    "Iteration",
    "Loop",
    "Outcome",
    "Regression",
    "Run",
    "Spec",
    "Surface",
    "commit",
    "first_prompt",
    "repair_prompt",
    "surface_of",
    "write_app",
]
