"""Heavy checks on the board's own pads and copper (BoardCopper), so they see what the PCB says,
not what the schematic meant.

  pcb.exposed_pad_open  an IC's centre exposed pad, or the thermal vias inside it, has no net on the PCB
  pcb.thermal_vias_missing  a charger, motor driver or class-D amp's exposed pad has no via to the other layers
  pcb.mains_spacing     mains copper closer than 2.5 mm to neutral, earth or low-voltage copper
"""
import math
import re

from .findings import Finding, Severity
from .models import DesignSnapshot

# --- pcb.exposed_pad_open ----------------------------------------------------------------------

from .footprint_fit import EP_MAY_FLOAT as _EP_MAY_FLOAT


def _open(net: str) -> bool:
    return not net or net.startswith("unconnected-")


def _exposed_pads(copper):
    """(ref, centre pad, pads inside it, all its pads) for every footprint whose largest pad is an SMD pad in the
    middle of the others with at least 4 times their area: an exposed (thermal) pad."""
    by_ref: dict[str, list] = {}
    for pad in copper.pads:
        if pad.ref:
            by_ref.setdefault(pad.ref, []).append(pad)
    for ref, pads in sorted(by_ref.items()):
        big = max(pads, key=lambda p: p.size[0] * p.size[1])
        if big.kind != "smd":
            continue
        reach = max(big.size) / 2
        inside = [p for p in pads if p is not big
                  and abs(p.position[0] - big.position[0]) <= reach and abs(p.position[1] - big.position[1]) <= reach]
        outer = [p for p in pads if p is not big and p not in inside]
        if len(outer) < 4 or big.size[0] * big.size[1] < 4 * max(p.size[0] * p.size[1] for p in outer):
            continue
        xs, ys = [p.position[0] for p in outer], [p.position[1] for p in outer]
        if min(xs) < big.position[0] < max(xs) and min(ys) < big.position[1] < max(ys):
            yield ref, big, inside, pads


def check_exposed_pad_open(snapshot: DesignSnapshot) -> list[Finding]:
    """The largest SMD pad sits in the middle of the other pads (an exposed pad) and has no net,
    or the small through-hole pads inside it (thermal vias) have none while the pad does."""
    copper = snapshot.copper
    if copper is None or not copper.pads:
        return []
    values = {c.reference: c.value for c in snapshot.components}
    findings = []
    for ref, big, inside, pads in _exposed_pads(copper):
        if _EP_MAY_FLOAT.search(values.get(ref, "")):
            continue
        name = f"pad {big.number}" if big.number else "the unnumbered centre pad"
        if _open(big.net) and not any(p.number == big.number and not _open(p.net) for p in pads if big.number):
            findings.append(Finding(
                "pcb.exposed_pad_open", Severity.WARNING, f"{ref} exposed pad has no connection on the PCB",
                (ref,), (), f"{ref} {name} ({big.size[0]:.2f} x {big.size[1]:.2f} mm, the centre pad) has no net on "
                f"the board{f' ({big.net})' if big.net else ''}, so it is not soldered to ground or the plane.",
                "Give the exposed pad its net (usually ground) in the schematic and footprint, update the PCB, and "
                "add thermal vias to the plane, unless the datasheet says to leave it open."))
            continue
        vias = [p for p in inside if p.kind == "thru_hole" and _open(p.net) and max(p.size) <= 1.0]
        if len(vias) >= 2 and not _open(big.net):
            findings.append(Finding(
                "pcb.exposed_pad_open", Severity.WARNING, f"{ref} thermal vias have no net",
                (ref,), (big.net,), f"{len(vias)} through-hole pads inside {ref} {name} ({big.net}) have no net, so "
                "the plane is cleared around them and they carry no heat or current to it.",
                f"Give the thermal-via pads the exposed pad's net ({big.net}) in the footprint (same pad number), "
                "then update the PCB and refill the zones."))
    return findings


# --- pcb.thermal_vias_missing ------------------------------------------------------------------

# Parts that turn watts into heat through their exposed pad: linear Li-ion chargers, motor drivers,
# class-D audio amplifiers. Their datasheets' layouts all put a via array in the pad.
_HOT_PART = re.compile(r"(?i)TP405[67]|MCP738\d\d|BQ240[5-7]\d|BQ241\d\d|BQ25\d{3}|LTC40[5-9]\d|IP53\d\d|"
                       r"DRV8\d{3}|TMC[256]\d{3}|A49\d\d|MP6\d{3}|TPA3\d{3}|TPA2\d{3}|PAM8\d{3}|MAX9835\d|MAX9836\d")


