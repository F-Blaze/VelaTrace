"""Conservative vector reference-copper coverage diagnostics, not SI approval.

Inspired by the diagnostic problem in MIT KiCadRoutingTools check_impedance.py;
implemented independently using explicit net/layer requirements and full edges.
Only stored filled polygons count. Their freshness and ground connectivity must
be established separately; neither is implied by geometric coverage.
"""
from dataclasses import asdict, dataclass
from collections import defaultdict
import hashlib
import json
import math
import re

from .errors import CapabilityError, ValidationError
from .portfolio import candidate_geometry
from .routing import plan_digest
from .sexpr import QuotedAtom, children, one, parse
from .stackup import read_stackup


LIMITATIONS = (
    'Geometric coverage only; stored fill may be stale unless freshly regenerated for this candidate.',
    'Reference-net attachment, via return transitions, impedance and signal integrity are not proven.',
)


def geometry_library():
    try:
        import shapely
    except ImportError as exc:
        raise CapabilityError('Reference geometry requires the routing-research extra (Shapely 2.1.1).') from exc
    if shapely.__version__ != '2.1.1':
        raise CapabilityError('Reference geometry requires the tested Shapely 2.1.1 version.')
    return shapely


def _name(value):
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 for c in value)


@dataclass(frozen=True)
class ReferenceRequirement:
    net: str
    signal_layer: str
    reference_layer: str
    reference_net: str
    edge_margin_mm: float = 0

    def __post_init__(self):
        layer = r'(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu'
        if (not all(_name(n) for n in (self.net, self.signal_layer, self.reference_layer, self.reference_net))
                or not re.fullmatch(layer, self.signal_layer) or not re.fullmatch(layer, self.reference_layer)
                or self.signal_layer == self.reference_layer or self.net == self.reference_net
                or type(self.edge_margin_mm) not in {int, float}
                or not math.isfinite(self.edge_margin_mm) or not 0 <= self.edge_margin_mm <= 100):
            raise ValidationError('Reference requirements need explicit distinct nets/layers and a finite margin.')


