"""Per-layer line estimates for the native Vela-routing engine.

Turns the board stackup into what a router can use: which plane each signal
layer is referenced to, its propagation delay per mm and its impedance for a
given track width. Missing stackup data is filled with a generic FR-4 default
and every figure derived from it is flagged ``assumed``.

All numbers are quasi-static estimates (stackup.thin_microstrip and the IPC-2141
stripline approximation), not a field solver and not signal-integrity sign-off.
"""
from dataclasses import dataclass
import math
import re

from .errors import ValidationError
from .stackup import homogeneous_delay_ps, read_stackup, thin_microstrip

FR4_EPSILON_R = 4.5        # generic FR-4 near 1 GHz; real laminates run about 3.9 to 4.8
OUTER_PREPREG_MM = 0.2     # typical outer dielectric of a 4+ layer hobby stackup
COPPER_MM = 0.035          # 1 oz


@dataclass(frozen=True)
class LayerModel:
    name: str
    kind: str                # plane, microstrip, stripline, embedded, unreferenced
    height_mm: float         # dielectric to the nearest reference plane (board thickness if none)
    far_height_mm: float | None   # to the plane on the other side (stripline only)
    epsilon_r: float
    assumed: bool            # some value came from the FR-4 default, not from the board

    def delay_ps_per_mm(self, width_mm: float) -> float:
        if self.kind in ("stripline", "embedded", "plane"):
            return homogeneous_delay_ps(1, self.epsilon_r)
        try:
            return thin_microstrip(width_mm, self.height_mm, self.epsilon_r).delay_ps_per_mm
        except ValidationError:   # outside the model's width/height range
            return homogeneous_delay_ps(1, (self.epsilon_r + 1) / 2)

    def impedance_ohm(self, width_mm: float) -> float | None:
        """Single-ended estimate; None when there is no reference plane to speak of."""
        if self.kind in ("plane", "unreferenced"):
            return None
        if self.kind == "stripline":
            h1, h2, t = self.height_mm, self.far_height_mm, COPPER_MM
            return (80 / math.sqrt(self.epsilon_r) * math.log(1.9 * (2 * h1 + t) / (0.8 * width_mm + t))
                    * (1 - h1 / (4 * h2)))
        try:
            line = thin_microstrip(width_mm, self.height_mm, self.epsilon_r)
        except ValidationError:
            return None
        if self.kind == "embedded":   # buried in dielectric: no air above the trace
            return line.impedance_ohm * math.sqrt(line.effective_epsilon_r / self.epsilon_r)
        return line.impedance_ohm

    def width_for(self, target_ohm: float) -> float | None:
        """Track width for a target impedance, by bisection over 0.05 to 5 mm."""
        low, high = 0.05, 5.0
        z_low, z_high = self.impedance_ohm(low), self.impedance_ohm(high)
        if z_low is None or z_high is None or not z_high <= target_ohm <= z_low:
            return None
        for _ in range(40):
            middle = (low + high) / 2
            if self.impedance_ohm(middle) > target_ohm:
                low = middle
            else:
                high = middle
        return round((low + high) / 2, 4)


def _dielectrics(layers, thickness, text):
    """(thickness mm, epsilon_r, assumed) for the dielectric under each copper layer but the last."""
    count = len(layers)
    if count == 2:
        default = [max(0.1, thickness - 2 * COPPER_MM)]
    else:
        inner = max(0.05, (thickness - 2 * OUTER_PREPREG_MM - count * COPPER_MM) / (count - 3))
        default = [OUTER_PREPREG_MM] + [inner] * (count - 3) + [OUTER_PREPREG_MM]
    out = [(value, FR4_EPSILON_R, True) for value in default]
    try:
        stack = read_stackup(text)
    except ValidationError:
        return out
    found = [layer for layer in stack.layers if layer.kind == "dielectric"]
    if len(found) != count - 1:
        return out
    for index, layer in enumerate(found):
        parts = [part for part in layer.sublayers if part.thickness_nm]
        if not parts:
            continue
        total = sum(part.thickness_nm for part in parts)
        known = all(part.epsilon_r for part in parts)
        epsilon = (sum(part.epsilon_r * part.thickness_nm for part in parts) / total) if known else FR4_EPSILON_R
        out[index] = (total / 1e6, epsilon, not known)
    return out


