"""All-or-nothing SES preflight for straight paths and explicitly known vias.

SES coordinates use the Specctra Y-up convention. Convert to KiCad Y-down only
in the board adapter. A syntactically valid plan does not imply DRC or completion.
"""
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Mapping

from .errors import ValidationError
from .sexpr import children, one, parse


@dataclass(frozen=True)
class Track:
    net: str
    layer: str
    width_mm: float
    points_mm: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ViaSpec:
    diameter_mm: float
    drill_mm: float
    layers: tuple[str, str]

    def __post_init__(self):
        if (type(self.diameter_mm) not in {int, float} or type(self.drill_mm) not in {int, float}
                or not math.isfinite(self.diameter_mm) or not math.isfinite(self.drill_mm)
                or not 0 < self.drill_mm < self.diameter_mm <= 100
                or len(self.layers) != 2 or len(set(self.layers)) != 2
                or any(not isinstance(layer, str) or not layer for layer in self.layers)):
            raise ValidationError("Invalid trusted via geometry.")


@dataclass(frozen=True)
class Via:
    net: str
    position_mm: tuple[float, float]
    spec: ViaSpec


@dataclass(frozen=True)
class RoutePlan:
    base_design: str
    tracks: tuple[Track, ...]
    vias: tuple[Via, ...]

    @property
    def trace_count(self) -> int:
        return sum(len(track.points_mm) - 1 for track in self.tracks)

    @property
    def layers_used(self) -> tuple[str, ...]:
        return tuple(sorted({track.layer for track in self.tracks} |
                            {layer for via in self.vias for layer in via.spec.layers}))