def requirement_digest(requirements):
    if (type(requirements) is not tuple or not requirements or len(requirements) > 1024
            or any(type(item) is not ReferenceRequirement for item in requirements)
            or len(set(requirements)) != len(requirements)):
        raise ValidationError('Provide unique immutable reference requirements.')
    return hashlib.sha256(json.dumps([asdict(r) for r in requirements], sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _number(atom):
    try:
        value = float(atom)
    except (ValueError, TypeError):
        raise ValidationError('Invalid filled-polygon coordinate.') from None
    if not math.isfinite(value) or abs(value) > 1e6:
        raise ValidationError('Filled-polygon coordinate exceeds supported limits.')
    return value


def _filled_polygon(shape, coordinates):
    """Recover only KiCad's zero-width, out-and-back hole bridges.

    Polygonize un-noded edges after removing exact reversed duplicates. Ordinary
    bow-ties/crossings, unmatched bridges and open cuts remain errors. Select
    faces with the original contour's even/odd winding, preserving every hole.
    """
    polygon = shape.Polygon(coordinates)
    if polygon.is_valid and polygon.area > 1e-12:
        return polygon
    edges = defaultdict(list)
    closed = coordinates + [coordinates[0]] if coordinates[-1] != coordinates[0] else coordinates
    for a, b in zip(closed, closed[1:]):
        if a != b:
            edges[tuple(sorted((a, b)))].append((a, b))
    removed, remaining = 0, []
    for values in edges.values():
        if len(values) == 1:
            remaining.append(shape.LineString(values[0]))
        elif len(values) == 2 and values[0] == values[1][::-1]:
            removed += 1
        else:
            raise ValidationError('Ambiguous repeated edges in reference fill.')
    if not removed or not remaining:
        raise ValidationError('Invalid or degenerate reference fill contour.')
    faces, cuts, dangles, invalid = shape.polygonize_full(remaining)
    if any(not geometry.is_empty for geometry in (cuts, dangles, invalid)):
        raise ValidationError('Reference fill has unsupported crossing or open contours.')
    def inside(point):
        x, y, odd = point.x, point.y, False
        for (ax, ay), (bx, by) in zip(closed, closed[1:]):
            if (ay > y) != (by > y) and x < ax + (y-ay)*(bx-ax)/(by-ay):
                odd = not odd
        return odd
    kept = [face for face in faces.geoms if inside(face.representative_point())]
    if not kept:
        raise ValidationError('Reference fill contains no supported copper faces.')
    result = shape.union_all(kept)
    if not result.is_valid or result.is_empty:
        raise ValidationError('Reference fill faces could not be reconstructed safely.')
    return result


def reference_regions(filled_text, requirement):
    """Union known filled copper of the selected net on an adjacent copper layer.

    KiCad's exact reversed-edge hole bridges are supported. Other invalid or
    self-crossing contours are refused; no make_valid or outline fallback.
    """
    if type(requirement) is not ReferenceRequirement:
        raise ValidationError('Expected an explicit reference requirement.')
    shape = geometry_library()
    root = parse(filled_text, kicad=True)
    layers = read_stackup(filled_text).copper_layers
    if (requirement.signal_layer not in layers or requirement.reference_layer not in layers
            or abs(layers.index(requirement.signal_layer)-layers.index(requirement.reference_layer)) != 1):
        raise ValidationError('Only explicitly selected adjacent reference copper layers are supported.')
    net_ids = {}
    for row in children(root, 'net'):
        if len(row) != 3 or row[1] in net_ids:
            raise ValidationError('Ambiguous source net table.')
        net_ids[row[1]] = row[2]
    polygons, vertices = [], 0
    for zone in children(root, 'zone'):
        if children(zone, 'keepout'):
            continue
        declared = children(zone, 'layer') + children(zone, 'layers')
        if len(declared) != 1 or len(declared[0]) < 2:
            raise ValidationError('Reference zone has ambiguous layer membership.')
        zone_layers = declared[0][1:]
        if any(not isinstance(n, str) or '*' in n for n in zone_layers):
            raise ValidationError('Wildcard reference-zone layers are unsupported.')
        fills = children(zone, 'filled_polygon')
        if requirement.reference_layer not in zone_layers:
            # Do not accept a fill that claims a layer absent from its own zone.
            if any(children(f, 'layer') == [['layer', requirement.reference_layer]] for f in fills):
                raise ValidationError('Filled polygon lies on a layer its zone does not declare.')
            continue
        token = one(zone, 'net')
        if len(token) != 2:
            raise ValidationError('Reference zone has an invalid net.')
        name = token[1] if isinstance(token[1], QuotedAtom) else net_ids.get(token[1])
        label = children(zone, 'net_name')
        if name is None or len(label) > 1 or (label and (len(label[0]) != 2 or label[0][1] != name)):
            raise ValidationError('Reference zone has conflicting or unresolved net identity.')
        if name != requirement.reference_net:
            continue
        if not fills:
            raise ValidationError('Selected reference zone has no stored fill; refill this candidate first.')
        for fill in fills:
            flayer = one(fill, 'layer')
            if len(flayer) != 2 or flayer[1] not in zone_layers:
                raise ValidationError('Filled polygon layer disagrees with its zone.')
            if flayer[1] != requirement.reference_layer:
                continue
            if 'island' in fill or children(fill, 'island'):
                continue  # An island marker cannot establish a connected reference.
            if any(not isinstance(v, list) or not v or v[0] not in {'layer', 'pts'} for v in fill[1:]):
                raise ValidationError('Unsupported filled-polygon attributes.')
            points = one(fill, 'pts')[1:]
            vertices += len(points)
            if vertices > 200_000 or len(polygons) >= 4096:
                raise ValidationError('Reference fill exceeds the geometry budget.')
            if len(points) < 3 or any(len(p) != 3 or p[0] != 'xy' for p in points):
                raise ValidationError('Unsupported filled-polygon points.')
            poly = _filled_polygon(shape, [(_number(p[1]), _number(p[2])) for p in points])
            polygons.append(poly)
    if not polygons:
        raise ValidationError('No usable filled copper for the selected reference net/layer.')
    result = shape.union_all(polygons)
    if not result.is_valid or result.geom_type not in {'Polygon', 'MultiPolygon'}:
        raise ValidationError('Reference copper union is unsupported.')
    return result


@dataclass(frozen=True)
class CoverageCheck:
    requirement: ReferenceRequirement
    status: str
    affected_edges: tuple[tuple[int, int], ...] = ()
    details: str = ''


@dataclass(frozen=True)
class ReferenceCoverage:
    plan_digest: str
    filled_board_digest: str
    requirements_digest: str
    checks: tuple[CoverageCheck, ...]
    limitations: tuple[str, ...] = LIMITATIONS

    @property
    def status(self):
        statuses = {check.status for check in self.checks}
        return 'gap' if 'gap' in statuses else 'unknown' if 'unknown' in statuses else 'covered'

    @property
    def electrically_verified(self):
        return False


def check_reference_coverage(plan, filled_text, requirements):
    shape = geometry_library()
    fingerprint = requirement_digest(requirements)
    candidate_geometry(plan)
    if plan.trace_count > 20_000:
        raise ValidationError('Reference analysis exceeds the track-edge budget.')
    checks = []
    for requirement in requirements:
        selected = [(i, track) for i, track in enumerate(plan.tracks)
                    if (track.net, track.layer) == (requirement.net, requirement.signal_layer)]
        if not selected:
            checks.append(CoverageCheck(requirement, 'unknown', details='No matching signal tracks.'))
            continue
        try:
            region = reference_regions(filled_text, requirement)
        except ValidationError as exc:
            checks.append(CoverageCheck(requirement, 'unknown', details=str(exc)))
            continue
        failed = []
        for index, track in selected:
            for edge, (start, end) in enumerate(zip(track.points_mm, track.points_mm[1:])):
                line = shape.LineString(((start[0], -start[1]), (end[0], -end[1])))
                radius = track.width_mm/2 + requirement.edge_margin_mm
                # Continuous edge plus exact minimum boundary distance checks the
                # whole swept trace width. No sampling can skip a narrow void.
                if not region.covers(line) or line.distance(region.boundary) < radius + 1e-6:
                    failed.append((index, edge))
        if failed:
            checks.append(CoverageCheck(requirement, 'gap', tuple(failed),
                                        'Trace width/margin crosses missing reference copper.'))
        elif any(via.net == requirement.net for via in plan.vias):
            checks.append(CoverageCheck(requirement, 'unknown', details='Via return transitions are unverified.'))
        else:
            checks.append(CoverageCheck(requirement, 'covered', details='Full trace width/margin is geometrically covered.'))
    return ReferenceCoverage(plan_digest(plan), hashlib.sha256(filled_text.encode()).hexdigest(),
                             fingerprint, tuple(checks))
