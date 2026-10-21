"""Routing problem read straight from a saved KiCad board; no DSN round trip.

Research input for the native Vela-routing engine (vela_engine). Coordinates are
KiCad's own: millimetres, Y down. Nothing here writes a board.

Not modelled (KiCad DRC stays the judge): copper text (the runner asks KiCad to
render it), custom rules beyond the routing subset ``_custom_rules`` reads (each rule
left out is listed in ``Board.notes``), blind/buried vias, trapezoid/chamfer corners
(treated as their full rectangle). Existing tracks are obstacles only;
existing vias are treated as pads of their net, so they get connected.
"""
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
import json
import math
from pathlib import Path
import re

from .candidate import read_board
from .errors import ValidationError
from .sexpr import children, parse


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
    teardrop: tuple = ()         # (half width, length past the rim) of KiCad's teardrop on a leaving track


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
    name: str = ""               # a rule area's name, for custom-rule conditions


@dataclass(frozen=True)
class NetClass:
    name: str
    width: float
    clearance: float
    via_diameter: float
    via_drill: float
    diff_pair_width: float = 0.0   # 0 = not set: the track width
    diff_pair_gap: float = 0.0     # 0 = not set: the clearance
    min_width: float = 0.0         # custom-rule minimum width: no narrower escape (neck)


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
    overrides: dict = field(default_factory=dict)   # net -> NetClass with its custom-rule values
    notes: list = field(default_factory=list)       # custom rules not (fully) modelled

    def netclass(self, net: str) -> NetClass:
        if net in self.overrides:
            return self.overrides[net]
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


def _teardrop(node, size):
    """(half width, length) of the teardrop KiCad grows where a track leaves a pad, both from
    the pad's smaller side; () when the pad has none."""
    if node is None or str((_first(node, "enabled") or [0, "no"])[1]) != "yes":
        return ()
    value = {row[0]: float(row[1]) for row in node[1:] if isinstance(row, list) and len(row) > 1
             and row[0] in ("best_length_ratio", "max_length", "best_width_ratio", "max_width")}
    width = value.get("best_width_ratio", 1.0) * size
    length = value.get("best_length_ratio", 0.5) * size
    return (min(width, value.get("max_width") or width) / 2, min(length, value.get("max_length") or length))


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
        name = _first(node, "name")
        for polygon in children(node, "polygon"):
            points = tuple((_f(xy, 1), _f(xy, 2)) for xy in children(_first(polygon, "pts"), "xy"))
            if len(points) >= 3:
                yield Zone("" if keepout else _net_name(node, codes), _copper(names, layers), points,
                           flags.get("tracks") == "not_allowed", flags.get("vias") == "not_allowed",
                           flags.get("copperpour") == "not_allowed", _f(gap) if gap else 0.0, _f(thin) if thin else 0.25, mode,
                           *(_f(value) if value else 0.5 for value in relief), str(name[1]) if name else "")


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
                spoke_angle=_f(_first(pad, "thermal_bridge_angle")) if _first(pad, "thermal_bridge_angle") else None,
                teardrop=_teardrop(_first(pad, "teardrops"), min(_f(size, 1), _f(size, 2)))))
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
    _custom_rules(board, path.with_suffix(".kicad_dru"))
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
            drill, float(item.get("diff_pair_width") or 0), float(item.get("diff_pair_gap") or 0)))
    if not any(item.name == "Default" for item in board.classes):
        board.classes.append(NetClass("Default", 0.2, 0.2, 0.6, 0.3))
    # Explicit per-net assignments win over patterns, as in KiCad.
    for net, assigned in (settings.get("netclass_assignments") or {}).items():
        for name in ([assigned] if isinstance(assigned, str) else assigned or []):
            board.patterns.append((str(net), str(name)))
    for item in settings.get("netclass_patterns") or []:
        if item.get("pattern"):
            board.patterns.append((str(item["pattern"]), str(item.get("netclass", "Default"))))


# ---- custom rules (.kicad_dru) ------------------------------------------------
# Idea from KiCadRoutingTools: read the routing subset of KiCad's custom rules, resolve
# it in KiCad's order, and list what is not modelled instead of ignoring it.

_UNITS = {"mm": 1.0, "mil": 0.0254, "in": 25.4, "um": 0.001}
_TOKEN = re.compile(r"""\s*('[^']*'|"[^"]*"|&&|\|\||==|!=|[()!,.]|[A-Za-z_]\w*|[-+\d.]+\w*)""")
# Constraint -> the routed item it sizes (None: tracks and vias alike).
_KINDS = {"clearance": None, "hole_clearance": None, "edge_clearance": None,
          "track_width": "Track", "via_diameter": "Via", "hole_size": "Via"}


class _Unsupported(ValueError):
    pass


