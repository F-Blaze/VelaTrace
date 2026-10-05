"""Advisory delay estimates for one explicitly named, front-layer pin path.

This intentionally narrow estimate uses an ideal zero-thickness microstrip
model. It is neither impedance validation nor signal-integrity approval.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math

from .errors import ValidationError
from .sexpr import QuotedAtom, children, one, parse
from .stackup import read_stackup, thin_microstrip
from .routing import plan_digest
from .portfolio import candidate_geometry


_LIMITATIONS = (
    "Advisory zero-thickness quasi-static microstrip estimate; no mask, loss, dispersion, frequency, coupling, or manufacturing tolerance.",
    "Reference copper attachment, filled-plane continuity, return transitions, impedance, and signal integrity are not verified.",
    "Only F.Cu paths with adjacent In1.Cu reference copper, zero-rotation front pads, and no vias are supported.",
)


@dataclass(frozen=True)
class EndpointRequirement:
    net: str
    start_reference: str
    start_pad: str
    end_reference: str
    end_pad: str
    reference_net: str
    signal_layer: str = "F.Cu"
    reference_layer: str = "In1.Cu"

    def __post_init__(self):
        if (any(not isinstance(x, str) or not x or any(ord(c) < 32 for c in x)
                for x in (self.net, self.start_reference, self.start_pad,
                          self.end_reference, self.end_pad, self.reference_net))
                or self.start_reference == self.end_reference and self.start_pad == self.end_pad
                or self.signal_layer != "F.Cu" or self.reference_layer != "In1.Cu"
                or self.net == self.reference_net):
            raise ValidationError("Endpoint timing needs distinct explicit pins and an F.Cu/In1.Cu reference pair.")


@dataclass(frozen=True)
class EndpointDelayReport:
    status: str
    board_digest: str
    stackup_digest: str
    plan_digest: str
    requirement_digest: str
    path_length_mm: float | None = None
    estimated_delay_ps: float | None = None
    details: str = ""
    limitations: tuple[str, ...] = _LIMITATIONS

    @property
    def electrically_verified(self):
        return False


def _nm(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e6:
        raise ValidationError("Endpoint geometry contains an invalid coordinate.")
    return round(value * 1_000_000)


def _atom(value):
    return str(value) if isinstance(value, str) else None


def _net_name(pad, net_codes):
    rows = children(pad, "net")
    if len(rows) != 1:
        return None
    row = rows[0]
    if len(row) == 2 and isinstance(row[1], QuotedAtom):
        return _atom(row[1])
    if len(row) == 3 and net_codes.get(_atom(row[1])) == row[2]:
        return row[2]
    return None


def _pin_points(board, requirement):
    nets = {}
    for row in children(board, "net"):
        if len(row) != 3 or row[1] in nets:
            raise ValidationError("Board net table is ambiguous.")
        nets[_atom(row[1])] = _atom(row[2])
    found = {}
    for fp in children(board, "footprint"):
        refs = [r[2] for r in children(fp, "property") if len(r) >= 3 and r[1] == "Reference"]
        refs += [r[2] for r in children(fp, "fp_text") if len(r) >= 3 and r[1] == "reference"]
        wanted = {requirement.start_reference, requirement.end_reference}
        if not wanted.intersection(map(str, refs)):
            continue
        if len(set(map(str, refs))) != 1:
            raise ValidationError("Endpoint footprint reference is ambiguous.")
        if one(fp, 'layer') != ['layer', 'F.Cu']:
            raise ValidationError('Only front-side endpoint footprints are supported.')
        at = one(fp, "at")
        if len(at) not in (3, 4) or float(at[3] if len(at) > 3 else 0) != 0:
            raise ValidationError("Only zero-rotation endpoint footprints are supported.")
        origin = (_nm(float(at[1])), _nm(float(at[2])))
        ref = str(refs[0])
        for pad in children(fp, "pad"):
            if len(pad) < 2:
                continue
            number = str(pad[1])
            if (ref, number) not in {(requirement.start_reference, requirement.start_pad),
                                     (requirement.end_reference, requirement.end_pad)}:
                continue
            rows = children(pad, "at")
            if len(rows) != 1 or len(rows[0]) not in (3, 4):
                raise ValidationError("Endpoint pad position is missing or ambiguous.")
            local = rows[0]
            if len(local) == 4 and float(local[3]) != 0:
                raise ValidationError("Only zero-rotation endpoint pads are supported.")
            layers = children(pad, "layers")
            if len(layers) != 1 or "F.Cu" not in layers[0][1:]:
                raise ValidationError("Endpoint pads must explicitly include F.Cu.")
            if _net_name(pad, nets) != requirement.net:
                raise ValidationError("Endpoint pad is missing or belongs to a different net.")
            key = (ref, number)
            if key in found:
                raise ValidationError("Endpoint pad identity is duplicated.")
            found[key] = (origin[0] + _nm(float(local[1])), origin[1] + _nm(float(local[2])))
    start = found.get((requirement.start_reference, requirement.start_pad))
    end = found.get((requirement.end_reference, requirement.end_pad))
    if start is None or end is None:
        raise ValidationError("An explicitly required endpoint pad is missing.")
    if start == end:
        raise ValidationError("Endpoint pads quantize to the same location.")
    declared_nets = set(nets.values())
    for fp in children(board, 'footprint'):
        declared_nets.update(_net_name(p, nets) for p in children(fp, 'pad'))
    for zone in children(board, 'zone'):
        row = children(zone, 'net')
        if len(row) == 1 and len(row[0]) == 2 and isinstance(row[0][1], QuotedAtom):
            declared_nets.add(row[0][1])
    if requirement.reference_net not in declared_nets:
        raise ValidationError("The explicit reference net is absent from the board.")
    return start, end


def _cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def _on(a, b, p):
    return _cross(a, b, p) == 0 and min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def _intersects(first, second):
    a, b = first; c, d = second
    x1, x2, x3, x4 = _cross(a,b,c), _cross(a,b,d), _cross(c,d,a), _cross(c,d,b)
    return ((x1 == 0 and _on(a,b,c)) or (x2 == 0 and _on(a,b,d))
            or (x3 == 0 and _on(c,d,a)) or (x4 == 0 and _on(c,d,b))
            or (x1 < 0 < x2 or x2 < 0 < x1) and (x3 < 0 < x4 or x4 < 0 < x3))


def _path(plan, requirement, start, end, height_mm, epsilon_r):
    if any(v.net == requirement.net for v in plan.vias):
        raise ValidationError("Endpoint path contains an unsupported via transition.")
    tracks = [t for t in plan.tracks if t.net == requirement.net]
    if not tracks or any(t.layer != "F.Cu" for t in tracks):
        raise ValidationError("Endpoint path is missing or leaves F.Cu.")
    edges, graph, seen = [], {}, set()
    for track in tracks:
        if len(track.points_mm) < 2 or not math.isfinite(track.width_mm) or track.width_mm <= 0:
            raise ValidationError("Endpoint route geometry is invalid.")
        # Route plans use Y-up; saved KiCad footprint/pad coordinates use Y-down.
        points = tuple((_nm(x), -_nm(y)) for x, y in track.points_mm)
        for a, b in zip(points, points[1:]):
            if a == b:
                raise ValidationError("Endpoint path has a zero-length segment.")
            key = (*sorted((a, b)), _nm(track.width_mm))
            if key in seen:
                continue
            seen.add(key)
            edge_id = len(edges)
            edges.append((a, b, track.width_mm))
            graph.setdefault(a, []).append((b, edge_id))
            graph.setdefault(b, []).append((a, edge_id))
            if len(edges) > 512:
                raise ValidationError("Endpoint topology exceeds its bounded geometry budget.")
    for i, (a,b,_) in enumerate(edges):
        for c,d,_ in edges[i+1:]:
            if not _intersects((a,b), (c,d)):
                continue
            shared = {a,b} & {c,d}
            if len(shared) != 1 or any(p not in shared and _on(a,b,p) for p in (c,d)) or any(p not in shared and _on(c,d,p) for p in (a,b)):
                raise ValidationError("Endpoint route has an overlap or unrepresented interior junction.")
    if start not in graph or end not in graph:
        raise ValidationError("Route does not reach both endpoint pads.")
    # Check the full connected component, including branches beyond the end pin.
    stack = [start]
    parents = {start: (None, None)}
    while stack:
        node = stack.pop()
        for neighbor, edge_id in graph[node]:
            if neighbor == parents[node][0]:
                continue
            if neighbor in parents:
                raise ValidationError("Endpoint route contains a cycle or ambiguous branch topology.")
            parents[neighbor] = node, edge_id
            stack.append(neighbor)
    if end not in parents:
        raise ValidationError("Endpoint pads are disconnected or have multiple paths.")
    path_edges, node = [], end
    while node != start:
        node, edge_id = parents[node]
        path_edges.append(edge_id)
    length = math.fsum(math.hypot(edges[i][1][0]-edges[i][0][0], edges[i][1][1]-edges[i][0][1])
                       / 1_000_000 for i in path_edges)
    delay = math.fsum(math.hypot(edges[i][1][0]-edges[i][0][0], edges[i][1][1]-edges[i][0][1])
                      / 1_000_000 * thin_microstrip(edges[i][2], height_mm, epsilon_r).delay_ps_per_mm
                      for i in path_edges)
    return length, delay


def analyze_endpoint_delay(board_text, plan, requirement):
    """Return a hash-bound advisory estimate, or `unknown` when inputs exceed scope."""
    if type(requirement) is not EndpointRequirement:
        raise ValidationError("Expected an explicit endpoint timing requirement.")
    candidate_geometry(plan)
    stackup_digest = ""
    try:
        board = parse(board_text, kicad=True)
        if not board or board[0] != "kicad_pcb":
            raise ValidationError("Expected a KiCad board.")
        stackup = read_stackup(board_text)
        stackup_digest = stackup.fingerprint
        if stackup.copper_layers[:2] != ("F.Cu", "In1.Cu"):
            raise ValidationError("Explicit F.Cu/In1.Cu reference layers are unavailable.")
        physical = tuple(layer for layer in stackup.layers if layer.kind != 'surface')
        dielectric = physical[1] if len(physical) > 1 and physical[1].kind == 'dielectric' else None
        if (dielectric is None or len(dielectric.sublayers) != 1
                or dielectric.sublayers[0].thickness_nm is None
                or dielectric.sublayers[0].epsilon_r is None):
            raise ValidationError("A single declared F.Cu/In1.Cu dielectric thickness and epsilon_r are required.")
        height = dielectric.sublayers[0].thickness_nm / 1_000_000
        epsilon = dielectric.sublayers[0].epsilon_r
        start, end = _pin_points(board, requirement)
        length, delay = _path(plan, requirement, start, end, height, epsilon)
        status, details = "estimated", "Connected endpoint path estimated with the declared dielectric and ideal microstrip model."
    except (ValidationError, ValueError, TypeError, IndexError, KeyError) as exc:
        length = delay = None
        status, details = "unknown", str(exc)
    req_digest = hashlib.sha256(json.dumps(asdict(requirement), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return EndpointDelayReport(status, hashlib.sha256(board_text.encode()).hexdigest(), stackup_digest,
                               plan_digest(plan), req_digest, length, delay, details)
