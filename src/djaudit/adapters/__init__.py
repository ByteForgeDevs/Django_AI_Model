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
from djaudit.adapters.process import probe_tool, read_version, run_tool

__all__ = [
    "Adapter",
    "Availability",
    "Claim",
    "ClaimTable",
    "Claimed",
    "External",
    "Report",
    "as_finding",
    "is_test_path",
    "probe_tool",
    "read_version",
    "run_tool",
]
