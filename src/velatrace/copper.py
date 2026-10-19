"""Routed copper facts for the high-speed audit checks. Reads only; nothing is written.

Public interface (rules should use only these):

* ``BoardCopper`` (on ``DesignSnapshot.copper``; None when the source has no copper,
  e.g. a netlist; checks must then skip) with ``Segment``, ``Via``, ``FilledZone`` and
  ``NetClassInfo`` rows. Units are millimetres, KiCad coordinates (Y down).
* ``read_copper_file(path)``: a saved ``.kicad_pcb`` (net classes from the ``.kicad_pro``
  beside it, else none). ``read_copper_ipc(board)``: a live kipy board, unsaved edits
  included. Both parse the same board text, so they produce the same data.
* ``find_high_speed(names, net_classes)``: interface guesses and likely differential pairs.
* ``net_routing(copper)``: per net routed length, estimated delay, vias and layers.

Everything here is an estimate from names and geometry, not signal-integrity sign-off.
"""
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
import json
import math
from pathlib import Path
import re
from typing import Iterable, Mapping

from .audit_rules import is_ground, is_supply_name, net_label
from .candidate import read_board
from .errors import ValidationError
from .sexpr import children, parse
from .stackup import homogeneous_delay_ps
from .vela_board import _GRAPHICS, _first, _net_name, _outline
from .vela_electrical import FR4_EPSILON_R, LayerModel, differential_pairs, layer_models

PLANE_COVERAGE = 0.5   # share of the board outline's convex hull a layer's ground/supply fill must cover


@dataclass(frozen=True)
class Segment:
    """One straight track, or one whole arc track (``length_mm`` is then the arc length)."""
    net: str
    layer: str
    start: tuple[float, float]
    end: tuple[float, float]
    width_mm: float
    length_mm: float


@dataclass(frozen=True)
class Via:
    net: str
    position: tuple[float, float]
    layers: tuple[str, ...]      # every copper layer the via spans, in stackup order

    def through(self, copper_layers: tuple[str, ...]) -> bool:
        return self.layers == copper_layers


@dataclass(frozen=True)
class FilledZone:
    """One stored zone-fill polygon outline (KiCad saves holes bridged into the outline).

    The fill is whatever was saved / last filled in KiCad; it may be stale."""
    net: str
    layer: str
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class NetClassInfo:
    name: str
    diff_pair_width_mm: float | None = None
    diff_pair_gap_mm: float | None = None


@dataclass(frozen=True)
class BoardCopper:
    layers: tuple[str, ...]           # copper layers, F.Cu first, B.Cu last
    thickness_mm: float
    segments: tuple[Segment, ...]
    vias: tuple[Via, ...]
    zones: tuple[FilledZone, ...]
    # Layers treated as reference planes when the layer models were built: an inner layer
    # of KiCad type "power", or whose ground/supply fill covers PLANE_COVERAGE of the board;
    # on a 2-layer board the better-covered side if it qualifies. Outer layers of 4+ layer
    # boards are never planes (they carry the signals).
    planes: frozenset[str]
    # vela_electrical.layer_models result per copper layer; empty if it could not be built.
    # LayerModel.assumed marks layers whose stackup came from the generic FR-4 default.
    models: Mapping[str, LayerModel]
    net_classes: Mapping[str, NetClassInfo]   # net name -> its (effective) net class; may be empty


# ---------------------------------------------------------------- reading

def _shoelace(points) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))) / 2


def _polyline_length(points) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def _hull(points) -> list:
    """Convex hull (monotone chain). ponytail: overstates concave boards; chain Edge.Cuts if that matters."""
    points = sorted(set(points))

    def half(seq):
        out = []
        for p in seq:
            while len(out) > 1 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                    - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]
    return half(points) + half(points[::-1])


