"""Deterministic suggestion flags. Nothing here edits a design."""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from math import dist, isfinite

from .models import Component
from .errors import ValidationError


class Bucket(str, Enum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    NICE_TO_HAVE = "nice-to-have"
    REDUNDANT = "redundant"


@dataclass(frozen=True)
class Function:
    text: str
    role: str
    confidence: float


@dataclass(frozen=True)
class Verdict:
    reference: str
    bucket: Bucket
    confidence: float
    suggestion: str


@dataclass(frozen=True)
class Flag:
    reference: str
    reason: str
    possible: bool = False
    duplicate_of: str | None = None


def _norm(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def redundancy_flags(components: tuple[Component, ...],
                     functions: dict[str, Function], proximity_mm: float = 5.0) -> list[Flag]:
    """Require identical pin-to-net maps, value, footprint/type, and confirmed role.

    Unknown/conflicting roles or unavailable/distant placement downgrade matching
    candidates to possible. Different value or footprint is never sufficient for
    a duplicate flag, regardless of what an LLM claims.
    """
    if type(proximity_mm) not in {int, float} or not isfinite(proximity_mm) or proximity_mm <= 0:
        raise ValidationError("Proximity must be finite and positive.")
    groups: dict[tuple, list[Component]] = {}
    for comp in components:
        if (not comp.value.strip() or not comp.footprint.strip() or not comp.pins
                or any(not pin.net or not pin.number for pin in comp.pins)):
            continue
        key = (_norm(comp.value), _norm(comp.footprint), _norm(comp.kind),
               tuple(sorted((pin.number, pin.net) for pin in comp.pins)))
        groups.setdefault(key, []).append(comp)
    flags = []
    for group in groups.values():
        if len(group) < 2:
            continue
        original = group[0]
        first = functions.get(original.reference)
        for comp in group[1:]:
            other = functions.get(comp.reference)
            roles = {_norm(first.role) if first else "unknown",
                     _norm(other.role) if other else "unknown"}
            # Deliberately exclude distinct energy storage/local decoupling roles.
            if any("bulk" in role for role in roles) and any("decoupl" in role for role in roles):
                continue
            same_role = bool(first and other and _norm(first.role) == _norm(other.role)
                             and _norm(first.role) not in {"", "unknown"})
            near = (original.position_mm is not None and comp.position_mm is not None
                    and dist(original.position_mm, comp.position_mm) <= proximity_mm)
            confident = first and other and min(first.confidence, other.confidence) >= 0.8
            possible = not (same_role and near and confident)
            flags.append(Flag(comp.reference,
                              "possible redundancy — verify" if possible else
                              "Matching connectivity, value, footprint/type and role; nearby duplicate — verify",
                              possible, original.reference))
    return flags


def compute_flags(components: tuple[Component, ...], functions: dict[str, Function],
                  verdicts: dict[str, Verdict]) -> list[Flag]:
    flags = {flag.reference: flag for flag in redundancy_flags(components, functions)}
    for reference, flag in tuple(flags.items()):
        if verdicts[reference].bucket in {Bucket.CRITICAL, Bucket.IMPORTANT}:
            flags[reference] = Flag(reference, "possible redundancy — verify", True,
                                    flag.duplicate_of)
    for comp in components:
        verdict = verdicts[comp.reference]
        if comp.reference in flags:
            continue
        if verdict.bucket == Bucket.REDUNDANT:
            # Model assertion cannot create flags without hard matching facts.
            continue
        elif verdict.bucket == Bucket.NICE_TO_HAVE:
            flags[comp.reference] = Flag(comp.reference, "Optional function — verify before changing design")
        elif verdict.confidence < 0.8:
            flags[comp.reference] = Flag(comp.reference, "Borderline classification — verify", True)
    return list(flags.values())


def flagged_total(prices: dict[str, Decimal]) -> Decimal:
    if any(not value.is_finite() or value < 0 for value in prices.values()):
        raise ValidationError("Prices must be finite nonnegative amounts in one currency.")
    return sum(prices.values(), Decimal("0"))


def candidate_savings(flags: list[Flag], prices: dict[str, Decimal]) -> Decimal:
    """Hypothetical maximum, never a recommendation to remove critical parts."""
    return flagged_total({flag.reference: prices[flag.reference] for flag in flags
                          if not flag.possible and flag.reference in prices})
