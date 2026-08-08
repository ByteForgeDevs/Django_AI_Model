"""A static audit must not drag in the machinery for executing the target.

This has been a defect twice, by two different doors. In 4.1.4 the degradation
module lived under `djaudit.live`, so `import djaudit.engine` pulled the whole
subprocess stack and cost 82ms on every run. In 4.4.3 `DJM-010` imported
`djaudit.live.sqlmigrate` at module scope, and because every rule module is
imported on every run, the same stack came back for another 24ms.

Neither was visible in any test. Both were found by measuring, and a measurement
nobody repeats is a measurement that stops being true. So it is a test.

The check runs in a fresh interpreter. `sys.modules` inside a pytest process has
been populated by pytest, by other test modules, and by this one, so asking it
directly would answer a different question.
"""

from __future__ import annotations

import subprocess
import sys

TIMEOUT = 120

PROBE = """
import sys
import djaudit.rules
djaudit.rules.load_all()
print("subprocess" in sys.modules)
print(any(name.startswith("djaudit.live") for name in sys.modules))
"""

CONTROL = """
import sys
import djaudit.live.sqlmigrate
print("subprocess" in sys.modules)
print(any(name.startswith("djaudit.live") for name in sys.modules))
"""


def run(code: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


class TestLoadingEveryRuleStaysStatic:
    def test_subprocess_is_not_imported(self) -> None:
        """The audit tier never executes anything, so it has no use for the
        module that does. A rule reaching for it at import time is the defect."""
        pulled_subprocess, _ = run(PROBE)
        assert pulled_subprocess == "False"

    def test_nor_is_the_live_package(self) -> None:
        """The broader statement: a live rule declares its tier and imports its
        machinery when it runs, not when it is registered."""
        _, pulled_live = run(PROBE)
        assert pulled_live == "False"


class TestTheProbeCanFail:
    """The control. An assertion that something is absent is worth nothing
    until the same probe is shown reporting it present."""

    def test_importing_the_live_layer_does_pull_subprocess(self) -> None:
        pulled_subprocess, pulled_live = run(CONTROL)
        assert pulled_subprocess == "True"
        assert pulled_live == "True"