def _planes(layers, kinds, zones, edges) -> frozenset[str]:
    points = edges or [p for zone in zones for p in zone.points]
    if not points:
        return frozenset(name for name in layers[1:-1] if kinds.get(name) == "power")
    area = max(_shoelace(_hull(points)), 1e-9)
    cover = {name: 0.0 for name in layers}
    for zone in zones:
        if zone.layer in cover and (is_ground(zone.net) or is_supply_name(zone.net)):
            cover[zone.layer] += _shoelace(list(zone.points)) / area
    if len(layers) == 2:
        best = max(layers, key=lambda name: cover[name])
        return frozenset([best]) if cover[best] >= PLANE_COVERAGE else frozenset()
    return frozenset(name for name in layers[1:-1]
                     if kinds.get(name) == "power" or cover[name] >= PLANE_COVERAGE)


def parse_copper(text: str, net_classes: Mapping[str, NetClassInfo] | None = None, root=None) -> BoardCopper:
    """Copper of a board given as .kicad_pcb text (a file or ``board.get_as_string()``).

    ``root`` is the already-parsed text, when the caller has it."""
    root = root or parse(text, kicad=True)
    if root[0] != "kicad_pcb":
        raise ValidationError("Expected KiCad board text.")
    table = [row for row in (_first(root, "layers") or [])[1:] if isinstance(row, list) and len(row) > 2]
    kinds = {str(row[1]): str(row[2]) for row in table if str(row[1]).endswith(".Cu")}
    inner = sorted((n for n in kinds if n.startswith("In")), key=lambda n: int(n[2:-3]))
    layers = tuple(n for n in ("F.Cu", *inner, "B.Cu") if n in kinds)
    general = _first(root, "general")
    thickness = _first(general, "thickness") if general else None
    thickness_mm = float(thickness[1]) if thickness else 1.6
    codes = {row[1]: str(row[2]) for row in children(root, "net") if len(row) == 3}

    def xy(node, name):
        item = _first(node, name)
        return float(item[1]), float(item[2])

    segments = []
    for node in children(root, "segment") + children(root, "arc"):
        layer = str(_first(node, "layer")[1])
        if layer not in layers:
            continue
        start, end = xy(node, "start"), xy(node, "end")
        length = (_polyline_length(_outline(["gr_arc", *node[1:]], lambda x, y: (x, y)))
                  if node[0] == "arc" else math.dist(start, end))
        segments.append(Segment(_net_name(node, codes), layer, start, end,
                                float(_first(node, "width")[1]), length))
    vias = []
    for node in children(root, "via"):
        ends = [layers.index(str(n)) for n in (_first(node, "layers") or [])[1:] if str(n) in layers]
        span = layers[min(ends):max(ends) + 1] if len(ends) == 2 else layers
        vias.append(Via(_net_name(node, codes), xy(node, "at"), span))
    zones = []
    for parent in [root, *children(root, "footprint")]:
        for node in children(parent, "zone"):
            net = _net_name(node, codes)
            for fill in children(node, "filled_polygon"):
                layer = str((_first(fill, "layer") or ["", ""])[1])
                points = tuple((float(p[1]), float(p[2])) for p in children(_first(fill, "pts") or [], "xy"))
                if layer in layers and len(points) >= 3:
                    zones.append(FilledZone(net, layer, points))
    edges = [p for kind in _GRAPHICS for node in children(root, "gr_" + kind)
             if (_first(node, "layer") or ["", ""])[1] == "Edge.Cuts"
             for p in _outline(node, lambda x, y: (x, y))]
    planes = _planes(layers, kinds, zones, edges)
    try:
        built = layer_models(layers, thickness_mm, text, {layers.index(name) for name in planes})
        models = {model.name: model for model in built}
    except (ValidationError, ValueError, ZeroDivisionError, IndexError):   # 1-layer or odd stackup
        models = {}
    return BoardCopper(layers, thickness_mm, tuple(segments), tuple(vias), tuple(zones), planes, models,
                       dict(net_classes or {}))


