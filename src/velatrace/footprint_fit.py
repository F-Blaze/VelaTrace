"""Offline footprint fit: do the footprint's pads look like the real part's pads?

Pure geometry on two pad lists, no network. Only the most fundamental mismatch per part is
reported (pad count > mount type > layout > pitch > pin order) so one wrong footprint is one finding.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Sequence

from .audit_rules import part_kind
from .easyeda import RealPart
from .findings import Finding, Severity
from .models import Component, DesignSnapshot

# Starting value, not measured: KiCad and vendor land patterns of the same part differ by about
# 0.1 mm (SOIC-8 rows, for one), so anything tighter would flag correct footprints.
FIT_TOLERANCE_MM = 0.15
_PASSIVE_KINDS = {"resistor", "capacitor", "inductor", "ferrite", "resistor_array", "testpoint"}
# ponytail: brute-force nearest pad is O(8 * n^2); above this (huge BGAs) alignment is skipped
# and only count, mount type and layout are checked. Upgrade: grid lookup instead of a full scan.
_MAX_ALIGN_PADS = 400
_PIN_LABEL = re.compile(r"[A-Z]{0,2}\d+")

Pad = tuple[str, float, float, bool | None]   # number, x, y (mm, footprint frame), through-hole


def kicad_pads(comp: Component) -> list[Pad]:
    """Numbered pads with a known offset; unnumbered (mechanical) pads are not compared."""
    return [(pin.number, pin.offset_mm[0], pin.offset_mm[1], pin.through_hole)
            for pin in comp.pins if pin.number and pin.offset_mm is not None]


def fit_candidates(snapshot: DesignSnapshot) -> list[Component]:
    """Parts worth looking up: not a passive, with a footprint and at least one numbered pad.
    The snapshot does not carry DNP, so callers that know the DNP set filter it themselves."""
    return [comp for comp in snapshot.components
            if comp.footprint and part_kind(comp) not in _PASSIVE_KINDS and kicad_pads(comp)]


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


def _alignments(fp: Sequence[tuple[float, float]], real: Sequence[tuple[float, float]]):
    """For each of the 8 rotations/mirrors of the real pads: (worst offset, total offset,
    footprint pad -> nearest real pad index, offset per footprint pad)."""
    for mirror in (1, -1):
        for turn in range(4):
            cos, sin = round(math.cos(turn * math.pi / 2)), round(math.sin(turn * math.pi / 2))
            moved = [(mirror * x * cos - y * sin, mirror * x * sin + y * cos) for x, y in real]
            near = [_nearest(point, moved) for point in fp]
            back = max(_nearest(point, fp)[1] for point in moved)
            offsets = [distance for _, distance in near]
            yield max(max(offsets), back), sum(offsets), [i for i, _ in near], offsets


def _part_name(real: RealPart) -> str:
    return " ".join(item for item in (real.lcsc, real.title) if item) or "the part"


def _label(rows: int, cols: int) -> str:
    return f"{rows} × {cols}"


def compare(reference: str, kicad_pads: Iterable[Pad], real: RealPart) -> list[Finding]:
    """Findings (at most one) for a footprint whose pads do not fit the real part."""
    fp = _collapse(pad for pad in kicad_pads if pad[0].strip())
    part = _collapse((p.number, p.x_mm, p.y_mm, p.through_hole) for p in real.pads if p.number.strip())
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
    if not part:
        return []

    rows_cols, part_rows_cols = _rows_cols(fp), _rows_cols(part)
    if sorted(rows_cols) != sorted(part_rows_cols):
        return finding("fit.layout", f"pad layout differs from {name} (footprint "
                       f"{_label(*rows_cols)}, part {_label(*part_rows_cols)})",
                       f"The footprint's {len(fp)} pads form {rows_cols[0]} row(s) × {rows_cols[1]} column(s); "
                       f"the part's {len(part)} pads form {part_rows_cols[0]} row(s) × "
                       f"{part_rows_cols[1]} column(s).",
                       "Use the footprint for this part's package; the pin count matches but the pads "
                       "are arranged differently.")
    if len(fp) > _MAX_ALIGN_PADS:
        return []

    mine, theirs = _centred(fp), _centred(part)
    options = list(_alignments(mine, theirs))
    fits = [option for option in options if option[0] <= FIT_TOLERANCE_MM]
    if not fits:
        worst, _, _, offsets = min(options, key=lambda option: option[:2])
        pad = fp[offsets.index(max(offsets))][0]
        return finding("fit.pitch", f"pad positions differ from {name} by up to {worst:.2f} mm",
                       f"Best alignment still leaves pad {pad} {worst:.2f} mm off (tolerance "
                       f"{FIT_TOLERANCE_MM:.2f} mm). Pad pitch: footprint {_pitch(mine):.2f} mm, part "
                       f"{_pitch(theirs):.2f} mm.",
                       "Use the footprint with the part's pad pitch and spacing.")
    wrong = []
    for _, _, nearest, _ in fits:
        bad = [(fp[i][0], part[j][0]) for i, j in enumerate(nearest)
               if fp[i][0].upper() != part[j][0].upper()
               and _PIN_LABEL.fullmatch(fp[i][0].upper()) and _PIN_LABEL.fullmatch(part[j][0].upper())]
        if not bad:
            return []
        wrong.append(bad)
    bad = min(wrong, key=len)
    sample = ", ".join(f"footprint pad {a} sits where the part has pin {b}" for a, b in bad[:3])
    return finding("fit.pin_order", f"pad numbers differ from {name} ({len(bad)} of {len(fp)} pads)",
                   f"The pads line up with the part, but the numbering does not: {sample}"
                   f"{'; ...' if len(bad) > 3 else ''}.",
                   "Check pin 1 and the numbering direction against the datasheet; the footprint may "
                   "be rotated, mirrored or numbered differently from the part.", Severity.WARNING)
