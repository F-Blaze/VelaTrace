"""EasyEDA/LCSC component data -> the pads of the real part. Input is untrusted network data."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

MAX_PADS = 2000          # no real footprint comes close; more means junk or an attack
_MM_PER_UNIT = 0.254     # EasyEDA canvas unit = 10 mil
_LIMIT = 1e6             # canvas units; larger is not a footprint


@dataclass(frozen=True)
class RealPad:
    number: str
    x_mm: float      # relative to the footprint origin, y down like KiCad
    y_mm: float
    through_hole: bool


@dataclass(frozen=True)
class RealPart:
    lcsc: str
    title: str
    package: str
    pads: tuple[RealPad, ...] = ()
    source: str = "easyeda"
    # Package-only parts (no pad geometry, e.g. from a distributor): pin count and mount type.
    pin_count: int = 0
    through_hole: bool | None = None
    rows: int = 0    # connector rows when the source states them (pins are then rows x columns)


def _text(value: object) -> str:
    return re.sub(r"[^\x20-\x7e]", "", value)[:120] if isinstance(value, str) else ""


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and abs(result) < _LIMIT else None


def _pad(fields: list[str], origin: tuple[float, float]) -> RealPad | None:
    if len(fields) < 10:
        return None
    x, y, hole = _number(fields[2]), _number(fields[3]), _number(fields[9])
    number = _text(fields[8]).strip()
    if x is None or y is None or hole is None or not number:  # unnumbered = mechanical
        return None
    slot = _number(fields[13]) if len(fields) > 13 and fields[13] else 0.0
    return RealPad(number, round((x - origin[0]) * _MM_PER_UNIT, 4), round((y - origin[1]) * _MM_PER_UNIT, 4),
                   fields[6] == "11" or hole > 0 or bool(slot))


def parse_component(data: dict) -> RealPart | None:
    """Pads of an EasyEDA component response, or None when it has no usable pad geometry."""
    try:
        result = data["result"]
        detail = result["packageDetail"]
        head = detail["dataStr"]["head"]
        shapes = detail["dataStr"]["shape"]
        origin = (_number(head["x"]), _number(head["y"]))
        if None in origin or not isinstance(shapes, list):
            return None
    except (KeyError, TypeError):
        return None
    pads = []
    for shape in shapes:
        if isinstance(shape, str) and shape.startswith("PAD~"):
            pad = _pad(shape.split("~"), origin)  # type: ignore[arg-type]
            if pad is not None:
                pads.append(pad)
                if len(pads) > MAX_PADS:
                    return None
    if not pads:
        return None
    lcsc = result.get("lcsc")
    return RealPart(_text(lcsc.get("number")) if isinstance(lcsc, dict) else "",
                    _text(result.get("title")), _text(detail.get("title")), tuple(pads))