def _project_classes(project: Path, nets: Iterable[str]) -> dict[str, NetClassInfo]:
    """Net -> class from a .kicad_pro: explicit assignments, then patterns, else Default."""
    try:
        settings = json.loads(project.read_text(encoding="utf-8")).get("net_settings") or {}
        classes = {str(item.get("name", "Default")): NetClassInfo(
            str(item.get("name", "Default")),
            float(item["diff_pair_width"]) if item.get("diff_pair_width") else None,
            float(item["diff_pair_gap"]) if item.get("diff_pair_gap") else None)
            for item in settings.get("classes") or []}
        rules = [(str(net), str(name)) for net, assigned in (settings.get("netclass_assignments") or {}).items()
                 for name in ([assigned] if isinstance(assigned, str) else assigned or [])]
        rules += [(str(item["pattern"]), str(item.get("netclass", "Default")))
                  for item in settings.get("netclass_patterns") or [] if item.get("pattern")]
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {}
    default = classes.get("Default", NetClassInfo("Default"))
    out = {}
    for net in nets:
        out[net] = next((classes.get(name, NetClassInfo(name)) for pattern, name in rules
                         if pattern == net or fnmatchcase(net, pattern)), default)
    return out


def read_copper_file(path: Path) -> BoardCopper:
    """Copper of a saved board; net classes from the .kicad_pro beside it when present."""
    path = Path(path)
    text, root = read_board(path)
    copper = parse_copper(text, root=root)
    project = path.with_suffix(".kicad_pro")
    if project.is_file() and project.stat().st_size < 8_000_000:
        # Every named net: KiCad 9 writes (net 3 "X"), KiCad 10 (net "X").
        nets = sorted(set(re.findall(r'\(net(?:_name)?\s+(?:\d+\s+)?"([^"]+)"', text)))
        copper = replace(copper, net_classes=_project_classes(project, nets))
    return copper


def read_copper_ipc(board) -> BoardCopper:
    """Copper of the board open in KiCad (kipy Board), with KiCad's own resolved net classes."""
    def mm(value):
        return value / 1_000_000 if value else None
    classes = {}
    nets = list(board.get_nets())
    if nets:
        classes = {str(net): NetClassInfo(item.name, mm(item.diff_pair_track_width), mm(item.diff_pair_gap))
                   for net, item in board.get_netclass_for_nets(nets).items()}
    return parse_copper(board.get_as_string(), classes)


# ---------------------------------------------------------------- high-speed net finder

# Matched against the designer's net label (hierarchy stripped, upper case). Each pattern
# needs a data/clock role next to the interface word, so USB_VBUS, HDMI_CEC or ETH_LED stay out.
_POL = r"(?:_?[+\-PN]|_?D[PMN])?"
_INTERFACES = (
    ("usb", re.compile(r"D[+\-]|USB\w*?_(?:D[+\-PMN]|D_[PN]|DATA\w*|[TR]X\w*|SS\w*)")),
    ("ethernet", re.compile(r"(?:ETH|RJ45|E?PHY|MDI)\w*?(?:[TR][XD]\w*|MDI\w*|_[PN]\d?|\d_?[+\-PN])"
                            r"|[TR]D[+\-]|R?G?MII_\w+|SGMII\w*")),
    ("hdmi", re.compile(r"TMDS\w*|HDMI\w*?(?:D\d|CK|CLK|TX\d?)" + _POL + r"\w*")),
    ("lvds/mipi", re.compile(r"(?:\w*_)?(?:LVDS|MIPI|CSI\d?|DSI\d?)(?:_\w+?)?_(?:D\d+|CK|CLK|DATA\d*|[TR]X\d*)_?[+\-PN]?")),
    ("sdio/sd", re.compile(r"(?:\w*_)?(?:SDIO|SDMMC|E?MMC|SDC?\d?)\d?_(?:CLK|CMD|D\d|DAT\d)\w*")),
    ("qspi", re.compile(r"(?:\w*_)?[QO]SPI\d?_\w+")),
    ("rf", re.compile(r"(?:\w*_)?(?:RF|ANT\d?|ANTENNA|LNA)(?:_?(?:IN|OUT|P|N|\d))?(?:_\w*)?")),
    ("clock", re.compile(r"(?:\w*_)?(?:CLK|REFCLK|MCLK|CLKIN|CLKOUT|XTAL\d?|XIN|XOUT|OSC\d?|OSC_?(?:IN|OUT)"
                         r"|HSE_?(?:IN|OUT))(?:_?[+\-PN]|\d|_\w*)?")),
)
_CLASS_HINTS = (("usb", "USB"), ("ethernet", "ETH"), ("hdmi", "HDMI"), ("lvds/mipi", "LVDS"),
                ("lvds/mipi", "MIPI"), ("sdio/sd", "SDIO"), ("qspi", "QSPI"), ("rf", "RF"))
