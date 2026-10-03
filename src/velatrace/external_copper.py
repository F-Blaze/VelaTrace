"""Strict importer for copper added by an external file-based router.

Only straight tracks and standard through-vias are accepted. The input board is
the immutable routing baseline; every non-copper row must remain identical.
"""
from __future__ import annotations

import math
import uuid

from .candidate import board_nets, canonical, prepare_copper
from .errors import CapabilityError, ValidationError
from .ses import RoutePlan, Track, Via, ViaSpec
from .sexpr import children, one, parse, render


_UUID_FIELDS = {"uuid", "tstamp"}
_UUID_CHARS = frozenset("0123456789abcdef-")


def _number(value, what: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"External router returned an invalid {what}.") from None
    if not math.isfinite(result) or abs(result) > 1_000_000:
        raise ValidationError(f"External router returned an excessive {what}.")
    return result


def _single(row: list, name: str, size: int) -> list:
    found = children(row, name)
    if len(found) != 1 or len(found[0]) != size:
        raise ValidationError(f"External router returned malformed {name} geometry.")
    return found[0]


def _uuid(row: list, source_ids: set[str], new_ids: set[str]) -> str:
    item = _single(row, "uuid", 2)[1]
    value = str(item)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValidationError("External router returned an invalid copper UUID.") from None
    if parsed.version is None or str(parsed) != value.lower() or not set(value.lower()) <= _UUID_CHARS:
        raise ValidationError("External router returned a non-canonical copper UUID.")
    normalized = str(parsed)
    if normalized in source_ids or normalized in new_ids:
        raise ValidationError("External router returned a duplicate or conflicting copper UUID.")
    new_ids.add(normalized)
    return normalized


def _all_ids(root: list) -> set[str]:
    found = set()
    def visit(value):
        if not isinstance(value, list):
            return
        if value and value[0] in _UUID_FIELDS and len(value) == 2:
            found.add(str(value[1]).lower())
        for child in value:
            visit(child)
    visit(root)
    return found


def _non_copper_snapshot(root: list):
    """Use VelaTrace's canonicalizer per row while retaining top-level order.

    canonical() intentionally tolerates KiCad serialization reordering and
    generator fields. The explicit generator comparison below closes the latter
    gap, and row-by-row use preserves the original top-level sequence.
    """
    copper = {"segment", "arc", "via"}
    rows = [row for row in root[1:] if not (isinstance(row, list) and row and row[0] in copper)]
    # Nest each row twice so canonical() does not apply its intentional
    # top-level generator normalization to the row being compared.
    return tuple(canonical(["board-row", ["payload", row]]) for row in rows)


