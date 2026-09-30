"""Actionable audit findings shared by rule checks, BOM checks and the UI."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"      # likely functional/manufacturing defect
    WARNING = "warning"  # risky or non-standard
    SAVING = "saving"    # cost or time can be saved
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    rule: str                      # stable id, e.g. "decoupling.missing"
    severity: Severity
    title: str                     # one line, e.g. "U3 has no decoupling capacitor on +3V3"
    refs: tuple[str, ...] = ()     # component references involved
    nets: tuple[str, ...] = ()     # nets involved
    evidence: str = ""             # what in the design shows this (pins/nets/values)
    fix: str = ""                  # concrete suggested change
    cost_delta: Decimal | None = None  # per-board cost change in USD; negative = saving