# A net class that says "impedance controlled / differential" without naming the interface.
_FAST_CLASS = re.compile(r"DIFF|PAIR|IMPED|HIGH.?SPEED|\bHS\b|Z\d{2,3}\b|\d{2,3}\s*(?:R|OHM|Ω)\b", re.IGNORECASE)
_SLOW = re.compile(r"(?:^|_)(?:SPI\d?|I2C\d?|I2S\d?|UART\d?|SWD|JTAG|SCL|SDA)(?:_|$)")


def interface_guess(net: str, net_class: str = "") -> str | None:
    """Fast interface a net probably belongs to, from its name and net-class name only.

    A name guess: it never looks at parts or data rates, and returns None when unsure."""
    label = net_label(net)
    for interface, pattern in _INTERFACES:
        if label and pattern.fullmatch(label) and not (interface == "clock" and _SLOW.search(label)):
            return interface
    upper = net_class.upper()
    for interface, word in _CLASS_HINTS:
        if re.search(rf"(?:^|[^A-Z]){word}(?:[^A-Z]|$)", upper):
            return interface
    return None


@dataclass(frozen=True)
class HighSpeedNets:
    interfaces: Mapping[str, str]          # net -> guess, only for nets with a guess
    pairs: tuple[tuple[str, str], ...]     # (positive, negative) likely differential pairs


def find_high_speed(names: Iterable[str], net_classes: Mapping[str, NetClassInfo] | None = None) -> HighSpeedNets:
    """Nets that look like fast interfaces, and their differential pairs.

    Pairs come from ``differential_pairs`` (…+/…-, …_P/…_N, …DP/…DM …) but are kept only
    when a member has an interface guess or sits in a net class named like a
    differential/impedance class, so MOTOR_P/MOTOR_N style pairs are not reported."""
    net_classes = net_classes or {}
    names = sorted(set(names))
    classes = {name: (net_classes[name].name if name in net_classes else "") for name in names}
    found = {name: guess for name in names if (guess := interface_guess(name, classes[name]))}
    pairs = tuple(pair for pair in differential_pairs(names)
                  if any(member in found or _FAST_CLASS.search(classes[member]) for member in pair))
    return HighSpeedNets(found, pairs)


# ---------------------------------------------------------------- per-net routing figures

@dataclass(frozen=True)
class NetRouting:
    length_mm: float
    delay_ps: float           # segment delays plus VIA_DELAY_PER_MM_PS x span thickness per via
    vias: int
    layers: frozenset[str]    # copper layers with track on this net
    assumed: bool             # some delay figure used the generic FR-4 default (or no model at all)


def _via_delay_ps(via: Via, copper: BoardCopper) -> float:
    """Via allowance: its barrel length (span share of board thickness) at FR-4 speed.

    ponytail: ignores pad/antipad capacitance and stubs; a field solver gives the real figure."""
    share = (len(via.layers) - 1) / max(len(copper.layers) - 1, 1)
    return homogeneous_delay_ps(share * copper.thickness_mm, FR4_EPSILON_R)


def net_routing(copper: BoardCopper) -> dict[str, NetRouting]:
    """Routed length, estimated delay, via count and layers for every net with copper tracks or vias."""
    fallback = homogeneous_delay_ps(1, FR4_EPSILON_R)
    totals: dict[str, list] = {}
    for seg in copper.segments:
        if not seg.net:
            continue
        row = totals.setdefault(seg.net, [0.0, 0.0, 0, set(), False])
        model = copper.models.get(seg.layer)
        row[0] += seg.length_mm
        row[1] += seg.length_mm * (model.delay_ps_per_mm(seg.width_mm) if model else fallback)
        row[3].add(seg.layer)
        row[4] = row[4] or model is None or model.assumed
    for via in copper.vias:
        if via.net:
            row = totals.setdefault(via.net, [0.0, 0.0, 0, set(), False])
            row[1] += _via_delay_ps(via, copper)
            row[2] += 1
    return {net: NetRouting(length, delay, count, frozenset(layers), assumed)
            for net, (length, delay, count, layers, assumed) in totals.items()}