def _length(text):
    found = re.fullmatch(r"([-+]?[\d.]+)\s*(mm|mil|in|um)?", str(text))
    return float(found[1]) * _UNITS[found[2] or "mm"] if found else None


def _condition(text, item, area=None):
    """Tri-state value of a rule condition for a net's routed item: True, False, or None when
    it hangs on what a net-wide value cannot pin down (layer, other item B, area). ``item``:
    {"net", "class", "type"}, None = unknown. Area tests give ``area``; their names are
    collected in item["areas"]. Raises _Unsupported for anything else."""
    tokens, i = [], 0
    while text[i:].strip():
        found = _TOKEN.match(text, i)
        if not found:
            raise _Unsupported(f"cannot read {text[i:]!r}")
        tokens.append(found[1])
        i = found.end()
    tokens.append("")
    at = 0

    def take(expected=None):
        nonlocal at
        token = tokens[at]
        if not token or expected is not None and token != expected:
            raise _Unsupported(f"expected {expected or 'more'} at {token or 'end'!r}")
        at += 1
        return token.strip("'\"")

    def tri(values, stop):   # and (stop False) / or (stop True) over True, False, None
        return stop if stop in values else None if None in values else not stop

    def either():
        values = [both()]
        while tokens[at] == "||":
            take()
            values.append(both())
        return tri(values, True)

    def both():
        values = [one()]
        while tokens[at] == "&&":
            take()
            values.append(one())
        return tri(values, False)

    def one():
        if tokens[at] in ("!", "("):
            if take() == "!":
                value = one()
                return None if value is None else not value
            value = either()
            take(")")
            return value
        side = take()
        if side not in ("A", "B"):
            raise _Unsupported(f"unknown item {side!r}")
        take(".")
        name = take().lower()
        if tokens[at] == "(":
            take()
            args = []
            while tokens[at] != ")":
                args.append(take())
                if tokens[at] == ",":
                    take()
            take(")")
            if name in ("intersectsarea", "insidearea", "enclosedbyarea") and args:
                item.setdefault("areas", []).append(args[0])
                return None if side == "B" else area
            if name == "hasnetclass" and args:
                value = item["class"]
            elif name == "existsonlayer":
                return None
            else:
                raise _Unsupported(f"{name}()")
            target, equal = args[0], True
        else:
            if tokens[at] not in ("==", "!=") or name not in (
                    "netclass", "netname", "type", "pad_type", "via_type", "layer"):
                raise _Unsupported(f"{side}.{name}")
            equal, target = take() == "==", take()
            kind = item["type"]
            value = {"netclass": item["class"], "netname": item["net"], "type": kind,
                     "pad_type": kind and "",   # a track or via is no pad
                     "via_type": kind and ("Through" if kind == "Via" else "")}.get(name)
        # KiCad compares strings case-blind, with * and ? wildcards.
        return None if side == "B" or value is None else fnmatchcase(value.lower(), target.lower()) == equal
    value = either()
    if tokens[at]:
        raise _Unsupported(f"unexpected {tokens[at]!r}")
    return value


def _rule_layers(rule, layers):
    """Copper layer indexes a rule's (layer ...) clause selects; None = every layer."""
    clause = _first(rule, "layer")
    if clause is None:
        return None
    name, last = str(clause[1]), len(layers) - 1
    return {"outer": {0, last}, "inner": set(range(1, last))}.get(name, {layers.index(name)} if name in layers else set())


