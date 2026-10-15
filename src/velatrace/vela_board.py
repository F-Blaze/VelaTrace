"""Routing problem read straight from a saved KiCad board; no DSN round trip.

Research input for the native Vela-routing engine (vela_engine). Coordinates are
KiCad's own: millimetres, Y down. Nothing here writes a board.

Not modelled (KiCad DRC stays the judge): copper text (the runner asks KiCad to
render it), custom-rule files (.kicad_dru), blind/buried vias, trapezoid/chamfer
corners (treated as their full rectangle). Existing tracks are obstacles only;
existing vias are treated as pads of their net, so they get connected.
"""
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
import json
import math
from pathlib import Path

from .candidate import read_board
from .errors import ValidationError
from .sexpr import children


@dataclass(frozen=True)
class Pad:
    ref: str
    number: str
    net: str
    x: float
    y: float
    angle: float                 # absolute, degrees, KiCad convention
    # Pad-local primitives: ("rrect", cx, cy, w, h, corner_radius), ("poly", ((x, y), ...))
    # or ("line", x1, y1, x2, y2, width).
    shapes: tuple
    layers: tuple[int, ...]      # copper layer indexes, 0 = F.Cu
    drill: float = 0.0
    plated: bool = True
    clearance: float = 0.0       # local override from the pad or its footprint; 0 = net class
    tie: tuple = ()              # net-tie footprint: other nets allowed to touch this copper
    zone_connect: int = -1       # pad/footprint override: 0 none, 1 thermal, 2 solid, 3 thermal if THT; -1 zone
    spoke_angle: float | None = None   # thermal spoke angle; None = KiCad default (45 round, 90 other)


@dataclass(frozen=True)
class Zone:
    net: str                     # "" for a rule area
    layers: tuple[int, ...]
    points: tuple[tuple[float, float], ...]
    no_tracks: bool = False
    no_vias: bool = False
    no_pour: bool = False        # a rule area that keeps zone fill out
    clearance: float = 0.0       # the pour's own clearance to other nets
    min_thickness: float = 0.25  # narrowest copper the fill keeps
    connect: str = "thermal"     # how pads join it: thermal, yes (solid), no, thru_hole_only
    thermal_gap: float = 0.5
    spoke_width: float = 0.5


@dataclass(frozen=True)
class NetClass:
    name: str
    width: float
    clearance: float
    via_diameter: float
    via_drill: float


@dataclass
class Board:
    text: str
    layers: tuple[str, ...]
    pads: list[Pad] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    edges: list[tuple[float, float, float, float]] = field(default_factory=list)
    tracks: list[tuple] = field(default_factory=list)   # (net, layer, x1, y1, x2, y2, width)
    rules: dict = field(default_factory=dict)
    classes: list[NetClass] = field(default_factory=list)
    patterns: list[tuple[str, str]] = field(default_factory=list)
    thickness: float = 1.6

    def netclass(self, net: str) -> NetClass:
        by_name = {item.name: item for item in self.classes}
        for pattern, name in self.patterns:
            if name in by_name and (pattern == net or fnmatchcase(net, pattern)):
                return by_name[name]
        return by_name["Default"]


def _f(node, index=1):
    return float(node[index])


def _rot(x, y, degrees):
    """KiCad RotatePoint: Y is down, positive angles turn counter-clockwise on screen."""
    a = math.radians(degrees)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def _first(node, name):
    found = children(node, name)
    return found[0] if found else None


def _copper(names, layers):
    out = set()
    for name in names:
        if name == "*.Cu":
            out.update(range(len(layers)))
        elif name == "F&B.Cu":
            out.update((0, len(layers) - 1))
        elif name in layers:
            out.add(layers.index(name))
    return tuple(sorted(out))


