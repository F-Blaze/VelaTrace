"""Pre-flight checks on the live board before Freerouting starts: one actionable line each.

Problems refuse routing (it would fail later anyway, after a long wait); notes only
warn. Numeric-only net, reference, image or padstack names were tested and route
fine in Freerouting 2.1.0, so they are not checked.
"""
from collections import Counter
import math
from pathlib import Path

from .models import DesignSnapshot
from .sexpr import children


def _points(row):
    return [(float(item[1]), float(item[2])) for item in row[1:]
            if isinstance(item, list) and item[:1] in (["start"], ["end"], ["mid"], ["center"], ["xy"])
            and len(item) >= 3]


def _circle(a, b, c):
    """Centre and radius of the circle through three points, or None if collinear."""
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-12:
        return None
    sq = [p[0] ** 2 + p[1] ** 2 for p in (a, b, c)]
    x = (sq[0] * (b[1] - c[1]) + sq[1] * (c[1] - a[1]) + sq[2] * (a[1] - b[1])) / d
    y = (sq[0] * (c[0] - b[0]) + sq[1] * (a[0] - c[0]) + sq[2] * (b[0] - a[0])) / d
    return (x, y), math.dist((x, y), a)


def outline_box(root):
    """Bounding box of the Edge.Cuts outline, or None when there is none.

    ponytail: a box, not the polygon; parts inside a notch of an L-shaped board pass
    here and are left to Freerouting and DRC. Arcs and circles use their full circle,
    so the box only ever errs larger (never a false refusal)."""
    points, footprint_outline = [], False
    for row in root[1:]:
        if isinstance(row, list) and row[:1] == ["footprint"]:
            footprint_outline |= any(any(item[:2] == ["layer", "Edge.Cuts"] for item in shape if isinstance(item, list))
                                     for shape in row if isinstance(shape, list))
        if not (isinstance(row, list) and str(row[0]).startswith("gr_")
                and any(isinstance(item, list) and item[:2] == ["layer", "Edge.Cuts"] for item in row)):
            continue
        if row[0] in ("gr_poly", "gr_curve"):
            pts = children(row, "pts")
            points += _points(pts[0]) if pts else []
            continue
        row_points = _points(row)
        if row[0] == "gr_circle" and len(row_points) == 2:
            (x, y), r = row_points[0], math.dist(*row_points)
            row_points = [(x - r, y - r), (x + r, y + r)]
        elif row[0] == "gr_arc" and len(row_points) == 3 and (circle := _circle(*row_points)):
            (x, y), r = circle
            row_points += [(x - r, y - r), (x + r, y + r)]
        points += row_points
    if not points:
        return "footprint" if footprint_outline else None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _rule_notes(project: dict, board_path: Path | None) -> list[str]:
    """Board Setup constraints Freerouting never sees: KiCad's DSN carries net classes only."""
    settings = project.get("board", {}).get("design_settings", {}) if isinstance(project, dict) else {}
    rules = settings.get("rules", {}) if isinstance(settings, dict) else {}
    classes = project.get("net_settings", {}).get("classes", []) if isinstance(project, dict) else []
    def least(key):
        values = [row[key] for row in classes if isinstance(row, dict) and isinstance(row.get(key), (int, float))]
        return min(values) if values else None
    clearance, width = least("clearance"), least("track_width")
    notes = []
    severities = settings.get("rule_severities", {}) if isinstance(settings, dict) else {}
    ignored = sum(1 for value in severities.values() if value == "ignore") if isinstance(severities, dict) else 0
    if ignored:
        notes.append(f"{ignored} DRC check(s) are set to Ignore in Board Setup; VelaTrace runs them as warnings "
                     "when it checks the route, so a route cannot add such an issue unnoticed.")
    for key, name, floor in (("min_clearance", "minimum clearance", clearance),
                             ("min_track_width", "minimum track width", width),
                             ("min_copper_edge_clearance", "copper-to-edge clearance", clearance),
                             ("min_hole_clearance", "hole clearance", clearance)):
        value = rules.get(key) if isinstance(rules, dict) else None
        if isinstance(value, (int, float)) and floor is not None and value > floor + 1e-9:
            notes.append(f"Board Setup {name} {value:g} mm is stricter than the net classes ({floor:g} mm) and "
                         "is not passed to Freerouting; DRC still checks it. Raise the net class value to match.")
    rules_file = board_path.with_suffix(".kicad_dru") if board_path else None
    if rules_file and rules_file.is_file() and "(rule" in rules_file.read_text(encoding="utf-8", errors="replace"):
        notes.append(f"Custom rules in {rules_file.name} are not passed to Freerouting; DRC still checks them. "
                     "The route is also checked without that file, so a custom rule cannot hide a new issue.")
    return notes


def preflight(root, snapshot: DesignSnapshot, project: dict) -> tuple[list[str], list[str]]:
    """(problems, notes) for the live board `root` (parsed KiCad text) and its saved project."""
    problems = []
    if any(children(root, kind) for kind in ("segment", "arc", "via")):
        problems.append("The board already has tracks or vias; VelaTrace routes unrouted boards only. "
                        "Delete them (or Undo) in KiCad, then retry.")
    box = outline_box(root)
    if box is None:
        problems.append("No board outline: draw a closed outline on Edge.Cuts, then retry.")
    padded = [item for item in snapshot.components if item.pins]
    duplicates = sorted(ref for ref, count in Counter(item.reference for item in padded).items() if count > 1)
    if duplicates:
        problems.append("Duplicate reference designators: " + ", ".join(duplicates[:8])
                        + ". Annotate the schematic and update the PCB, then retry.")
    if isinstance(box, tuple):
        x0, y0, x1, y1 = box
        def outside(item):
            points = [pin.position_mm for pin in item.pins if pin.position_mm] or [item.position_mm]
            return any(p and not (x0 - 1e-6 <= p[0] <= x1 + 1e-6 and y0 - 1e-6 <= p[1] <= y1 + 1e-6) for p in points)
        stray = sorted({item.reference for item in padded if outside(item)})
        if stray:
            problems.append(f"{len(stray)} footprint(s) outside the board outline: " + ", ".join(stray[:8])
                            + (" …" if len(stray) > 8 else "") + ". Place them inside Edge.Cuts, then retry.")
    return problems, _rule_notes(project, snapshot.path)
