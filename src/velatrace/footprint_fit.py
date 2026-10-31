"""Offline footprint fit: do the footprint's pads look like the real part's pads?

Pure geometry on two pad lists, no network. Only the most fundamental mismatch per part is
reported (pad count > mount type > layout > pitch > pin order) so one wrong footprint is one finding.

Library differences that are not wrong parts, and how they are left alone:
- Physical pads, not numbers: pads at one spot (KiCad stacks USB-C A1 and B12, EasyEDA draws one
  "A1B12") are one pad, and so are pads sharing a number less than 2.5 mm apart (thermal vias, a
  split tab). The same number farther apart is separate legs (a tact switch's two pads "1").
- Mounting pads: KiCad numbers them "0", "MP" or "SH" or leaves them unnumbered; EasyEDA gives them
  the next numbers (a 10-pin connector's tabs are 11 and 12, a USB-C shell 1-4). When both sides
  have them they are counted and placed like any pad. Footprint mechanical pads the part has no
  pads for (paste-only or unnumbered pads) are left out, together with as many of the part's
  largest pads whose numbers the footprint does not use. A part with mounting pads on a footprint
  without any still differs.
- Tabs and exposed pads: the largest pad, when it is 4 x the median or more, is left out of the
  layout and position checks (still counted). Libraries put its centre in different places: KiCad's
  SOT-89 anchors the tab pad next to the pins, EasyEDA at the middle of the tab.
- Row spacing: vendors move whole rows of pads in or out (toe/heel), so pad positions are checked
  tightly only along a line of 3+ pads (the pin pitch). Across it, and between the two pads of a
  2-pad part, the spacing may differ by up to 0.65 mm or 20 %, whichever is larger.
- Rows x columns are only reported when the pads do not line up: rows a hair apart (3.0 and 3.15 mm)
  can cluster differently on the two sides.
- Pad areas: where pad sizes are known (KiCad pads' copper, not the part's), a part pad whose centre
  lands inside a footprint pad (0.25 mm margin) is served by it. Several part pads in one footprint
  pad are one merged pad (a power SON's drain tab: no pad_count), and a 2-pad part whose pad
  centres land inside the footprint's pads fits however long its toes are (no pitch).
- Pin order: only numbers both sides use count, and not numbers the footprint repeats (joined legs),
  so a tab numbered 2 in KiCad and 4 at LCSC is a name, not a swap. When the pad nets are known, a
  swap whose pins share a net (a mirrored header with paired pins) or whose pin is unconnected
  (a trimmer used as a rheostat) changes nothing and is skipped, and so is any numbering of four
  pads on two nets, two each (a tact switch whose pairs are joined inside).
"""
from __future__ import annotations

from collections import Counter
import math
from typing import Iterable, Sequence

from .audit_rules import part_kind
from .easyeda import RealPart
from .findings import Finding, Severity
from .models import Component, DesignSnapshot

# Starting values, not measured on unseen boards. Along a row of pads: the pin pitch varies by
# less than 0.15 mm between libraries. Across rows: on megadesk, KiCad and LCSC land patterns of the
# same parts differed by 0.46-0.70 mm (8-18 %) in row spacing, KiCad's SOT-23 (1.875 mm) and LCSC's
# (2.47 mm) by 0.6 mm; a different package (SOD-323 vs SOD-123, narrow vs wide SOIC) differs by
# 0.8 mm and 30 % or more.
FIT_TOLERANCE_MM = 0.15
ROW_SPACING_MM = 0.65
ROW_SPACING_SHARE = 0.2
_LARGE_PAD = 4.0    # x the median pad area: a tab, exposed pad or mounting pad
_SAME_PAD_MM = 2.5  # same number closer than this: one pad; separate legs are 2.54 mm apart or more
_MECHANICAL = {"", "0", "MP", "SH"}   # KiCad numbers of mounting pads
_PASSIVE_KINDS = {"resistor", "capacitor", "inductor", "ferrite", "resistor_array", "testpoint"}
# ponytail: brute-force nearest pad is O(8 * n^2); above this (huge BGAs) alignment is skipped
# and only count, mount type and layout are checked. Upgrade: grid lookup instead of a full scan.
_MAX_ALIGN_PADS = 400