def _arc(start, mid, end, step=math.radians(10)):
    """Three-point arc as a polyline; a degenerate arc is its chord."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, a1, a2 = (math.atan2(py - uy, px - ux) for px, py in (start, mid, end))
    sweep, via = (a2 - a0) % math.tau, (a1 - a0) % math.tau
    if via > sweep:
        sweep -= math.tau
    count = max(2, int(abs(sweep) / step) + 1)
    return [(ux + r * math.cos(a0 + sweep * i / count), uy + r * math.sin(a0 + sweep * i / count))
            for i in range(count + 1)]


def _outline(node, place):
    """Edge.Cuts graphic -> list of absolute points (closed shapes repeat the first)."""
    kind = node[0][3:]
    def pt(name):
        item = _first(node, name)
        return place(_f(item, 1), _f(item, 2))
    if kind == "line":
        return [pt("start"), pt("end")]
    if kind == "arc":
        return _arc(pt("start"), pt("mid"), pt("end")) if _first(node, "mid") else [pt("start"), pt("end")]
    if kind == "rect":
        s, e = _first(node, "start"), _first(node, "end")
        corners = [(_f(s, 1), _f(s, 2)), (_f(e, 1), _f(s, 2)), (_f(e, 1), _f(e, 2)), (_f(s, 1), _f(e, 2))]
        return [place(*c) for c in corners + corners[:1]]
    if kind == "circle":
        c, e = _first(node, "center"), _first(node, "end")
        r = math.hypot(_f(e, 1) - _f(c, 1), _f(e, 2) - _f(c, 2))
        return [place(_f(c, 1) + r * math.cos(i * math.tau / 36), _f(c, 2) + r * math.sin(i * math.tau / 36))
                for i in range(37)]
    if kind == "poly":
        points = [place(_f(xy, 1), _f(xy, 2)) for xy in children(_first(node, "pts"), "xy")]
        return points + points[:1]
    return []


_GRAPHICS = ("line", "arc", "rect", "circle", "poly")


def _net_name(node, codes):
    """KiCad 10 names nets inline; KiCad 9 and older use (net <code> ["name"])."""
    name = _first(node, "net_name")
    if name is not None and len(name) > 1:
        return str(name[1])
    net = _first(node, "net")
    if net is None or len(net) < 2:
        return ""
    return str(net[-1]) if len(net) > 2 or net[1] not in codes else codes[net[1]]


def _pad_shapes(pad, kind, w, h):
    # The copper shape may sit off the hole: (drill ... (offset x y)), pad-local.
    offset = _first(_first(pad, "drill") or ["drill"], "offset")
    ox, oy = (_f(offset, 1), _f(offset, 2)) if offset else (0.0, 0.0)
    if kind == "circle":
        shapes = [("rrect", ox, oy, w, w, w / 2)]
    elif kind == "oval":
        shapes = [("rrect", ox, oy, w, h, min(w, h) / 2)]
    elif kind == "roundrect":
        ratio = _first(pad, "roundrect_rratio")
        shapes = [("rrect", ox, oy, w, h, (_f(ratio) if ratio else 0.25) * min(w, h))]
    else:
        shapes = [("rrect", ox, oy, w, h, 0.0)]
    if kind == "custom":
        for primitive in (_first(pad, "primitives") or [])[1:]:
            if isinstance(primitive, list) and primitive[0][:3] == "gr_":
                shapes.extend(_copper_shapes(primitive, lambda x, y: (x, y)))
    return tuple(shapes)


def _copper_shapes(node, place):
    """A copper graphic as shapes: its filled area and/or its stroked outline."""
    kind = node[0][3:]
    if kind not in _GRAPHICS:
        return []
    points = _outline(node, place)
    stroke = _first(node, "stroke")
    width = _first(stroke, "width") if stroke else _first(node, "width")
    width = _f(width) if width else 0.0
    fill = _first(node, "fill")
    filled = kind == "poly" or (kind in ("rect", "circle") and fill is not None and fill[1] not in ("no", "none"))
    shapes = []
    if filled and len(points) >= 4:
        shapes.append(("poly", tuple(points[:-1])))
    if width > 0 or not filled:
        shapes.extend(("line", *a, *b, max(width, 0.01)) for a, b in zip(points, points[1:]) if a != b)
    return shapes


def _graphic_pad(layer, shapes):
    """Net-less copper as a pad at the shapes' centre, so the engine keeps everything off it."""
    xs = [v for s in shapes for v in ([p[0] for p in s[1]] if s[0] == "poly" else (s[1], s[3]))]
    ys = [v for s in shapes for v in ([p[1] for p in s[1]] if s[0] == "poly" else (s[2], s[4]))]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    local = tuple(("poly", tuple((x - cx, y - cy) for x, y in s[1])) if s[0] == "poly"
                  else ("line", s[1] - cx, s[2] - cy, s[3] - cx, s[4] - cy, s[5]) for s in shapes)
    return Pad("", "", "", cx, cy, 0.0, local, (layer,))