def number(value: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValidationError("SES contains a non-numeric coordinate.") from None
    if not math.isfinite(result) or abs(result) > 1e12:
        raise ValidationError("SES contains an invalid or excessive coordinate.")
    return result


def resolution(node: list) -> float:
    if len(node) != 3 or node[1] not in {"mm", "um", "mil", "inch"}:
        raise ValidationError("Unsupported Specctra coordinate resolution.")
    divisor = number(node[2])
    if divisor <= 0:
        raise ValidationError("Specctra resolution must be positive.")
    result = {"mm": 1, "um": .001, "mil": .0254, "inch": 25.4}[node[1]] / divisor
    if not math.isfinite(result) or not 1e-12 <= result <= 1e6:
        raise ValidationError("Specctra resolution is outside supported bounds.")
    return result


def coordinate(value: str, scale: float) -> float:
    result = number(value) * scale
    if not math.isfinite(result) or abs(result) > 1e6:
        raise ValidationError("Specctra coordinate is outside supported board bounds.")
    return result


def _sections(node: list, allowed: set[str], offset: int = 1):
    if any(not isinstance(item, list) or not item or item[0] not in allowed for item in node[offset:]):
        raise ValidationError(f"Unsupported SES content in {node[0]}; the whole result was refused.")


def parse_ses(text: str, *, expected_design: str, nets: set[str], layers: set[str],
              via_catalog: Mapping[str, ViaSpec] | None = None,
              expected_placements: Mapping[str, tuple[float, float, str, float]] | None = None) -> RoutePlan:
    root = parse(text)
    if root[0] != "session" or len(root) < 3 or not isinstance(root[1], str):
        raise ValidationError("Expected a Specctra session root.")
    _sections(root, {"base_design", "routes", "placement", "was_is"}, 2)
    base = one(root, "base_design")
    if len(base) != 2 or not isinstance(base[1], str) or base[1] not in {Path(expected_design).name, Path(expected_design).stem}:
        raise ValidationError("SES base design does not match the exported DSN.")
    for name in ("placement", "was_is"):
        if len(children(root, name)) > 1:
            raise ValidationError("Duplicate SES placement metadata.")
    if any(row != ["was_is"] for row in children(root, "was_is")):
        raise ValidationError("SES component renaming is unsupported.")
    for placement in children(root, "placement"):
        if expected_placements is None:
            raise ValidationError("SES placement requires verified original DSN placements.")
        _sections(placement, {"resolution", "component"})
        place_scale = resolution(one(placement, "resolution"))
        seen_places = set()
        for component in children(placement, "component"):
            if len(component) < 2 or not isinstance(component[1], str):
                raise ValidationError("Malformed SES component placement.")
            _sections(component, {"place"}, 2)
            for place in component[2:]:
                if len(place) != 6 or place[1] in seen_places or place[1] not in expected_placements:
                    raise ValidationError("Unknown, duplicate or unsupported SES placement.")
                seen_places.add(place[1])
                x, y, side, angle = expected_placements[place[1]]
                if (abs(coordinate(place[2], place_scale)-x) > 1e-6 or
                        abs(coordinate(place[3], place_scale)-y) > 1e-6 or place[4] != side or
                        abs(number(place[5])-angle) > 1e-6):
                    raise ValidationError("SES moved, rotated or flipped a footprint; entire route refused.")
        if seen_places != set(expected_placements):
            raise ValidationError("SES placement reference list changed.")
    routes = one(root, "routes")
    _sections(routes, {"resolution", "parser", "library_out", "network_out"})
    if any(len(children(routes, name)) > 1 for name in {"parser", "library_out"}):
        raise ValidationError("Duplicate SES metadata sections.")
    scale = resolution(one(routes, "resolution"))
    # Parser metadata cannot carry geometry. Restrict its standard informational fields.
    for parser in children(routes, "parser"):
        _sections(parser, {"host_cad", "host_version", "string_quote", "space_in_quoted_tokens"})
        if any(any(isinstance(value, list) for value in item[1:]) for item in parser[1:]):
            raise ValidationError("Malformed SES parser metadata.")
    # Padstack definitions are never trusted to choose a drill or layer pair.
    for library in children(routes, "library_out"):
        _sections(library, {"padstack"})
        known = {}
        for padstack in library[1:]:
            if len(padstack) < 3 or not isinstance(padstack[1], str):
                raise ValidationError("Malformed SES padstack.")
            if padstack[1] in known and padstack != known[padstack[1]]:
                raise ValidationError("Conflicting duplicate SES padstack.")
            known[padstack[1]] = padstack
            spec = (via_catalog or {}).get(padstack[1])
            if spec is None:
                raise ValidationError("SES padstack lacks a verified board drill/layer mapping.")
            _sections(padstack, {"shape", "attach"}, 2)
            circle_layers = set()
            for shape in children(padstack, "shape"):
                if len(shape) != 2 or not isinstance(shape[1], list):
                    raise ValidationError("Unsupported SES padstack shape.")
                circle = shape[1]
                if (len(circle) not in {3, 5} or circle[0] != "circle" or not isinstance(circle[1], str) or circle[1] not in layers
                        or circle[1] in circle_layers or abs(number(circle[2]) * scale - spec.diameter_mm) > 1e-6
                        or (len(circle) == 5 and (number(circle[3]) != 0 or number(circle[4]) != 0))):
                    raise ValidationError("SES padstack geometry differs from verified board via.")
                circle_layers.add(circle[1])
            if not set(spec.layers) <= circle_layers:
                raise ValidationError("SES via layer span differs from verified board via.")
            if any(attach != ["attach", "off"] for attach in children(padstack, "attach")):
                raise ValidationError("Unsupported SES padstack attachment.")
    network = one(routes, "network_out")
    _sections(network, {"net"})
    tracks, vias, seen = [], [], set()
    for net in network[1:]:
        if len(net) < 2 or not isinstance(net[1], str) or net[1] not in nets or net[1] in seen:
            raise ValidationError("SES contains an unknown or duplicate net.")
        seen.add(net[1])
        _sections(net, {"wire", "via"}, 2)
        for geometry in net[2:]:
            if geometry[0] == "wire":
                _sections(geometry, {"path", "type"})
                for kind in children(geometry, "type"):
                    if kind not in [["type", "route"], ["type", "normal"]]:
                        raise ValidationError("Unsupported SES wire type.")
                path = one(geometry, "path")
                if len(path) < 7 or len(path) % 2 != 1 or not isinstance(path[1], str) or path[1] not in layers:
                    raise ValidationError("Unsupported SES path or layer.")
                width = number(path[2]) * scale
                if not 0 < width <= 100:
                    raise ValidationError("Invalid SES track width.")
                points = tuple((coordinate(path[i], scale), coordinate(path[i+1], scale))
                               for i in range(3, len(path), 2))
                if any(a == b for a, b in zip(points, points[1:])):
                    raise ValidationError("Zero-length SES track segment.")
                tracks.append(Track(net[1], path[1], width, points))
            else:
                if len(geometry) not in {4, 5} or not isinstance(geometry[1], str):
                    raise ValidationError("Unsupported SES via geometry.")
                if len(geometry) == 5 and geometry[4] != ["type", "route"]:
                    raise ValidationError("Unsupported SES via type.")
                spec = (via_catalog or {}).get(geometry[1])
                if spec is None or not 0 < spec.drill_mm < spec.diameter_mm <= 100 or not set(spec.layers) <= layers:
                    raise ValidationError("SES via lacks a verified board padstack/drill mapping.")
                vias.append(Via(net[1], (coordinate(geometry[2], scale), coordinate(geometry[3], scale)), spec))
    return RoutePlan(base[1], tuple(tracks), tuple(vias))