def _custom_rules(board: Board, path: Path) -> None:
    """Fold the routing subset of a .kicad_dru into per-net classes, board rules and keep-outs.

    As in KiCad the last matching rule wins; a pad or footprint clearance still applies on
    top (Pad.clearance). A rule that applies to a net only sometimes (a layer, an area, the
    other item) is applied net-wide where that is the safer side, and noted. Clearances are
    only ever raised: VelaTrace also proves a route under Board Setup rules alone. ``disallow``
    of tracks or vias becomes a keep-out over the rule areas it names, or the whole board.
    """
    if not path.is_file() or path.stat().st_size > 1_000_000:
        return
    text = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#"))
    try:
        rules = [node for node in parse(f"(rules {text})", kicad=True)[1:] if node[:1] == ["rule"]]
    except (ValidationError, IndexError, TypeError) as exc:
        board.notes.append(f"custom rules not read: {exc}")
        return
    layers, r = board.layers, board.rules
    every = set(range(len(layers)))
    nets = sorted({pad.net for pad in board.pads if pad.net} | {zone.net for zone in board.zones if zone.net})
    resolved = {net: {} for net in nets}   # net -> kind -> (value, minimum)
    notes = []
    for rule in rules:
        if any(len(s) > 1 and s[1] == "ignore" for s in children(rule, "severity")):
            continue
        condition = _first(rule, "condition")
        condition = str(condition[1]) if condition else ""
        on = _rule_layers(rule, layers)
        for constraint in children(rule, "constraint"):
            kind = str(constraint[1]) if len(constraint) > 1 else ""
            limits = {str(c[0]): _length(c[1]) for c in constraint[2:] if isinstance(c, list) and len(c) > 1}
            low, opt, high = limits.get("min"), limits.get("opt"), limits.get("max")
            try:
                if kind == "disallow":
                    notes += _disallow(board, rule, constraint, condition, on)
                    continue
                if kind not in _KINDS:
                    notes.append(f"custom rule {rule[1]!r}: {kind} is not a routing constraint; KiCad DRC checks it")
                    continue
                vague = False
                for net in nets:
                    base = board.netclass(net)
                    applies = _condition(condition, {"net": net, "class": base.name, "type": _KINDS[kind]}) if condition else True
                    if applies is False or on is not None and not on & every:
                        continue
                    if on is not None and not on >= every:
                        applies = None
                    vague |= applies is None
                    start = {"track_width": base.width, "via_diameter": base.via_diameter, "hole_size": base.via_drill}.get(kind)
                    value, floor = resolved[net].get(kind, (start, 0.0))
                    if start is None:   # a clearance
                        value = low if applies else max(value or 0.0, low or 0.0)
                    elif applies:
                        value, floor = (opt if opt is not None else min(max(start, low or 0.0), high or math.inf)), low or 0.0
                    else:
                        value, floor = min(max(value, low or 0.0), high or math.inf), max(floor, low or 0.0)
                    resolved[net][kind] = (value, floor)
                if vague:
                    notes.append(f"custom rule {rule[1]!r}: {kind} depends on a layer, an area or the other item; "
                                 "applied net-wide where that is the safer side")
            except _Unsupported as exc:
                notes.append(f"custom rule {rule[1]!r}: condition not modelled ({exc}); not applied")
    for net, values in resolved.items():
        base = board.netclass(net)
        drill = max(values.get("hole_size", (base.via_drill,))[0], r["min_through_hole_diameter"])
        override = replace(
            base, clearance=max(base.clearance, values.get("clearance", (0.0,))[0] or 0.0),
            width=max(values.get("track_width", (base.width,))[0], r["min_track_width"]),
            via_drill=drill, via_diameter=max(values.get("via_diameter", (base.via_diameter,))[0],
                                              r["min_via_diameter"], drill + 2 * r["min_via_annular_width"]),
            min_width=values.get("track_width", (0, 0.0))[1])
        if override != base:
            board.overrides[net] = override
        for kind, key in (("hole_clearance", "min_hole_clearance"), ("edge_clearance", "min_copper_edge_clearance")):
            r[key] = max(r[key], values.get(kind, (0.0,))[0] or 0.0)
    board.notes.extend(dict.fromkeys(notes))


def _disallow(board, rule, constraint, condition, on):
    """``disallow`` of tracks and/or vias as rule-area keep-outs; returns notes."""
    kinds = {str(k) for k in constraint[2:]}
    tracks, vias = "track" in kinds, bool(kinds & {"via", "through_via"})
    rest = kinds - {"track", "via", "through_via", "micro_via", "buried_via", "blind_via"}
    notes = [f"custom rule {rule[1]!r}: disallow {' '.join(sorted(rest))} is not routing; KiCad DRC checks it"] if rest else []
    if not (tracks or vias):
        return notes   # micro, blind and buried vias: Vela only makes through vias
    item = {"net": None, "class": None, "type": "Via" if vias and not tracks else "Track" if not vias else None}
    inside = _condition(condition, item, True) if condition else True
    names = item.get("areas")
    outside = _condition(condition, dict(item), False) if names else inside
    if inside is None or outside is None:
        return notes + [f"custom rule {rule[1]!r}: disallow depends on the net, layer or item; not modelled"]
    if not inside:
        return notes
    names = None if outside else names   # true outside the areas too: the whole board
    areas = [zone for zone in board.zones if not zone.net and zone.name in (names or ())]
    if names and not areas:
        return notes + [f"custom rule {rule[1]!r}: rule area {names[0]!r} not found; not applied"]
    if not names:
        xs = [v for e in board.edges for v in (e[0], e[2])] or [pad.x for pad in board.pads]
        ys = [v for e in board.edges for v in (e[1], e[3])] or [pad.y for pad in board.pads]
        areas = [Zone("", tuple(range(len(board.layers))), ((min(xs), min(ys)), (max(xs), min(ys)),
                                                             (max(xs), max(ys)), (min(xs), max(ys))))]
    for zone in areas:
        board.zones.append(Zone("", tuple(sorted(set(zone.layers) & (on if on is not None else set(zone.layers)))),
                                zone.points, no_tracks=tracks, no_vias=vias, name=f"rule {rule[1]}"))
    return notes
