"""Project missing reference copper into router keepouts; never edits a board.

The projection steers geometry; it is not proof of coverage after routing. Every
candidate must be freshly refilled, checked for coverage and independently DRC'd.
Keepouts affect all nets on a profiled signal layer, a conservative restriction.
"""
from dataclasses import dataclass
import hashlib
from collections import defaultdict

from .dsn import dsn_scale
from .electrical_rules import _serialize
from .errors import ValidationError
from .reference_planes import geometry_library, reference_regions, requirement_digest
from .sexpr import QuotedAtom, children, one, parse


@dataclass(frozen=True)
class ReferenceProjection:
    dsn_text: str
    filled_board_digest: str
    requirements_digest: str
    keepout_count: int


def _merge_convex_neighbors(polygons, *, max_checks=50_000, max_passes=12):
    """Greedily merge edge-neighbor polygons when their union is already convex.

    Exact shared-edge indexing avoids all-pairs scans. Each pass merges disjoint
    pairs, so a bounded number of passes and candidate checks caps extra work;
    any polygons left behind remain the original exact triangulation pieces.
    """
    current = list(polygons)
    checks = 0
    for _ in range(max_passes):
        edges = defaultdict(list)
        for index, polygon in enumerate(current):
            coordinates = list(polygon.exterior.coords)
            for a, b in zip(coordinates, coordinates[1:]):
                if a != b:
                    edges[tuple(sorted((a, b)))].append(index)

        used = set()
        merged = []
        changed = False
        for owners in edges.values():
            if len(owners) != 2 or owners[0] == owners[1]:
                continue
            left, right = owners
            if left in used or right in used:
                continue
            if checks >= max_checks:
                break
            checks += 1
            candidate = current[left].union(current[right])
            if (candidate.geom_type == 'Polygon' and candidate.is_valid
                    and candidate.equals(candidate.convex_hull)):
                used.update((left, right))
                merged.append(candidate)
                changed = True
        if not changed:
            break
        merged.extend(polygon for index, polygon in enumerate(current) if index not in used)
        current = merged
        if checks >= max_checks:
            break
    return current


def compile_reference_keepouts(dsn_text, filled_text, requirements, *, merge_convex=False):
    """Keep existing DSN content and add layer-wide projected copper exclusions.

    Only one simple closed board boundary is supported. Constrained triangulation
    represents holes without filling them accidentally. This is a steering hint:
    output quantization and router clearance handling require final verification.
    Exact convex compaction is opt-in: reduced polygon count has not demonstrated
    a consistent end-to-end speed improvement on the repeated native corpus.
    """
    if type(merge_convex) is not bool:
        raise ValidationError('Reference keepout merging option must be boolean.')
    shape = geometry_library()
    fingerprint = requirement_digest(requirements)
    root = parse(dsn_text)
    if root[0] != 'pcb':
        raise ValidationError('Reference projection requires a PCB DSN.')
    structure = one(root, 'structure')
    layers = {row[1] for row in children(structure, 'layer')}
    nets = {row[1] for row in children(one(root, 'network'), 'net')}
    if any(r.net not in nets or r.signal_layer not in layers or r.reference_layer not in layers
           for r in requirements):
        raise ValidationError('Reference projection names a net or layer absent from the DSN.')
    boundary = one(structure, 'boundary')
    if len(boundary) != 2 or not isinstance(boundary[1], list):
        raise ValidationError('Reference projection needs one simple board boundary.')
    border, scale = boundary[1], dsn_scale(root)
    try:
        from .reference_planes import _number
        if border[:2] == ['rect', 'pcb'] and len(border) == 6:
            x1, y1, x2, y2 = [_number(v)*scale for v in border[2:]]
            board = shape.box(min(x1, x2), min(-y1, -y2), max(x1, x2), max(-y1, -y2))
        elif (border[:2] == ['path', 'pcb'] and len(border) >= 11 and len(border) % 2 == 1
              and _number(border[2]) == 0):
            coords = [(_number(border[i])*scale, -_number(border[i+1])*scale) for i in range(3, len(border), 2)]
            if coords[0] != coords[-1]:
                raise ValidationError('Reference projection needs an explicitly closed board boundary.')
            board = shape.Polygon(coords)
        else:
            raise ValidationError('Unsupported reference-projection board outline.')
    except (TypeError, IndexError) as exc:
        raise ValidationError('Malformed reference-projection board outline.') from exc
    if not board.is_valid or board.is_empty or board.area <= 0:
        raise ValidationError('Invalid reference-projection board outline.')
    supported = {}
    for requirement in requirements:
        region = reference_regions(filled_text, requirement)
        if requirement.edge_margin_mm:
            region = region.buffer(-requirement.edge_margin_mm, quad_segs=32)
        key = requirement.signal_layer
        supported[key] = region if key not in supported else supported[key].intersection(region)
    count = 0
    for layer, copper in sorted(supported.items()):
        forbidden = board.difference(copper)
        if not forbidden.is_valid or forbidden.geom_type not in {'Polygon', 'MultiPolygon'}:
            raise ValidationError('Unsupported projected reference voids.')
        if forbidden.is_empty:
            continue
        triangles = shape.constrained_delaunay_triangles(forbidden)
        if len(triangles.geoms) + count > 10_000:
            raise ValidationError('Projected reference voids exceed the keepout budget.')
        if not shape.union_all(triangles.geoms).equals(forbidden):
            raise ValidationError('Reference triangulation did not preserve all voids.')
        convex_parts = (_merge_convex_neighbors(triangles.geoms) if merge_convex
                        else list(triangles.geoms))
        if not shape.union_all(convex_parts).equals(forbidden):
            raise ValidationError('Convex reference merging did not preserve all voids.')
        for polygon in convex_parts:
            coordinates = list(polygon.exterior.coords)[:-1]
            if (len(coordinates) < 3 or polygon.area <= 0 or not polygon.is_valid
                    or not polygon.equals(polygon.convex_hull)):
                raise ValidationError('Reference projection produced invalid convex keepouts.')
            # Convert KiCad Y-down millimeters to the DSN's Y-up native units.
            points = [format(value, '.12g') for x, y in coordinates for value in (x/scale, -y/scale)]
            count += 1
            structure.append(['keepout', QuotedAtom(f'VelaTrace-reference-{count}'),
                              ['polygon', layer, '0', *points]])
    return ReferenceProjection(_serialize(root), hashlib.sha256(filled_text.encode()).hexdigest(),
                               fingerprint, count)
