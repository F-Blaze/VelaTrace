"""The JLCPCB view of a design: BOM lines with tier, stock and price, a cost estimate and
stock findings. Pure and offline: it combines the snapshot with whatever catalogue and live
answers are already on disk. Fee assumptions are the ones documented in docs/jlcpcb.md.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
import re
from typing import Iterable

from .bom import (DEFAULT_BOARDS_PER_ORDER, JLC_EXTENDED_FEE_USD, JLC_FEE_CHECKED, _LCSC_FIELDS,
                  _field_key, _ref_key, _ref_list, bom_findings, lcsc_code, mpn_of, package_of,
                  suggested_parts)
from .findings import Finding, Severity
from .jlc_catalog import best_parts_db
from .jlc_live import LivePart, load_live
from .models import DesignSnapshot

ESTIMATE_BOARDS = (5, 10, 30, 100)
# "Low" = not enough JLCPCB stock to build this many boards of the design.
LOW_STOCK_BOARDS = 100
_ECONOMIC = ("basic", "preferred")


@dataclass(frozen=True)
class BomLine:
    refs: tuple[str, ...]
    value: str
    footprint: str
    lcsc: str = ""                 # "" = no part number assigned
    mpn: str = ""
    part: object | None = None     # catalogue row (CatalogPart, or Part from the small list)
    live: LivePart | None = None
    suggestion: str = ""           # Basic/Preferred part number that matches an unassigned passive
    known_tiers: bool = False      # a parts list was available to classify the line
    catalogue_label: str = ""      # where `part` came from, with its date

    @property
    def quantity(self) -> int:
        return len(self.refs)

    @property
    def package(self) -> str:
        return (getattr(self.part, "package", "") or package_of(self.footprint)
                or self.footprint.rsplit(":", 1)[-1])

    @property
    def tier(self) -> str:
        """basic | preferred | extended | unlisted | "" (unknown or unassigned)."""
        if not self.lcsc:
            return ""
        if self.live is not None and not self.live.found:
            return "unlisted"
        # Live is newer for Basic/Extended but cannot tell Preferred; the catalogue can.
        if self.live is not None and self.live.library:
            preferred = self.live.library == "extended" and getattr(self.part, "tier", "") == "preferred"
            return "preferred" if preferred else self.live.library
        if self.part is not None:
            return self.part.tier
        return "extended" if self.known_tiers else ""

    @property
    def stock(self) -> int | None:
        if self.live is not None:
            return self.live.stock if self.live.found else 0
        return getattr(self.part, "stock", None)

    def unit_price(self, boards: int = DEFAULT_BOARDS_PER_ORDER) -> Decimal | None:
        """USD each at the quantity this many boards need, from real price breaks."""
        for source in (self.live if self.live is not None and self.live.found else None, self.part):
            if source is not None and hasattr(source, "unit_price"):
                if (price := source.unit_price(self.quantity * boards)) is not None:
                    return price
        return None

    @property
    def source(self) -> str:
        return self.live.label if self.live is not None else self.catalogue_label

    @property
    def fee(self) -> bool:
        """This line costs one Extended loading fee per order."""
        return self.tier in ("extended", "unlisted")


@dataclass(frozen=True)
class CostRow:
    boards: int
    parts_per_board: Decimal
    fees_per_order: Decimal

    @property
    def per_board(self) -> Decimal:
        return (self.parts_per_board + self.fees_per_order / self.boards).quantize(Decimal("0.01"))


def with_assignments(snapshot: DesignSnapshot, assignments: dict[str, str]) -> DesignSnapshot:
    """The snapshot as it reads once these part numbers are on the footprints: every field
    that holds a part number today is overwritten, else an LCSC field is added."""
    if not assignments:
        return snapshot
    components = []
    for comp in snapshot.components:
        code = assignments.get(comp.reference)
        if code:
            fields = dict(comp.fields)
            holding = [name for name, value in fields.items()
                       if isinstance(name, str) and _field_key(name) in _LCSC_FIELDS
                       and isinstance(value, str) and re.fullmatch(r"C\d{1,9}", value.strip().upper())]
            fields.update(dict.fromkeys(holding or ["LCSC"], code))
            comp = replace(comp, fields=fields)
        components.append(comp)
    return replace(snapshot, components=tuple(components))


def bom_table(snapshot: DesignSnapshot, db=None, live: dict[str, LivePart] | None = None, *,
              excluded: Iterable[str] = ()) -> list[BomLine]:
    """One row per (LCSC code, value, footprint), like the BOM JLCPCB receives.
    `excluded`: references marked DNP or exclude-from-BOM (the snapshot does not carry them)."""
    skip = set(excluded)
    groups: dict[tuple, list] = defaultdict(list)
    for comp in snapshot.components:
        if comp.reference in skip or comp.reference.startswith("#") or not comp.footprint:
            continue
        groups[(lcsc_code(comp) or "", comp.value.strip(), comp.footprint)].append(comp)
    suggestions = suggested_parts(snapshot, db) if db is not None else {}
    label = f"catalogue {db.label}" if db is not None else ""
    lines = []
    for (code, value, footprint), comps in groups.items():
        refs = tuple(sorted((c.reference for c in comps), key=_ref_key))
        hint = {suggestions[ref].lcsc for ref in refs if ref in suggestions}
        part = db.part(code) if db is not None and code else None
        lines.append(BomLine(refs, value, footprint, code,
                             next((m for m in map(mpn_of, comps) if m), ""), part,
                             (live or {}).get(code) if code else None,
                             next(iter(hint)) if len(hint) == 1 and not code else "",
                             db is not None, label if part is not None else ""))
    return sorted(lines, key=lambda line: _ref_key(line.refs[0]))


def cost_estimate(lines: Iterable[BomLine], boards: Iterable[int] = ESTIMATE_BOARDS) -> list[CostRow]:
    """Parts at their price break plus Extended loading fees. Lines without a price add 0."""
    lines = list(lines)
    fees = JLC_EXTENDED_FEE_USD * sum(1 for line in lines if line.fee)
    return [CostRow(count, sum(((line.unit_price(count) or Decimal(0)) * line.quantity
                                for line in lines), Decimal(0)), fees) for count in boards]


def stock_findings(lines: Iterable[BomLine], *,
                   boards: int = DEFAULT_BOARDS_PER_ORDER) -> list[Finding]:
    """jlc.out_of_stock, jlc.low_stock and jlc.cost_estimate from stock/prices already on disk."""
    lines = [line for line in lines if line.lcsc]
    findings = []
    for line in lines:
        stock, need = line.stock, line.quantity * boards
        if stock is None:
            continue
        refs, where = _ref_list(line.refs, 4), line.source or "catalogue"
        refresh = "" if line.live is not None else " Click Refresh JLCPCB stock/prices to confirm."
        if line.live is not None and not line.live.found:
            findings.append(Finding(
                "jlc.out_of_stock", Severity.WARNING,
                f"{refs}: JLCPCB does not list {line.lcsc}", refs=line.refs,
                evidence=f"JLCPCB's parts service has no part {line.lcsc} ({where}).",
                fix="Check the LCSC field for a typo or pick a current part."))
        elif stock == 0 or (line.live is not None and not line.live.buyable):
            findings.append(Finding(
                "jlc.out_of_stock", Severity.WARNING,
                f"{refs}: {line.lcsc} is out of stock at JLCPCB", refs=line.refs,
                evidence=(f"{line.lcsc} ({line.value}): stock {stock}, {where}. The design needs "
                          f"{need} for {boards} boards." + refresh),
                fix="Pick an in-stock equivalent, pre-order the part at JLCPCB, or mark it DNP "
                    "and fit it yourself."))
        elif stock < line.quantity * LOW_STOCK_BOARDS:
            findings.append(Finding(
                "jlc.low_stock", Severity.WARNING if stock < need else Severity.INFO,
                f"{refs}: only {stock} of {line.lcsc} in stock at JLCPCB", refs=line.refs,
                evidence=(f"{line.lcsc} ({line.value}): stock {stock}, {where}. Enough for "
                          f"{stock // line.quantity} board(s) ({line.quantity} per board); the "
                          f"threshold is {LOW_STOCK_BOARDS} boards." + refresh),
                fix="Order soon or choose a part with deeper stock."))
    priced = [line for line in lines if line.unit_price(boards) is not None]
    if priced:
        rows = cost_estimate(lines)
        live = sum(1 for line in priced if line.live is not None)
        unpriced = len(lines) - len(priced)
        fee_lines = sum(1 for line in lines if line.fee)
        findings.append(Finding(
            "jlc.cost_estimate", Severity.INFO,
            "JLCPCB parts cost per board: " + ", ".join(
                f"${row.per_board} at {row.boards}" for row in rows),
            evidence=(f"{len(priced)} BOM line(s) with an LCSC code priced from their own quantity "
                      f"breaks ({live} live, {len(priced) - live} from the catalogue), plus "
                      f"{fee_lines} Extended line(s) × ${JLC_EXTENDED_FEE_USD} loading fee per order "
                      f"(fee checked {JLC_FEE_CHECKED}), spread over the boards. "
                      + (f"{unpriced} line(s) have no price and count as $0. " if unpriced else "")
                      + "Not included: lines without an LCSC code, the PCB, assembly set-up, "
                        "stencil and per-joint fees, attrition, shipping and tax."),
            fix="Use the JLCPCB quote for the real total; this compares part choices only."))
    return findings


def design_findings(snapshot: DesignSnapshot, config_dir: Path) -> list[Finding]:
    """Everything the JLCPCB data on disk says about this design. Offline."""
    db = best_parts_db(config_dir)
    try:
        return bom_findings(snapshot, db) + stock_findings(
            bom_table(snapshot, db, load_live(config_dir)))
    finally:
        if hasattr(db, "close"):
            db.close()
