"""Physical stackup data and explicitly bounded, quasi-static line estimates.

The importer follows KiCad 10's board_stackup.cpp serialization, including the
bare ``addsublayer`` token. It does not infer material properties or plane nets.
Coordinates start at the top face of F.Cu, increasing towards B.Cu; copper
coordinates are centers, not a microstrip substrate height.

Sources:
https://raw.githubusercontent.com/KiCad/kicad-source-mirror/10.0/pcbnew/board_stackup_manager/board_stackup.cpp
https://qucs.sourceforge.net/tech/node75.html

The formula below is independently expressed from the published equations.
It is an idealized estimator, not a field solver or signal-integrity approval.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, DecimalException, localcontext
import hashlib
import json
import math
import re

from .errors import ValidationError
from .sexpr import QuotedAtom, children, one, parse


_C_MM_PER_PS = 0.299792458
_COPPER = re.compile(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu\Z")
_DIELECTRIC = re.compile(r"dielectric ([1-9][0-9]*)\Z")
_SURFACES = {f"{side}.{kind}" for side in ("F", "B")
             for kind in ("SilkS", "Paste", "Mask")}


@dataclass(frozen=True)
class MaterialSublayer:
    thickness_nm: int | None
    epsilon_r: float | None = None
    loss_tangent: float | None = None
    material: str | None = None
    thickness_locked: bool = False


@dataclass(frozen=True)
class StackupLayer:
    name: str
    kind: str  # copper, dielectric, or surface
    type_name: str | None
    sublayers: tuple[MaterialSublayer, ...]

    @property
    def thickness_nm(self) -> int | None:
        if any(part.thickness_nm is None for part in self.sublayers):
            return None
        return sum(part.thickness_nm for part in self.sublayers)


@dataclass(frozen=True)
class PhysicalStackup:
    copper_layers: tuple[str, ...]
    layers: tuple[StackupLayer, ...]

    @property
    def fingerprint(self) -> str:
        """Hash normalized physical data; ignore board items and cosmetic metadata."""
        payload = {"schema": 1, **asdict(self)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode("utf-8")).hexdigest()

    @property
    def has_stackup(self) -> bool:
        return bool(self.layers)

    def _physical_layers(self) -> tuple[StackupLayer, ...]:
        return tuple(layer for layer in self.layers if layer.kind != "surface")

    def copper_z_nm(self, name: str) -> float | None:
        """Copper center from F.Cu's top face; unknown upstream thickness gives None."""
        if name not in self.copper_layers:
            raise ValidationError(f"Unknown copper layer: {name}.")
        position = 0
        for layer in self._physical_layers():
            thickness = layer.thickness_nm
            if thickness is None:
                return None
            if layer.name == name:
                return position + thickness / 2
            position += thickness
        return None

    def separation_nm(self, first: str, second: str) -> float | None:
        """Absolute center separation, using only thicknesses within this span."""
        if first not in self.copper_layers or second not in self.copper_layers:
            raise ValidationError("Separation requires two declared copper layers.")
        if first == second:
            return 0.0
        layers = self._physical_layers()
        names = [layer.name for layer in layers]
        if not layers:
            return None
        start, end = sorted((names.index(first), names.index(second)))
        span = layers[start:end + 1]
        if any(layer.thickness_nm is None for layer in span):
            return None
        return (span[0].thickness_nm + span[-1].thickness_nm) / 2 + sum(
            layer.thickness_nm for layer in span[1:-1])


