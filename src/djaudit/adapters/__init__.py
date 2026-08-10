"""External tool adapters.

Nothing an external tool says reaches a report without a written decision
about it. See `djaudit.adapters.base` for why, and for the corpus measurement
that made the decision unavoidable.
"""

from djaudit.adapters.base import (
    Adapter,
    Availability,
    Claim,
    Claimed,
    ClaimTable,
    External,
    Report,
    as_finding,
    is_test_path,
)
from djaudit.adapters.merge import Merged, identified, merge
from djaudit.adapters.pip_audit import PipAuditAdapter
from djaudit.adapters.process import probe_tool, read_version, run_tool
from djaudit.adapters.ruff import RuffAdapter


def every() -> tuple[Adapter, ...]:
    """Every adapter a run can be asked for, in the order they are run.

    ruff comes first because it is local, fast and always safe. `pip-audit`
    is last because it is the one adapter that reaches the network: a
    vulnerability database is not something we can carry, and a caller who
    stops the run partway through still gets the tool that costs nothing.
    """
    return (RuffAdapter(), PipAuditAdapter())


REACHES_THE_NETWORK = frozenset({"pip-audit"})
"""Adapters that query a remote service, named so a disclosure can name them.

Written as data rather than as prose in the CLI so that adding an adapter that
phones home cannot quietly leave the disclosure describing the old set.
"""

__all__ = [
    "REACHES_THE_NETWORK",
    "Adapter",
    "Availability",
    "Claim",
    "ClaimTable",
    "Claimed",
    "External",
    "Merged",
    "PipAuditAdapter",
    "Report",
    "RuffAdapter",
    "as_finding",
    "every",
    "identified",
    "is_test_path",
    "merge",
    "probe_tool",
    "read_version",
    "run_tool",
]