def layer_models(layers, thickness, text, planes) -> list[LayerModel]:
    """One model per copper layer; ``planes`` are the indexes kept as solid reference planes."""
    dielectric = _dielectrics(layers, thickness, text)
    count = len(layers)

    def nearest(index, step):
        height, seen, at = 0.0, [], index
        while 0 <= at + step < count:
            below = dielectric[at if step > 0 else at - 1]
            height += below[0]
            seen.append(below)
            at += step
            if at in planes:
                return height, sum(e for _, e, _ in seen) / len(seen), any(a for _, _, a in seen)
            height += COPPER_MM
        return None

    models = []
    for index, name in enumerate(layers):
        sides = [side for side in (nearest(index, -1), nearest(index, 1)) if side]
        if index in planes:
            below = dielectric[min(index, count - 2)]
            models.append(LayerModel(name, "plane", below[0], None, below[1], below[2]))
        elif len(sides) == 2:
            near, far = sorted(sides)
            models.append(LayerModel(name, "stripline", near[0], far[0], (near[1] + far[1]) / 2, near[2] or far[2]))
        elif sides:
            kind = "microstrip" if index in (0, count - 1) else "embedded"
            models.append(LayerModel(name, kind, *sides[0][:1], None, sides[0][1], sides[0][2]))
        else:
            total = sum(d[0] for d in dielectric) + COPPER_MM * (count - 2)
            mean = sum(d[1] for d in dielectric) / len(dielectric)
            models.append(LayerModel(name, "unreferenced", total, None, mean, any(d[2] for d in dielectric)))
    return models


_PAIR_SUFFIXES = (("+", "-"), ("_P", "_N"), ("_DP", "_DN"), ("DP", "DM"), ("_D+", "_D-"), ("P", "N"), ("P", "M"))


def _pair_key(name):
    """KiCad's automatic name Net-(U3-GPIO20{slash}USB_D+) -> U3-USB_D+: the part and the pin's last alias."""
    found = re.fullmatch(r"Net-\((\w+)-(.*)\)", name)
    return f"{found.group(1)}-{found.group(2).split('{slash}')[-1]}" if found else name


def differential_pairs(names) -> list[tuple[str, str]]:
    """Net-name pairs such as USB_D+/USB_D-; a naming guess."""
    keys = {}
    for name in names:
        keys.setdefault(_pair_key(name), []).append(name)
    keys = {key: found[0] for key, found in keys.items() if len(found) == 1}
    pairs, used = [], set()
    for name in sorted(keys):
        for positive, negative in _PAIR_SUFFIXES:
            other = name[:-len(positive)] + negative
            if name.endswith(positive) and other in keys and len(name) > len(positive) and not {name, other} & used:
                pairs.append((keys[name], keys[other]))
                used.update((name, other))
                break
    return pairs


def demo():
    four = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    board = '(kicad_pcb (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)))'
    top, plane, inner, bottom = layer_models(four, 1.6, board, {1})
    assert (top.kind, plane.kind, inner.kind, bottom.kind) == ("microstrip", "plane", "embedded", "microstrip")
    assert top.assumed and abs(top.height_mm - 0.2) < 1e-9 and bottom.height_mm > 1.0
    # 0.2 mm FR-4 microstrip is close to 50 ohm at about 0.37 mm wide.
    assert 0.3 < top.width_for(50) < 0.45 and abs(top.impedance_ohm(top.width_for(50)) - 50) < 0.1
    assert 5.5 < top.delay_ps_per_mm(0.2) < 6.2 < inner.delay_ps_per_mm(0.2) < 7.2   # buried is slower
    strip = layer_models(four, 1.6, board, {0, 2})[1]
    assert strip.kind == "stripline" and 20 < strip.impedance_ohm(0.2) < 90
    assert layer_models(("F.Cu", "B.Cu"), 1.6, "(kicad_pcb)", set())[0].impedance_ohm(0.2) is None
    assert differential_pairs(["USB_D+", "USB_D-", "CLK_P", "CLK_N", "GND", "P", "N"]) == [
        ("CLK_P", "CLK_N"), ("USB_D+", "USB_D-")]
    assert differential_pairs(["Net-(U3-USB_DP)", "Net-(U3-USB_DM)", "USBP", "USBM", "Net-(U1-IO19{slash}USB_D-)",
                               "Net-(U1-IO20{slash}USB_D+)"]) == [
        ("Net-(U1-IO20{slash}USB_D+)", "Net-(U1-IO19{slash}USB_D-)"), ("Net-(U3-USB_DP)", "Net-(U3-USB_DM)"),
        ("USBP", "USBM")]


if __name__ == "__main__":
    demo()
    print("ok")