def _number(value: object, label: str, *, minimum: float = 0,
            positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValidationError(f"{label} must be a finite number.")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValidationError(f"{label} must be a finite number.") from None
    if not math.isfinite(result) or result < minimum or (positive and result == 0):
        raise ValidationError(f"{label} is outside its supported range.")
    return result if result else 0.0


def _thickness(value: str, *, positive: bool) -> int:
    try:
        if len(value) > 64:
            raise ValidationError("Stackup thickness number is too long.")
        with localcontext() as context:
            # Do not round a just-below/above-nm decimal into an accepted integer.
            context.prec = 72
            mm = Decimal(value)
            if (not mm.is_finite() or mm < 0 or mm > 1000
                    or (mm != 0 and mm < Decimal("0.000001"))):
                raise ValidationError("Stackup thickness must be finite, nonnegative, "
                                      "at least one nm when nonzero, and at most 1000 mm.")
            nm = mm * 1_000_000
            if (not nm.is_finite() or nm < 0 or (positive and nm == 0)
                    or nm > 1_000_000_000 or nm != nm.to_integral_value()):
                raise ValidationError("Stackup thickness must be exact nm, positive for "
                                      "copper/dielectric, and at most 1000 mm.")
    except (DecimalException, ValueError):
        raise ValidationError("Stackup thickness must be a number in mm.") from None
    return int(nm)


def _declared_copper(board: list) -> tuple[str, ...]:
    table = one(board, "layers")
    names, ids, copper = set(), set(), {}
    for entry in table[1:]:
        if (not isinstance(entry, list) or len(entry) not in (3, 4)
                or any(not isinstance(atom, str) for atom in entry)):
            raise ValidationError("Malformed canonical board layer list.")
        token, name, kind = entry[:3]
        if not re.fullmatch(r"[0-9]{1,3}", token):
            raise ValidationError("Board layer IDs must be nonnegative integers.")
        layer_id = int(token)
        if name in names or layer_id in ids:
            raise ValidationError("Duplicate canonical board layer name or ID.")
        names.add(name)
        ids.add(layer_id)
        if name.endswith(".Cu") or kind in {"signal", "power", "mixed", "jumper"}:
            if not _COPPER.fullmatch(name) or kind not in {"signal", "power", "mixed", "jumper"}:
                raise ValidationError("Invalid canonical copper layer name or type.")
            copper[name] = layer_id
    count = len(copper)
    expected = ("F.Cu", *(f"In{i}.Cu" for i in range(1, count - 1)), "B.Cu")
    if count < 2 or count > 32 or count % 2 or set(copper) != set(expected):
        raise ValidationError("Copper layers must be F.Cu, consecutive inner layers, then B.Cu "
                              "with an even count from 2 to 32.")
    # Accept coherent old and KiCad 9/10 numbering; never derive physical order from IDs.
    modern = {"F.Cu": 0, "B.Cu": 2, **{f"In{i}.Cu": 2 * i + 2 for i in range(1, count - 1)}}
    legacy = {"F.Cu": 0, "B.Cu": 31, **{f"In{i}.Cu": i for i in range(1, count - 1)}}
    if copper not in (modern, legacy):
        raise ValidationError("Canonical copper names and layer IDs are inconsistent.")
    return expected


def _read_layer(node: list, copper: tuple[str, ...]) -> StackupLayer:
    if len(node) < 2 or not isinstance(node[1], str):
        raise ValidationError("Stackup layer has no name.")
    name = str(node[1])
    if name in copper:
        kind = "copper"
    elif _DIELECTRIC.fullmatch(name):
        kind = "dielectric"
    elif name in _SURFACES:
        kind = "surface"
    else:
        raise ValidationError(f"Unknown or undeclared stackup layer: {name}.")
    groups: list[dict] = [{}]
    type_name = None
    for item in node[2:]:
        if item == "addsublayer" and not isinstance(item, QuotedAtom):
            if kind != "dielectric" or not groups[-1]:
                raise ValidationError("addsublayer is only valid between dielectric sublayers.")
            groups.append({})
            continue
        if not isinstance(item, list) or not item or not isinstance(item[0], str):
            raise ValidationError("Malformed stackup layer property.")
        key = item[0]
        if key not in {"type", "thickness", "epsilon_r", "loss_tangent", "material", "color"}:
            raise ValidationError(f"Unsupported physical stackup property: {key}.")
        if key == "type":
            if type_name is not None or len(groups) != 1 or len(item) != 2 or not isinstance(item[1], str):
                raise ValidationError("Malformed or duplicate stackup layer type.")
            type_name = str(item[1])
            continue
        if key in groups[-1]:
            raise ValidationError(f"Duplicate stackup sublayer property: {key}.")
        if (len(item) not in ((2, 3) if key == "thickness" else (2,))
                or any(not isinstance(atom, str) for atom in item[1:])):
            raise ValidationError(f"Malformed stackup property: {key}.")
        if key == "thickness":
            if len(item) == 3 and item[2] != "locked":
                raise ValidationError("Unknown stackup thickness modifier.")
            groups[-1][key] = (_thickness(item[1], positive=kind != "surface"), len(item) == 3)
        elif key in {"epsilon_r", "loss_tangent"}:
            groups[-1][key] = _number(item[1], key, minimum=1 if key == "epsilon_r" else 0)
        else:
            groups[-1][key] = str(item[1])
    if len(groups) > 1 and not groups[-1]:
        raise ValidationError("Empty dielectric sublayer.")
    if kind == "copper" and type_name not in (None, "copper"):
        raise ValidationError("Copper stackup layer has inconsistent type.")
    if kind == "dielectric" and type_name not in (None, "core", "prepreg"):
        raise ValidationError("Unsupported dielectric stackup type.")
    parts = tuple(MaterialSublayer(
        thickness_nm=group.get("thickness", (None, False))[0],
        epsilon_r=group.get("epsilon_r"), loss_tangent=group.get("loss_tangent"),
        material=group.get("material") or None,
        thickness_locked=group.get("thickness", (None, False))[1]) for group in groups)
    return StackupLayer(name, kind, type_name, parts)


def read_stackup(board_text: str) -> PhysicalStackup:
    """Read physical data without defaults. A missing stackup produces empty layers.

    Missing thickness/permittivity remain None. Unsupported physical properties,
    contradictory copper lists, and malformed values raise ValidationError.
    General board thickness alone cannot supply a physical cross-section.
    """
    board = parse(board_text, kicad=True)
    if board[0] != "kicad_pcb":
        raise ValidationError("Expected a KiCad board document.")
    copper = _declared_copper(board)
    setups = children(board, "setup")
    if len(setups) > 1:
        raise ValidationError("Duplicate board setup.")
    stackups = children(setups[0], "stackup") if setups else []
    if not stackups:
        return PhysicalStackup(copper, ())
    if len(stackups) != 1:
        raise ValidationError("Duplicate physical stackup.")
    metadata = set()
    for item in stackups[0][1:]:
        if not isinstance(item, list) or not item or not isinstance(item[0], str):
            raise ValidationError("Malformed physical stackup entry.")
        if item[0] == "layer":
            continue
        # These fabrication/display settings do not specify line cross-section.
        if (item[0] not in {"copper_finish", "dielectric_constraints", "edge_connector", "edge_plating"}
                or len(item) != 2 or not isinstance(item[1], str) or item[0] in metadata):
            raise ValidationError("Unsupported or malformed physical stackup metadata.")
        if item[0] in {"dielectric_constraints", "edge_plating"} and item[1] not in {"yes", "no"}:
            raise ValidationError("Malformed physical stackup flag.")
        if item[0] == "edge_connector" and item[1] not in {"yes", "no", "bevelled"}:
            raise ValidationError("Malformed edge connector setting.")
        metadata.add(item[0])
    layers = tuple(_read_layer(node, copper) for node in children(stackups[0], "layer"))
    names = [layer.name for layer in layers]
    if len(names) != len(set(names)):
        raise ValidationError("Duplicate stackup layer.")
    physical = tuple(layer for layer in layers if layer.kind != "surface")
    if tuple(layer.name for layer in physical if layer.kind == "copper") != copper:
        raise ValidationError("Stackup copper layers do not match canonical physical order.")
    expected_kinds = tuple("copper" if i % 2 == 0 else "dielectric"
                           for i in range(2 * len(copper) - 1))
    if tuple(layer.kind for layer in physical) != expected_kinds:
        raise ValidationError("Each adjacent copper layer requires exactly one dielectric layer "
                              "(which may have sublayers).")
    first, last = names.index("F.Cu"), names.index("B.Cu")
    for index, layer in enumerate(layers):
        if layer.kind == "surface" and not ((index < first and layer.name.startswith("F."))
                                             or (index > last and layer.name.startswith("B."))):
            raise ValidationError("Surface stackup layer is on the wrong side of the copper stack.")
    return PhysicalStackup(copper, layers)


def homogeneous_delay_ps(length_mm: float, epsilon_r: float) -> float:
    """Lossless, nonmagnetic, nondispersive homogeneous TEM delay; not a via model."""
    length = _number(length_mm, "Length in mm")
    permittivity = _number(epsilon_r, "Relative permittivity", minimum=1)
    result = length * math.sqrt(permittivity) / _C_MM_PER_PS
    if not math.isfinite(result):
        raise ValidationError("Homogeneous delay overflow.")
    return result


@dataclass(frozen=True)
class MicrostripEstimate:
    impedance_ohm: float
    effective_epsilon_r: float
    delay_ps_per_mm: float
    model: str = "Hammerstad-Jensen, zero-thickness, quasi-static"
    assumptions: tuple[str, ...] = (
        "Zero conductor thickness; no solder mask or coating",
        "Uniform isotropic nonmagnetic substrate, air above, infinite reference plane",
        "Isolated straight line; no nearby conductors, bends, vias or plane voids",
        "Lossless quasi-static estimate; frequency dispersion and manufacturing tolerances omitted",
        "0.01 <= width/height <= 100 and 1 <= epsilon_r < 128",
    )


def thin_microstrip(width_mm: float, height_mm: float, epsilon_r: float) -> MicrostripEstimate:
    """Hammerstad-Jensen ideal microstrip, explicitly zero copper thickness.

    Height is substrate thickness from reference-plane surface to the ideal
    zero-thickness trace, NOT center-to-center spacing of finite copper layers.
    Qucs' published effective-permittivity range is used for the combined model:
    0.01 <= w/h <= 100, 1 <= epsilon_r < 128. This function does not select a
    reference plane or apply itself to a board; its assumptions need review.
    """
    width = _number(width_mm, "Microstrip width in mm", positive=True)
    height = _number(height_mm, "Microstrip height in mm", positive=True)
    er = _number(epsilon_r, "Relative permittivity", minimum=1)
    u = width / height
    if not 0.01 <= u <= 100 or er >= 128:
        raise ValidationError("Thin microstrip requires 0.01 <= width/height <= 100 "
                              "and 1 <= epsilon_r < 128.")
    a = 1 + math.log((u**4 + (u / 52)**2) / (u**4 + 0.432)) / 49
    a += math.log(1 + (u / 18.1)**3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3))**0.053
    effective = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u)**(-a * b)
    factor = 6 + (2 * math.pi - 6) * math.exp(-(30.666 / u)**0.7528)
    # Qucs uses free-space impedance ~376.73 ohm; retain this convention for golden values.
    impedance = 376.730313668 / (2 * math.pi) * math.log(
        factor / u + math.sqrt(1 + (2 / u)**2)) / math.sqrt(effective)
    return MicrostripEstimate(impedance, effective, homogeneous_delay_ps(1, effective))