_AREA_MARGIN_MM = 0.25   # a part pad's centre may sit this far outside the footprint pad's copper

# number, x, y (mm, footprint frame), through-hole, optionally the pad's (width, height) in that frame
Pad = tuple[str, float, float, bool | None] | tuple[str, float, float, bool | None, tuple[float, float] | None]
# one physical pad: "A1/B12", x, y, THT, area, the copper rectangles (x, y, width, height) behind it
Group = tuple[str, float, float, bool | None, float, tuple[tuple[float, float, float, float], ...]]


def kicad_pads(comp: Component) -> list[Pad]:
    """Pads with a known offset, mechanical ones included."""
    return [(pin.number, pin.offset_mm[0], pin.offset_mm[1], pin.through_hole)
            + ((pin.size_mm,) if pin.size_mm else ())
            for pin in comp.pins if pin.offset_mm is not None]


def pad_nets(comp: Component) -> dict[str, str]:
    """Pad number -> net, for compare()."""
    return {pin.number.strip().upper(): pin.net for pin in comp.pins}


def _mechanical(pad: Sequence) -> bool:
    return all(number.strip().upper() in _MECHANICAL for number in pad[0].split("/"))


def fit_candidates(snapshot: DesignSnapshot) -> list[Component]:
    """Parts worth looking up: not a passive, with a footprint and at least one numbered pad.
    The snapshot does not carry DNP, so callers that know the DNP set filter it themselves."""
    return [comp for comp in snapshot.components
            if comp.footprint and part_kind(comp) not in _PASSIVE_KINDS
            and any(not _mechanical(pad) for pad in kicad_pads(comp))]