def _zones(parent, layers, codes):
    for node in children(parent, "zone"):
        names = [str(n) for item in children(node, "layer") + children(node, "layers") for n in item[1:]]
        keepout = _first(node, "keepout")
        flags = {item[0]: item[1] for item in (keepout or [])[1:] if isinstance(item, list) and len(item) == 2}
        connect = _first(node, "connect_pads") or []
        gap = _first(connect, "clearance")
        mode = str(connect[1]) if len(connect) > 1 and isinstance(connect[1], str) else "thermal"
        fill = _first(node, "fill") or []
        relief = [_first(fill, key) for key in ("thermal_gap", "thermal_bridge_width")]
        thin = _first(node, "min_thickness")
        for polygon in children(node, "polygon"):
            points = tuple((_f(xy, 1), _f(xy, 2)) for xy in children(_first(polygon, "pts"), "xy"))
            if len(points) >= 3:
                yield Zone("" if keepout else _net_name(node, codes), _copper(names, layers), points,
                           flags.get("tracks") == "not_allowed", flags.get("vias") == "not_allowed",
                           flags.get("copperpour") == "not_allowed", _f(gap) if gap else 0.0, _f(thin) if thin else 0.25, mode,
                           *(_f(value) if value else 0.5 for value in relief))


def load_board(path: Path) -> Board:
    path = Path(path)
    text, root = read_board(path)
    table = [row for row in _first(root, "layers")[1:] if isinstance(row, list) and len(row) > 2]
    names = [row[1] for row in table if str(row[1]).endswith(".Cu")]
    layers = tuple(["F.Cu", *sorted((n for n in names if n.startswith("In")), key=lambda n: int(n[2:-3])), "B.Cu"])
    if set(layers) != set(names):
        raise ValidationError("Board copper layers are not F.Cu, inner layers, B.Cu.")
    board = Board(text, layers)
    general = _first(root, "general")
    thickness = _first(general, "thickness") if general else None
    board.thickness = _f(thickness) if thickness else 1.6
    codes = {row[1]: str(row[2]) for row in children(root, "net") if len(row) == 3}

    def edge(points):
        board.edges.extend((*a, *b) for a, b in zip(points, points[1:]) if a != b)

    def graphic(node, place):
        layer = _first(node, "layer")
        if layer and layer[1] == "Edge.Cuts":
            edge(_outline(node, place))
        elif layer and layer[1] in layers:
            shapes = _copper_shapes(node, place)
            if shapes:
                board.pads.append(_graphic_pad(layers.index(layer[1]), shapes))

    for kind in _GRAPHICS:
        for node in children(root, "gr_" + kind):
            graphic(node, lambda x, y: (x, y))
    for fp in children(root, "footprint"):
        at = _first(fp, "at")
        fx, fy, frot = _f(at, 1), _f(at, 2), (_f(at, 3) if len(at) > 3 else 0.0)
        reference = next((str(p[2]) for p in children(fp, "property") if len(p) > 2 and p[1] == "Reference"), "")
        first = len(board.pads)

        def place(x, y, fx=fx, fy=fy, frot=frot):
            dx, dy = _rot(x, y, frot)
            return fx + dx, fy + dy

        for kind in _GRAPHICS:
            for node in children(fp, "fp_" + kind):
                graphic(node, place)
        local, joined = _first(fp, "clearance"), _first(fp, "zone_connect")
        for pad in children(fp, "pad"):
            pad_at, size, drill = _first(pad, "at"), _first(pad, "size"), _first(pad, "drill")
            hole = 0.0
            if drill is not None:
                numbers = [float(v) for v in drill[1:] if isinstance(v, str) and v != "oval"]
                hole = max(numbers[:2]) if numbers else 0.0  # ponytail: slots as their long axis
            x, y = place(_f(pad_at, 1), _f(pad_at, 2))
            board.pads.append(Pad(
                reference, str(pad[1]), _net_name(pad, codes), x, y,
                _f(pad_at, 3) if len(pad_at) > 3 else 0.0,
                _pad_shapes(pad, pad[3], _f(size, 1), _f(size, 2)),
                _copper([str(n) for n in (_first(pad, "layers") or [])[1:]], layers),
                hole, pad[2] != "np_thru_hole", _f(_first(pad, "clearance") or local or [0, 0]),
                zone_connect=int(_f(_first(pad, "zone_connect") or joined or [0, -1])),
                spoke_angle=_f(_first(pad, "thermal_bridge_angle")) if _first(pad, "thermal_bridge_angle") else None))
        # A net tie joins nets on purpose: its grouped pads and its copper may touch each other.
        for group in (_first(fp, "net_tie_pad_groups") or [])[1:]:
            numbers = {part.strip() for part in str(group).split(",")}
            nets = {pad.net for pad in board.pads[first:] if pad.number in numbers and pad.net}
            if len(nets) > 1:
                for index in range(first, len(board.pads)):
                    pad = board.pads[index]
                    if (pad.number in numbers and pad.net) or not (pad.net or pad.ref):
                        board.pads[index] = replace(pad, tie=tuple(sorted(nets - {pad.net})))
        board.zones.extend(_zones(fp, layers, codes))
    board.zones.extend(_zones(root, layers, codes))
    for node in children(root, "segment"):
        s, e, layer = _first(node, "start"), _first(node, "end"), _first(node, "layer")[1]
        if layer in layers:
            board.tracks.append((_net_name(node, codes), layers.index(layer), _f(s, 1), _f(s, 2),
                                 _f(e, 1), _f(e, 2), _f(_first(node, "width"))))
    for node in children(root, "arc"):
        layer = _first(node, "layer")[1]
        if layer in layers:
            points = _outline(["gr_arc", *node[1:]], lambda x, y: (x, y))
            board.tracks.extend((_net_name(node, codes), layers.index(layer), *a, *b, _f(_first(node, "width")))
                                for a, b in zip(points, points[1:]))
    for node in children(root, "via"):
        at = _first(node, "at")
        size = _f(_first(node, "size"))
        board.pads.append(Pad("", "via", _net_name(node, codes), _f(at, 1), _f(at, 2), 0.0,
                              (("rrect", 0.0, 0.0, size, size, size / 2),), tuple(range(len(layers))),
                              _f(_first(node, "drill"))))
    _read_project(board, path.with_suffix(".kicad_pro"))
    return board


