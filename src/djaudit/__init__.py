"""djaudit — Django-aware static analysis.

Audits an existing Django codebase and emits ranked, evidence-backed findings
across settings hardening, injection, DRF authorization, ORM performance,
migration safety and cross-database portability.
"""

from djaudit.models import (
    Confidence,
    Evidence,
    EvidenceKind,
    Family,
    Finding,
    Location,
    Severity,
    Tier,
)

__version__ = "0.3.0"

__all__ = [
    "Confidence",
    "Evidence",
    "EvidenceKind",
    "Family",
    "Finding",
    "Location",
    "Severity",
    "Tier",
    "__version__",
]