def check_thermal_vias_missing(snapshot: DesignSnapshot) -> list[Finding]:
    """A charger, motor driver or class-D amp whose connected exposed pad has no via (or through-hole
    pad) under it, so its heat has no path to the other layers."""
    copper = snapshot.copper
    if copper is None or not copper.pads or len(copper.layers) < 2:
        return []
    values = {c.reference: c.value for c in snapshot.components}
    findings = []
    for ref, big, inside, _ in _exposed_pads(copper):
        value = values.get(ref, "")
        if _open(big.net) or not _HOT_PART.search(value) or any(p.kind == "thru_hole" for p in inside):
            continue
        reach = max(big.size) / 2
        if any(abs(v.position[0] - big.position[0]) <= reach and abs(v.position[1] - big.position[1]) <= reach
               for v in copper.vias):
            continue
        findings.append(Finding(
            "pcb.thermal_vias_missing", Severity.WARNING, f"{ref} ({value}) exposed pad has no thermal vias",
            (ref,), (big.net,), f"{ref} {value} exposed pad {big.number} ({big.size[0]:.2f} x {big.size[1]:.2f} mm, "
            f"{big.net}) has no via under it on this {len(copper.layers)}-layer board, so the part's heat leaves only "
            "through its pins and the top copper.",
            "Put a grid of vias (about 0.3 mm drill, 1 to 1.2 mm pitch) in the exposed pad to the ground plane, as the "
            "datasheet's layout example does."))
    return findings


# --- pcb.mains_spacing -------------------------------------------------------------------------

MAINS_GAP_MM = 2.5   # IEC 60664-1 creepage, 250 V, pollution degree 2, material group III
_LINE = re.compile(r"(?:AC_?)?(?:L|LINE|LIVE)\d?(?:_?(?:IN|PSU|FUSED|F))?")
_NEUTRAL = re.compile(r"(?:AC_?)?(?:N|NEUTRAL)\d?(?:_?(?:IN|PSU|FUSED|F))?")
_MAINS_NAME = re.compile(_LINE.pattern + "|" + _NEUTRAL.pattern + r"|AC\d?|MAINS\w*|VAC|\d{3}V_?AC|AC_?\d{3}V?")
_EARTH = re.compile(r"EARTH|PE|CHASSIS|FG|SHIELD")
# Parts that isolate one side from the other: relays (by reference), transformers, AC-DC modules,
# optocouplers, digital isolators and solid-state relays.
_BARRIER_REF = re.compile(r"(?:K|RL|RLY|T|TR|PS)\d")
_BARRIER = re.compile(r"(?i)PC8\d\d|EL8\d\d|LTV|TLP|MOC\d|H11|4N\d\d|6N\d\d|HLK|IRM-|PM0\d|AC-?DC|ISO\d|ADUM|SI8\d|"
                      r"HCPL|ACPL|CPC\d|AQ[YHW]|G3MB|SSR|OPTO")
_SWITCH_REF = re.compile(r"(?:F|FU|K|RL|RLY)\d")


def _leaf(net: str) -> str:
    return net.rsplit("/", 1)[-1].upper()


def _closest(p, q, a, b):
    """Closest points of segments pq and ab: (distance, point on pq, point on ab)."""
    def side(o, s, t):
        return (t[1] - o[1]) * (s[0] - o[0]) - (s[1] - o[1]) * (t[0] - o[0])
    if side(a, b, p) * side(a, b, q) < 0 and side(p, q, a) * side(p, q, b) < 0:
        return 0.0, p, a

    def onto(x, s, t):
        dx, dy = t[0] - s[0], t[1] - s[1]
        span = dx * dx + dy * dy
        k = 0.0 if span == 0 else max(0.0, min(1.0, ((x[0] - s[0]) * dx + (x[1] - s[1]) * dy) / span))
        return s[0] + k * dx, s[1] + k * dy
    options = [(p, onto(p, a, b)), (q, onto(q, a, b)), (onto(a, p, q), a), (onto(b, p, q), b)]
    u, v = min(options, key=lambda pair: math.dist(*pair))
    return math.dist(u, v), u, v


def _crosses(p, q, a, b) -> bool:
    def side(o, s, t):
        return (t[1] - o[1]) * (s[0] - o[0]) - (s[1] - o[1]) * (t[0] - o[0])
    return side(a, b, p) * side(a, b, q) < 0 and side(p, q, a) * side(p, q, b) < 0