def import_copper(source_text: str, output_text: str, dsn, *,
                  via_catalog: dict[str, ViaSpec]) -> RoutePlan:
    """Convert a route-only external PCB result to an immutable ``RoutePlan``.

    ``source_text`` must be an unrouted board. ``via_catalog`` maps exact
    external via names to trusted project-backed geometries; dimensions are
    never inferred from untrusted external output.
    """
    source = parse(source_text, kicad=True)
    output = parse(output_text, kicad=True)
    if source[0] != "kicad_pcb" or output[0] != "kicad_pcb":
        raise ValidationError("External routing requires KiCad PCB source and output files.")
    if any(children(source, kind) for kind in ("segment", "arc", "via")):
        raise CapabilityError("External routing requires a board with no existing tracks or vias.")
    if len(via_catalog) > 10_000 or any(not isinstance(k, str) or type(v) is not ViaSpec
                                        for k, v in via_catalog.items()):
        raise ValidationError("External via catalog is invalid or oversized.")

    # Removing only recognized copper must restore the source document. This
    # rejects footprint, net, stackup, zone, rule, and metadata mutations.
    if _non_copper_snapshot(source) != _non_copper_snapshot(output):
        raise ValidationError("External router changed non-copper board data; output was refused.")

    layers_row = one(source, "layers")
    board_layers = {str(row[1]) for row in layers_row[1:] if isinstance(row, list)
                    and len(row) > 2 and row[2] in {"signal", "power", "mixed", "jumper"}}
    if board_layers != set(dsn.layers):
        raise ValidationError("Saved board copper layers differ from DSN.")
    net_map = board_nets(source)
    body_to_name = {}
    for name, body in net_map.items():
        if body in body_to_name:
            raise ValidationError("Saved board has ambiguous net identifiers.")
        body_to_name[body] = name

    source_ids = _all_ids(source)
    new_ids: set[str] = set()
    tracks: list[Track] = []
    vias: list[Via] = []

    for row in children(output, "arc"):
        raise ValidationError("External router returned unsupported curved copper.")
    segments = children(output, "segment")
    output_vias = children(output, "via")
    if not segments and not output_vias:
        raise ValidationError("External router returned no copper.")
    if len(segments) + len(output_vias) > 100_000:
        raise ValidationError("External router returned too many copper items.")

    for row in segments:
        allowed = {"start", "end", "width", "layer", "net", "uuid"}
        if any(not isinstance(attr, list) or not attr or attr[0] not in allowed for attr in row[1:]):
            raise ValidationError("External router returned unrecognized segment attributes.")
        start = _single(row, "start", 3)
        end = _single(row, "end", 3)
        width_row = _single(row, "width", 2)
        layer_row = _single(row, "layer", 2)
        net_row = _single(row, "net", 2)
        _uuid(row, source_ids, new_ids)
        layer = str(layer_row[1])
        net = body_to_name.get(render(net_row[1]))
        if layer not in board_layers or layer not in dsn.layers:
            raise ValidationError("External router returned an unsupported or unknown copper layer.")
        if net is None or net not in dsn.nets:
            raise ValidationError("External router returned an unknown or unexported net.")
        x1, y1 = _number(start[1], "coordinate"), _number(start[2], "coordinate")
        x2, y2 = _number(end[1], "coordinate"), _number(end[2], "coordinate")
        width = _number(width_row[1], "track width")
        if not 0 < width <= 100 or (x1, y1) == (x2, y2):
            raise ValidationError("External router returned empty or invalid segment geometry.")
        # KiCad PCB files are Y-down; RoutePlan coordinates are Specctra Y-up.
        tracks.append(Track(net, layer, width, ((x1, -y1), (x2, -y2))))

    for row in output_vias:
        allowed = {"at", "size", "drill", "layers", "net", "uuid", "type"}
        if any(not isinstance(attr, list) or not attr or attr[0] not in allowed for attr in row[1:]):
            raise ValidationError("External router returned unrecognized via attributes.")
        at = _single(row, "at", 3)
        size_row = _single(row, "size", 2)
        drill_row = _single(row, "drill", 2)
        layers = _single(row, "layers", 3)
        net_row = _single(row, "net", 2)
        _uuid(row, source_ids, new_ids)
        if len(children(row, "type")) > 1 or (children(row, "type") and
                (len(children(row, "type")[0]) != 2 or children(row, "type")[0][1] != "through")):
            raise ValidationError("Only standard through-vias are supported.")
        if tuple(map(str, layers[1:])) != ("F.Cu", "B.Cu"):
            raise ValidationError("Only standard F.Cu-to-B.Cu through-vias are supported.")
        net = body_to_name.get(render(net_row[1]))
        if net is None or net not in dsn.nets:
            raise ValidationError("External router returned an unknown or unexported via net.")
        x, y = _number(at[1], "via coordinate"), _number(at[2], "via coordinate")
        diameter = _number(size_row[1], "via diameter")
        drill = _number(drill_row[1], "via drill")
        if not 0 < drill < diameter <= 100:
            raise ValidationError("External router returned invalid via dimensions.")
        diameter_nm, drill_nm = round(diameter * 1_000_000), round(drill * 1_000_000)
        spec = next((known for known in via_catalog.values()
                     if round(known.diameter_mm * 1_000_000) == diameter_nm
                     and round(known.drill_mm * 1_000_000) == drill_nm
                     and tuple(known.layers) == ("F.Cu", "B.Cu")), None)
        if spec is None:
            raise ValidationError("External via dimensions do not match a trusted via catalog entry.")
        vias.append(Via(net, (x, -y), spec))

    plan = RoutePlan(getattr(dsn, "base_design", ""), tuple(tracks), tuple(vias))
    # Reuse the same geometry/source safety checks applied to native router plans.
    prepare_copper(plan, dsn, source=source_text)
    return plan
