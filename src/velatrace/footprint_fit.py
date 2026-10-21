"""Offline footprint fit: do the footprint's pads look like the real part's pads?

Pure geometry on two pad lists, no network. Only the most fundamental mismatch per part is
reported (pad count > mount type > layout > pitch > pin order) so one wrong footprint is one finding.

Library differences that are not wrong parts, and how they are left alone:
- Mounting pads: KiCad numbers them "0" or "MP" or leaves them unnumbered; EasyEDA gives them the
  next numbers (a 10-pin connector's tabs are 11 and 12). The footprint's mechanical pads are left
  out, and so are as many of the part's large pads (4 x the median pad area or more) whose numbers
  the footprint does not use. A part with mounting pads on a footprint without any still differs.
- Tabs and exposed pads: the largest pad, when it is 4 x the median or more, is left out of the
  layout and position checks (still counted). Libraries put its centre in different places: KiCad's
  SOT-89 anchors the tab pad next to the pins, EasyEDA at the middle of the tab.
- Row spacing: vendors move whole rows of pads in or out (toe/heel), so pad positions are checked
  tightly only along a line of 3+ pads (the pin pitch). Across it, and between the two pads of a
  2-pad part, the spacing may differ by up to 0.5 mm or 20 %, whichever is larger.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Sequence

from .audit_rules import part_kind
from .easyeda import RealPart
from .findings import Finding, Severity
from .models import Component, DesignSnapshot

# Starting values, not measured on unseen boards. Along a row of pads: the pin pitch varies by
# less than 0.15 mm between libraries. Across rows: on megadesk, KiCad and LCSC land patterns of the
# same parts differed by 0.46-0.70 mm (8-18 %) in row spacing; a different package (SOD-323 vs
# SOD-123, narrow vs wide SOIC) differs by 30 % or more.
FIT_TOLERANCE_MM = 0.15
ROW_SPACING_MM = 0.5
ROW_SPACING_SHARE = 0.2
_LARGE_PAD = 4.0    # x the median pad area: a tab, exposed pad or mounting pad
_MECHANICAL = {"", "0", "MP"}   # KiCad numbers of mounting pads
_PASSIVE_KINDS = {"resistor", "capacitor", "inductor", "ferrite", "resistor_array", "testpoint"}
# ponytail: brute-force nearest pad is O(8 * n^2); above this (huge BGAs) alignment is skipped
# and only count, mount type and layout are checked. Upgrade: grid lookup instead of a full scan.
_MAX_ALIGN_PADS = 400
_PIN_LABEL = re.compile(r"[A-Z]{0,2}\d+")

Pad = tuple[str, float, float, bool | None]   # number, x, y (mm, footprint frame), through-hole


def kicad_pads(comp: Component) -> list[Pad]:
    """Pads with a known offset; compare() leaves the mechanical ones out."""
    return [(pin.number, pin.offset_mm[0], pin.offset_mm[1], pin.through_hole)
            for pin in comp.pins if pin.offset_mm is not None]


def _mechanical(pad: Pad) -> bool:
    return pad[0].strip().upper() in _MECHANICAL


def fit_candidates(snapshot: DesignSnapshot) -> list[Component]:
    """Parts worth looking up: not a passive, with a footprint and at least one numbered pad.
    The snapshot does not carry DNP, so callers that know the DNP set filter it themselves."""
    return [comp for comp in snapshot.components
            if comp.footprint and part_kind(comp) not in _PASSIVE_KINDS
            and any(not _mechanical(pad) for pad in kicad_pads(comp))]


def _collapse(pads: Iterable[Pad]) -> list[Pad]:
    """One pad per number (thermal-via arrays and split tabs share a number): mean position."""
    groups: dict[str, list[Pad]] = {}
    for pad in pads:
        groups.setdefault(pad[0].strip(), []).append(pad)
    return [(number, sum(p[1] for p in group) / len(group), sum(p[2] for p in group) / len(group),
             None if all(p[3] is None for p in group) else any(p[3] for p in group))
            for number, group in groups.items()]


def _mostly_tht(pads: Sequence[Pad]) -> bool | None:
    known = [pad[3] for pad in pads if pad[3] is not None]
    return sum(known) * 2 > len(known) if known else None


def _clusters(values: Iterable[float]) -> int:
    ordered = sorted(values)
    return 1 + sum(b - a > FIT_TOLERANCE_MM for a, b in zip(ordered, ordered[1:])) if ordered else 0


def _rows_cols(pads: Sequence[Pad]) -> tuple[int, int]:
    return _clusters(p[2] for p in pads), _clusters(p[1] for p in pads)


def _centred(pads: Sequence[Pad]) -> list[tuple[float, float]]:
    cx, cy = sum(p[1] for p in pads) / len(pads), sum(p[2] for p in pads) / len(pads)
    return [(p[1] - cx, p[2] - cy) for p in pads]


def _pitch(points: Sequence[tuple[float, float]]) -> float:
    """Typical spacing: median distance from each pad to its nearest neighbour."""
    if len(points) < 2:
        return 0.0
    nearest = sorted(min(math.dist(a, b) for j, b in enumerate(points) if j != i)
                     for i, a in enumerate(points))
    return nearest[len(nearest) // 2]


def _nearest(point: tuple[float, float], points: Sequence[tuple[float, float]]) -> tuple[int, float]:
    return min(((i, math.dist(point, other)) for i, other in enumerate(points)), key=lambda item: item[1])


def _allowed(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Allowed (x, y) offset per pad: tight along a line of 3+ pads, loose across it."""
    def loose(c: float) -> float:   # half the row-spacing limit: both rows move
        return max(ROW_SPACING_MM, ROW_SPACING_SHARE * 2 * abs(c)) / 2

    def in_line(axis: int, value: float) -> int:
        return sum(abs(other[axis] - value) <= FIT_TOLERANCE_MM for other in points)
    return [(FIT_TOLERANCE_MM if in_line(1, y) >= 3 else loose(x), FIT_TOLERANCE_MM if in_line(0, x) >= 3 else loose(y))
            for x, y in points]