def _domains(snapshot, copper):
    """(mains, low_voltage, earth, line, neutral) net sets, or None when the board has no mains or its
    mains side reaches the low-voltage ground (a non-isolated supply: spacing to it is then functional)."""
    from .audit_rules import is_ground
    parts: dict[str, set] = {}
    for pad in copper.pads:
        if pad.ref and pad.net and not pad.net.startswith("unconnected-"):
            parts.setdefault(pad.ref, set()).add(pad.net)
    names = {c.reference: f"{c.value} {c.footprint}" for c in snapshot.components}
    barrier = {ref for ref in parts if _BARRIER_REF.match(ref.upper()) or _BARRIER.search(names.get(ref, ""))}
    links: dict[str, set] = {}
    for ref, nets in parts.items():
        if ref not in barrier:
            for net in nets:
                links.setdefault(net, set()).update(nets - {net})
    nets = set(links) | {n for ns in parts.values() for n in ns}
    earth = {n for n in nets if _EARTH.fullmatch(_leaf(n))}

    def spread(seed):
        seen, todo = set(seed), list(seed)
        while todo:
            for other in links.get(todo.pop(), ()):
                if other not in seen and other not in earth:
                    seen.add(other)
                    todo.append(other)
        return seen
    low = spread({n for n in nets if is_ground(n) and n not in earth})
    seeds = {n for n in nets if _MAINS_NAME.fullmatch(_leaf(n)) and n not in low}
    if not seeds:
        return None
    mains = spread(seeds)
    for ref in barrier:     # a relay or SSR whose contacts carry mains: its other non-low-voltage pins do too
        if parts[ref] & mains:
            mains |= {n for n in parts[ref] if n not in low and n not in earth}
    mains = spread(mains)
    if mains & low:
        return None
    line = {n for n in seeds if _LINE.fullmatch(_leaf(n))}
    neutral = {n for n in seeds if _NEUTRAL.fullmatch(_leaf(n))}
    for _ in range(3):      # through fuses and relay contacts, a line stays a line
        for ref, ns in parts.items():
            if _SWITCH_REF.match(ref.upper()) and ns & line:
                line |= {n for n in ns if n in mains and n not in neutral}
    return mains, low, earth, line, neutral


def check_mains_spacing(snapshot: DesignSnapshot) -> list[Finding]:
    """Edge-to-edge copper gap on one layer below MAINS_GAP_MM between mains copper and neutral, earth or
    low-voltage copper, unless a board cutout lies between. Pads are taken as circles of their smaller
    side (never closer than they are), and pads of one part are not compared with each other."""
    copper = snapshot.copper
    if copper is None or not copper.pads:
        return []
    found = _domains(snapshot, copper)
    if found is None:
        return []
    mains, low, earth, line, neutral = found
    items = []      # (net, p, q, half width, ref, layers)
    for pad in copper.pads:
        if pad.net:
            items.append((pad.net, pad.position, pad.position, min(pad.size) / 2, pad.ref, pad.layers))
    for seg in copper.segments:
        items.append((seg.net, seg.start, seg.end, seg.width_mm / 2, "", (seg.layer,)))
    for zone in copper.zones:
        pts = zone.points
        items.extend((zone.net, pts[i - 1], pts[i], 0.0, "", (zone.layer,)) for i in range(len(pts)))
    hot = [i for i in items if i[0] in mains]
    cold = [i for i in items if i[0] in low or i[0] in earth or i[0] in neutral]
    if not hot or not cold:
        return []
    reach = MAINS_GAP_MM

    def box(item):
        (x1, y1), (x2, y2), r = item[1], item[2], item[3] + reach
        return min(x1, x2) - r, min(y1, y2) - r, max(x1, x2) + r, max(y1, y2) + r
    worst: dict[tuple[str, str], tuple] = {}
    for a in hot:
        ax0, ay0, ax1, ay1 = box(a)
        for b in cold:
            if b[0] in neutral and a[0] not in line:
                continue
            if a[4] and a[4] == b[4] or not set(a[5]) & set(b[5]):
                continue
            (bx1, by1), (bx2, by2) = b[1], b[2]
            if max(bx1, bx2) + b[3] < ax0 or min(bx1, bx2) - b[3] > ax1 or \
               max(by1, by2) + b[3] < ay0 or min(by1, by2) - b[3] > ay1:
                continue
            distance, u, v = _closest(a[1], a[2], b[1], b[2])
            gap = distance - a[3] - b[3]
            if gap >= MAINS_GAP_MM or any(_crosses(u, v, *e) for e in copper.edges):
                continue
            key = (a[0], b[0])
            if key not in worst or gap < worst[key][0]:
                worst[key] = (max(gap, 0.0), u, a[4], b[4])
    findings = []
    groups = (("line and neutral", neutral, "functional insulation between line and neutral"),
              ("mains and earth", earth, "basic insulation to protective earth"),
              ("mains and low-voltage", low, "the isolation barrier to the low-voltage side"))
    for label, side, why in groups:
        rows = sorted((v[0], k, v) for k, v in worst.items() if k[1] in side)
        if not rows:
            continue
        shown = "; ".join(f"{k[0]} to {k[1]} {gap:.2f} mm at ({v[1][0]:.1f}, {v[1][1]:.1f})"
                          for gap, k, v in rows[:3])
        refs = tuple(sorted({r for _, _, v in rows for r in v[2:4] if r}))
        nets = tuple(dict.fromkeys(n for _, k, _ in rows for n in k))
        findings.append(Finding(
            "pcb.mains_spacing", Severity.WARNING,
            f"Copper between {label} is closer than {MAINS_GAP_MM:g} mm",
            refs, nets, f"{len(rows)} net pair{'s' if len(rows) != 1 else ''} below {MAINS_GAP_MM:g} mm edge to edge "
            f"on one layer, the creepage IEC 60664-1 gives for 250 V at pollution degree 2 ({why}): {shown}.",
            "Widen the gap (move or narrow the track, pull the pour back with a larger zone clearance) or mill a "
            "slot between the two coppers; a user-touchable low-voltage side needs about twice this (reinforced)."))
    return findings
