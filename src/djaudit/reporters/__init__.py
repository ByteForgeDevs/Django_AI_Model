"""Output formats."""

from __future__ import annotations

from enum import StrEnum


class OutputFormat(StrEnum):
    TERMINAL = "terminal"
    JSON = "json"
    SARIF = "sarif"
    HTML = "html"


__all__ = ["OutputFormat"]