def _group(pads: Sequence[tuple]) -> list[Group]:
    """Physical pads from (number, x, y, through_hole[, area or (width, height)]) records: one per
    spot, and one per run of same-number pads less than _SAME_PAD_MM apart (mean position, summed
    area). Mounting pads share a number by habit, not because they are one pad: they merge by spot only."""
    parent = list(range(len(pads)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    spots: dict[tuple[float, float], int] = {}
    numbers: dict[str, list[int]] = {}
    for i, (number, x, y, *_) in enumerate(pads):
        spot = (round(x, 2), round(y, 2))
        if spot in spots:
            parent[root(i)] = root(spots[spot])
        spots.setdefault(spot, i)
        same = numbers.setdefault(number.strip().upper(), [])
        for j in same:
            if number.strip().upper() not in _MECHANICAL and math.dist((x, y), pads[j][1:3]) < _SAME_PAD_MM:
                parent[root(i)] = root(j)
        same.append(i)
    runs: dict[int, list[tuple]] = {}
    for i, pad in enumerate(pads):
        runs.setdefault(root(i), []).append(pad)
    return [("/".join(sorted({p[0].strip().upper() for p in run})),
             sum(p[1] for p in run) / len(run), sum(p[2] for p in run) / len(run),
             None if all(p[3] is None for p in run) else any(p[3] for p in run),
             sum(p[4] for p in run if len(p) > 4 and not isinstance(p[4], tuple)),
             tuple((p[1], p[2], *p[4]) for p in run if len(p) > 4 and isinstance(p[4], tuple)))
            for run in runs.values()]


def _mostly_tht(pads: Sequence[Group]) -> bool | None:
    known = [pad[3] for pad in pads if pad[3] is not None]
    return sum(known) * 2 > len(known) if known else None


def _clusters(values: Iterable[float]) -> int:
    ordered = sorted(values)
    return 1 + sum(b - a > FIT_TOLERANCE_MM for a, b in zip(ordered, ordered[1:])) if ordered else 0


def _rows_cols(pads: Sequence[Group]) -> tuple[int, int]:
    return _clusters(p[2] for p in pads), _clusters(p[1] for p in pads)


def _centred(pads: Sequence[Group]) -> list[tuple[float, float]]:
    """Positions from the middle of the pads' bounding box: unlike the mean, that stays halfway
    between two rows however many pads each row has (SOT-23: two pads on one side, one opposite)."""
    xs, ys = [p[1] for p in pads], [p[2] for p in pads]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
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


def _moves(real: Sequence[tuple[float, float]]):
    """The real pads' positions under each of the 8 rotations/mirrors: (positions, mirrored)."""
    for mirror in (1, -1):
        for turn in range(4):
            cos, sin = round(math.cos(turn * math.pi / 2)), round(math.sin(turn * math.pi / 2))
            yield [(mirror * x * cos - y * sin, mirror * x * sin + y * cos) for x, y in real], mirror < 0


def _alignments(fp: Sequence[tuple[float, float]], real: Sequence[tuple[float, float]]):
    """For each of the 8 rotations/mirrors of the real pads: (worst excess over the allowed offset,
    total offset, footprint pad -> nearest real pad index, offset per footprint pad, mirrored)."""
    allowed = _allowed(fp)
    for moved, mirrored in _moves(real):
        near = [_nearest(point, moved) for point in fp]
        index = [i for i, _ in near]
        excess = max(max(abs(a - b) - tol for a, b, tol in zip(point, moved[i], limit))
                     for point, i, limit in zip(fp, index, allowed))
        if len(set(index)) < len(index):   # two footprint pads on one real pad: no fit
            excess = math.inf
        yield excess, sum(d for _, d in near), index, [d for _, d in near], mirrored


def _served(fp: Sequence[Group], part: Sequence[Group]) -> list[tuple]:
    """Alignments (shaped like _alignments, without offsets) in which every part pad's centre lies
    inside the copper of some footprint pad and every footprint pad serves one. Needs the sizes of
    all footprint pads. The index per footprint pad prefers the part pad with its own number."""
    if not part or not fp or any(not pad[5] for pad in fp):
        return []
    def middle(points):
        xs, ys = zip(*points)
        return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    here = middle([(p[1], p[2]) for p in fp])
    result = []
    for moved, mirrored in _moves([(p[1], p[2]) for p in part]):
        there = middle(moved)   # both libraries put the origin mid-part, else the bounding boxes are lined up
        for shift in ((0.0, 0.0), (here[0] - there[0], here[1] - there[1])):
            at = [(x + shift[0], y + shift[1]) for x, y in moved]
            hits = [[j for j, (x, y) in enumerate(at)
                     if any(abs(x - rx) <= w / 2 + _AREA_MARGIN_MM and abs(y - ry) <= h / 2 + _AREA_MARGIN_MM
                            for rx, ry, w, h in pad[5])] for pad in fp]
            if all(hits) and set().union(*hits) == set(range(len(at))):
                index = [min(hit, key=lambda j: (part[j][0] != pad[0], math.dist(at[j], pad[1:3])))
                         for hit, pad in zip(hits, fp)]
                result.append((0.0, 0.0, index, [], mirrored))
                break
    return result


def _large_pads(part: Sequence[Group]) -> list[str]:
    """Numbers of the part's pads with 4 x the median pad area or more, largest first."""
    sizes = sorted(pad[4] for pad in part)
    median = sizes[len(sizes) // 2] if sizes else 0.0
    return [pad[0] for pad in sorted(part, key=lambda pad: pad[4], reverse=True)
            if median and pad[4] >= _LARGE_PAD * median]


def _part_name(real: RealPart) -> str:
    return " ".join(item for item in (real.lcsc, real.title) if item) or "the part"


def _label(rows: int, cols: int) -> str:
    return f"{rows} × {cols}"


def compare(reference: str, kicad_pads: Iterable[Pad], real: RealPart,
            nets: dict[str, str] | None = None) -> list[Finding]:
    """Findings (at most one) for a footprint whose pads do not fit the real part. With nets (from
    pad_nets) a numbering difference whose swapped pins share a net is not reported."""
    groups = _group(list(kicad_pads))
    fp = [pad for pad in groups if not _mechanical(pad)]
    mech = [pad for pad in groups if _mechanical(pad)]
    part = _group([(p.number, p.x_mm, p.y_mm, p.through_hole, p.area_mm2) for p in real.pads if p.number.strip()])
    used = {pad[0] for pad in fp}
    if len(fp) + len(mech) == len(part):
        fp, mech = fp + mech, []
    elif len(fp) < len(part) < len(fp) + len(mech):
        spare = [pad for pad in sorted(part, key=lambda pad: pad[4], reverse=True) if pad[0] not in used]
        mounting = {pad[0] for pad in spare[:len(part) - len(fp)]}
        part = [pad for pad in part if pad[0] not in mounting]
    merged = _served(fp, part) if len(fp) < len(part) else []   # several part pads on one footprint pad
    large = _large_pads(part)
    tab = next((number for number in large if number in used), None) if len(part) > 2 and not merged else None
    part_count = len(part) or real.pin_count
    if not fp or not part_count:
        return []
    name = _part_name(real)
    blind = "" if part else " The part's pad layout is not available, so the pad positions were not compared."

    def finding(rule: str, title: str, evidence: str, fix: str, severity=Severity.ERROR) -> list[Finding]:
        return [Finding(rule, severity, f"{reference} {title}", (reference,), (), evidence + blind, fix)]

    if len(fp) != part_count and not merged:
        extra = f" (and {len(mech)} mounting pad(s))" if mech else ""
        return finding("fit.pad_count", f"footprint has {len(fp)} pads but {name} has {part_count}",
                       f"The footprint has {len(fp)} pads{extra}; the part's data lists {part_count}.",
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
    aligned = bool(part) and len(fp) <= _MAX_ALIGN_PADS
    mine, theirs = (_centred(fp), _centred(part)) if aligned else ([], [])
    options = list(_alignments(mine, theirs)) if aligned else []
    fits = [option for option in options if option[0] <= 0]
    if not fits:   # the centres differ, but part pads still land inside the footprint's pads
        fits = _served(fp, part)
    if not fits and sorted(rows_cols) != sorted(part_rows_cols):
        return finding("fit.layout", f"pad layout differs from {name} (footprint "
                       f"{_label(*rows_cols)}, part {_label(*part_rows_cols)})",
                       f"The footprint's {count} pads form {rows_cols[0]} row(s) × {rows_cols[1]} column(s); "
                       f"the part's {part_count} pads form {part_rows_cols[0]} row(s) × "
                       f"{part_rows_cols[1]} column(s).",
                       "Use the footprint for this part's package; the pin count matches but the pads "
                       "are arranged differently.")
    if not aligned:
        return []
    if not fits:
        _, _, _, offsets, _ = min(options, key=lambda option: option[:2])
        worst = max(offsets)
        pad = fp[offsets.index(worst)][0]
        return finding("fit.pitch", f"pad positions differ from {name} by up to {worst:.2f} mm",
                       f"Best alignment still leaves pad {pad} {worst:.2f} mm off (allowed: "
                       f"{FIT_TOLERANCE_MM:.2f} mm along a row of pads; {ROW_SPACING_MM:.2f} mm or "
                       f"{ROW_SPACING_SHARE:.0%} in row spacing). Pad pitch: footprint {_pitch(mine):.2f} mm, "
                       f"part {_pitch(theirs):.2f} mm.",
                       "Use the footprint with the part's pad pitch and spacing.")
    # Both sides are top views (bottom-side footprints are un-mirrored on read), so a mirror may
    # explain the geometry but never the numbering: a mirror-image pinout is a real mistake, unless
    # the part as placed (not mirrored) puts each swapped pin on the net it should have anyway.
    numbers = [pad[0] for pad in fp]
    shared = {n for n in numbers if numbers.count(n) == 1} & {pad[0] for pad in part}

    def net(number: str) -> str:
        found = (nets or {}).get(number, "")
        return "" if found.startswith("unconnected-") else found

    # Four pads on two nets, two pads each, are two internally joined pairs (a tact switch): any
    # numbering reaches the same nets.
    pairs = Counter(net(number) for number in numbers)
    if len(numbers) == 4 and "" not in pairs and sorted(pairs.values()) == [2, 2]:
        return []
    wrong = []
    for _, _, nearest, _, mirrored in fits:
        swapped = [(fp[i][0], part[j][0]) for i, j in enumerate(nearest)
                   if fp[i][0] != part[j][0] and fp[i][0] in shared and part[j][0] in shared]
        # A swap with an unconnected pad has no electrical effect.
        bad = [(a, b) for a, b in swapped
               if nets is None or (net(a) and net(b) and (mirrored or net(a) != net(b)))]
        if not bad and (swapped or not mirrored):
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