def _read_project(board: Board, project: Path) -> None:
    data = {}
    if project.is_file() and project.stat().st_size < 8_000_000:
        data = json.loads(project.read_text(encoding="utf-8"))
    rules = (data.get("board") or {}).get("design_settings", {}).get("rules", {}) or {}
    board.rules = {key: float(rules.get(key) or 0) for key in (
        "min_clearance", "min_track_width", "min_via_diameter", "min_through_hole_diameter",
        "min_copper_edge_clearance", "min_hole_clearance", "min_hole_to_hole", "min_via_annular_width")}
    settings = data.get("net_settings") or {}
    r = board.rules
    for item in settings.get("classes") or [{"name": "Default"}]:
        drill = max(float(item.get("via_drill") or 0.3), r["min_through_hole_diameter"])
        board.classes.append(NetClass(
            str(item.get("name", "Default")),
            max(float(item.get("track_width") or 0.2), r["min_track_width"]),
            max(float(item.get("clearance") or 0.2), r["min_clearance"]),
            max(float(item.get("via_diameter") or 0.6), r["min_via_diameter"],
                drill + 2 * r["min_via_annular_width"]),
            drill))
    if not any(item.name == "Default" for item in board.classes):
        board.classes.append(NetClass("Default", 0.2, 0.2, 0.6, 0.3))
    # Explicit per-net assignments win over patterns, as in KiCad.
    for net, assigned in (settings.get("netclass_assignments") or {}).items():
        for name in ([assigned] if isinstance(assigned, str) else assigned or []):
            board.patterns.append((str(net), str(name)))
    for item in settings.get("netclass_patterns") or []:
        if item.get("pattern"):
            board.patterns.append((str(item["pattern"]), str(item.get("netclass", "Default"))))