def _alignments(fp: Sequence[tuple[float, float]], real: Sequence[tuple[float, float]]):
    """For each of the 8 rotations/mirrors of the real pads: (worst excess over the allowed offset,
    total offset, footprint pad -> nearest real pad index, offset per footprint pad, mirrored)."""
    allowed = _allowed(fp)
    for mirror in (1, -1):
        for turn in range(4):
            cos, sin = round(math.cos(turn * math.pi / 2)), round(math.sin(turn * math.pi / 2))
            moved = [(mirror * x * cos - y * sin, mirror * x * sin + y * cos) for x, y in real]
            near = [_nearest(point, moved) for point in fp]
            index = [i for i, _ in near]
            excess = max(max(abs(a - b) - tol for a, b, tol in zip(point, moved[i], limit))
                         for point, i, limit in zip(fp, index, allowed))
            if len(set(index)) < len(index):   # two footprint pads on one real pad: no fit
                excess = math.inf
            yield excess, sum(d for _, d in near), index, [d for _, d in near], mirror < 0


def _large_pads(real: RealPart) -> list[str]:
    """Numbers of the part's pads with 4 x the median pad area or more, largest first."""
    area: dict[str, float] = {}
    for pad in real.pads:
        area[pad.number.strip()] = area.get(pad.number.strip(), 0.0) + pad.area_mm2
    sizes = sorted(area.values())
    median = sizes[len(sizes) // 2] if sizes else 0.0
    return sorted((n for n, a in area.items() if n and median and a >= _LARGE_PAD * median),
                  key=area.__getitem__, reverse=True)


def _part_name(real: RealPart) -> str:
    return " ".join(item for item in (real.lcsc, real.title) if item) or "the part"


def _label(rows: int, cols: int) -> str:
    return f"{rows} × {cols}"


def compare(reference: str, kicad_pads: Iterable[Pad], real: RealPart) -> list[Finding]:
    """Findings (at most one) for a footprint whose pads do not fit the real part."""
    pads = list(kicad_pads)
    fp = _collapse(pad for pad in pads if not _mechanical(pad))
    part = _collapse((p.number, p.x_mm, p.y_mm, p.through_hole) for p in real.pads if p.number.strip())
    large = _large_pads(real)
    used = {pad[0] for pad in fp}
    mounting = [number for number in large if number not in used][:sum(map(_mechanical, pads))]
    part = [pad for pad in part if pad[0] not in mounting]
    tab = next((number for number in large if number in used), None) if len(part) > 2 else None
    part_count = len(part) or real.pin_count
    if not fp or not part_count:
        return []
    name = _part_name(real)
    blind = "" if part else " The part's pad layout is not available, so the pad positions were not compared."

    def finding(rule: str, title: str, evidence: str, fix: str, severity=Severity.ERROR) -> list[Finding]:
        return [Finding(rule, severity, f"{reference} {title}", (reference,), (), evidence + blind, fix)]

    if len(fp) != part_count:
        return finding("fit.pad_count", f"footprint has {len(fp)} pads but {name} has {part_count}",
                       f"The footprint has {len(fp)} numbered pads; the part's data lists {part_count}.",
                       "Check that this is the right part for this footprint, or pick the footprint "
                       "that matches the part's package.")
    here, there = _mostly_tht(fp), real.through_hole if not part else _mostly_tht(part)
    if here is not None and there is not None and here != there:
        mount = {True: "through-hole", False: "surface-mount"}
        return finding("fit.mount_type", f"footprint is {mount[here]} but {name} is {mount[there]}",
                       f"The footprint's pads are {mount[here]}; the part's pads are {mount[there]}.",
                       "Use a footprint with the same mounting type as the part.")
    count = len(fp)
    fp, part = [pad for pad in fp if pad[0] != tab], [pad for pad in part if pad[0] != tab]
    rows_cols = _rows_cols(fp)
    if part:
        part_rows_cols = _rows_cols(part)
    elif real.rows and part_count % real.rows == 0:   # package-only part that states its rows
        part_rows_cols = (real.rows, part_count // real.rows)
    else:
        return []
    if sorted(rows_cols) != sorted(part_rows_cols):
        return finding("fit.layout", f"pad layout differs from {name} (footprint "
                       f"{_label(*rows_cols)}, part {_label(*part_rows_cols)})",
                       f"The footprint's {count} pads form {rows_cols[0]} row(s) × {rows_cols[1]} column(s); "
                       f"the part's {part_count} pads form {part_rows_cols[0]} row(s) × "
                       f"{part_rows_cols[1]} column(s).",
                       "Use the footprint for this part's package; the pin count matches but the pads "
                       "are arranged differently.")
    if not part or len(fp) > _MAX_ALIGN_PADS:
        return []

    mine, theirs = _centred(fp), _centred(part)
    options = list(_alignments(mine, theirs))
    fits = [option for option in options if option[0] <= 0]
    if not fits:
        _, _, _, offsets, _ = min(options, key=lambda option: option[:2])
        worst = max(offsets)
        pad = fp[offsets.index(worst)][0]
        return finding("fit.pitch", f"pad positions differ from {name} by up to {worst:.2f} mm",
                       f"Best alignment still leaves pad {pad} {worst:.2f} mm off (allowed: "
                       f"{FIT_TOLERANCE_MM:.2f} mm along a row of pads; {ROW_SPACING_MM:.1f} mm or "
                       f"{ROW_SPACING_SHARE:.0%} in row spacing). Pad pitch: footprint {_pitch(mine):.2f} mm, "
                       f"part {_pitch(theirs):.2f} mm.",
                       "Use the footprint with the part's pad pitch and spacing.")
    # Both sides are top views (bottom-side footprints are un-mirrored on read), so a mirror may
    # explain the geometry but never the numbering: a mirror-image pinout is a real mistake.
    wrong = []
    for _, _, nearest, _, mirrored in fits:
        bad = [(fp[i][0], part[j][0]) for i, j in enumerate(nearest)
               if fp[i][0].upper() != part[j][0].upper()
               and _PIN_LABEL.fullmatch(fp[i][0].upper()) and _PIN_LABEL.fullmatch(part[j][0].upper())]
        if not bad and not mirrored:
            return []
        wrong.append(bad)
    bad = min(wrong, key=len)
    if not bad:
        return finding("fit.pin_order", f"footprint is a mirror image of {name}",
                       "The pads and numbers match the part only when flipped left-to-right, so pin 1 "
                       "and the numbering run the wrong way round.",
                       "Use a footprint drawn from the top view, as in the part's datasheet.", Severity.WARNING)
    sample = ", ".join(f"footprint pad {a} sits where the part has pin {b}" for a, b in bad[:3])
    return finding("fit.pin_order", f"pad numbers differ from {name} ({len(bad)} of {count} pads)",
                   f"The pads line up with the part, but the numbering does not: {sample}"
                   f"{'; ...' if len(bad) > 3 else ''}.",
                   "Check pin 1 and the numbering direction against the datasheet; the footprint may "
                   "be rotated, mirrored or numbered differently from the part.", Severity.WARNING)
