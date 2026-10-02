"""Pure candidate normalization, never an editor or file mutation."""
from .errors import ValidationError
from .portfolio import candidate_geometry
from .ses import RoutePlan, Track


def consolidate_collinear(plan: RoutePlan) -> RoutePlan:
    """Union same-net/layer/width segments without changing quantized copper.

    Overlapping segments can leave object endpoints inside another track, which
    KiCad may flag as dangling. This representation change preserves the exact
    nanometre centerline union and widths. It is not a license to suppress DRC:
    the returned candidate still needs full connectivity and clearance checks.
    It never trims a branch, moves a trace, joins different nets or fills gaps.
    """
    before, _ = candidate_geometry(plan)
    tracks = []
    for net, layer, width, ux, uy, cross, start, end in before[1]:
        denominator = ux * ux + uy * uy
        points = []
        for projection in (start, end):
            x, rx = divmod(ux * projection - uy * cross, denominator)
            y, ry = divmod(uy * projection + ux * cross, denominator)
            if rx or ry:
                raise ValidationError('Consolidation would move an endpoint off the nanometre grid.')
            points.append((x / 1_000_000, y / 1_000_000))
        tracks.append(Track(net, layer, width / 1_000_000, tuple(points)))
    result = RoutePlan(plan.base_design, tuple(tracks), tuple(dict.fromkeys(plan.vias)))
    if candidate_geometry(result)[0] != before:
        raise ValidationError('Consolidation changed quantized copper; candidate refused.')
    return result
