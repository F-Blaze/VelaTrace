"""Compile conservative, already measured copper obstacles into DSN keepouts.

Coordinates are KiCad millimeters with Y increasing downwards. This module only
converts supplied bounds; it does not infer text or other board geometry.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math
import re

from .dsn import dsn_scale
from .electrical_rules import _serialize
from .errors import ValidationError
from .ses import resolution
from .sexpr import QuotedAtom, children, one, parse


MAX_OBSTACLES = 100_000
MAX_DSN_CHARS = 32_000_000
_CANONICAL_COPPER_LAYER = re.compile(r"(?:F|B)\.Cu|In(?:[1-9]|[12][0-9]|30)\.Cu")


def _valid_layer(value):
    return isinstance(value, str) and _CANONICAL_COPPER_LAYER.fullmatch(value) is not None


@dataclass(frozen=True)
class CopperObstacle:
    """One axis-aligned copper exclusion box in KiCad Y-down millimeters."""

    layer: str
    xmin_mm: float
    ymin_mm: float
    xmax_mm: float
    ymax_mm: float

    def __post_init__(self):
        bounds = (self.xmin_mm, self.ymin_mm, self.xmax_mm, self.ymax_mm)
        if (not _valid_layer(self.layer)
                or any(type(value) not in {int, float} or not math.isfinite(value)
                       for value in bounds)
                or any(abs(value) > 1_000_000 for value in bounds)
                or self.xmin_mm >= self.xmax_mm or self.ymin_mm >= self.ymax_mm):
            raise ValidationError("Copper obstacle needs a canonical copper layer and bounded, finite, nonzero bounds.")


def compile_copper_keepouts(dsn_text: str, obstacles: tuple[CopperObstacle, ...]) -> str:
    """Append exact rectangular obstacle bounds as DSN polygon keepouts.

    The supplied boxes are already conservative bounds in board millimeters.
    Their Y coordinate is flipped once when converted to DSN's Y-up coordinates;
    no clearance or additional margin is added here.
    """
    if type(obstacles) is not tuple or len(obstacles) > MAX_OBSTACLES:
        raise ValidationError("Copper obstacles must be a bounded immutable tuple.")
    if any(type(item) is not CopperObstacle for item in obstacles):
        raise ValidationError("Copper obstacles must use the immutable CopperObstacle type.")
    if not isinstance(dsn_text, str) or len(dsn_text) > MAX_DSN_CHARS:
        raise ValidationError("Specctra input exceeds the 32 MB limit.")

    root = parse(dsn_text)
    if root[0] != "pcb":
        raise ValidationError("Copper obstacles require a DSN PCB root.")
    structure = one(root, "structure")
    layers = set()
    for row in children(structure, "layer"):
        if len(row) < 3 or not _valid_layer(row[1]) or row[1] in layers:
            raise ValidationError("DSN contains a malformed or duplicate copper layer.")
        types = children(row, "type")
        if (len(types) != 1 or len(types[0]) != 2 or not isinstance(types[0][1], str)
                or types[0][1] not in {"signal", "power"}):
            raise ValidationError("DSN copper layer must have a signal or power type.")
        layers.add(row[1])
    if not layers or any(item.layer not in layers for item in obstacles):
        raise ValidationError("Copper obstacle names a layer absent from the DSN.")

    scale = dsn_scale(root)
    declarations = children(root, 'resolution')
    if len(declarations) > 1:
        raise ValidationError('Duplicate DSN resolution.')
    quantum = Decimal(str(resolution(declarations[0]))) if declarations else None
    if not obstacles:
        return dsn_text

    used_names = set()
    for keepout in children(structure, "keepout"):
        if len(keepout) < 2 or not isinstance(keepout[1], str):
            raise ValidationError("DSN contains a malformed keepout name.")
        used_names.add(str(keepout[1]))

    # Stable ordering gives stable identifiers even when callers gather boxes
    # from independent sources in a different order.
    ordered = sorted(obstacles, key=lambda item: (
        item.layer, item.xmin_mm, item.ymin_mm, item.xmax_mm, item.ymax_mm))
    next_number = 1
    for obstacle in ordered:
        while f"VelaTrace-copper-obstacle-{next_number}" in used_names:
            next_number += 1
        name = f"VelaTrace-copper-obstacle-{next_number}"
        used_names.add(name)
        next_number += 1

        # Transform the four corners from KiCad Y-down mm to DSN native Y-up
        # units. Values are serialized without rounding to an arbitrary grid.
        xlo, ylo, xhi, yhi = map(lambda v: Decimal(str(v)),
                                (obstacle.xmin_mm, obstacle.ymin_mm, obstacle.xmax_mm, obstacle.ymax_mm))
        if quantum is not None:
            # A declared DSN grid must never round an exclusion inward. This is
            # conservative enclosure, not a relaxation/change of design clearance.
            xlo, ylo = ((v/quantum).to_integral_value(rounding=ROUND_FLOOR)*quantum for v in (xlo, ylo))
            xhi, yhi = ((v/quantum).to_integral_value(rounding=ROUND_CEILING)*quantum for v in (xhi, yhi))
        coordinates = ((xlo, ylo), (xhi, ylo), (xhi, yhi), (xlo, yhi))
        unit = Decimal(str(scale))
        points = [str(value) for x, y in coordinates for value in (x/unit, -y/unit)]
        structure.append(["keepout", QuotedAtom(name),
                          ["polygon", obstacle.layer, "0", *points]])

    result = _serialize(root)
    if len(result) > MAX_DSN_CHARS:
        raise ValidationError("Compiled Specctra output exceeds the 32 MB limit.")
    return result
