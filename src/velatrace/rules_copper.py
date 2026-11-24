"""Heavy checks on the board's own pads and copper (BoardCopper), so they see what the PCB says,
not what the schematic meant.

  pcb.exposed_pad_open  an IC's centre exposed pad, or the thermal vias inside it, has no net on the PCB
  pcb.thermal_vias_missing  a charger, motor driver or class-D amp's exposed pad has no via to the other layers
  pcb.mains_spacing     mains copper closer than 2.5 mm to neutral, earth or low-voltage copper
  pcb.schematic_mismatch  PCB pads, pins or values differ from the saved schematic
  pcb.track_width       a motor driver's current-sense resistor is joined to its sense pin by thin track
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


# --- pcb.schematic_mismatch --------------------------------------------------------------------

def schematic_sync(schematic: DesignSnapshot):
    """A heavy check comparing the board's own pads and values with ``schematic`` (the saved schematic's
    netlist): pads joined differently from the schematic, and resistor/capacitor/inductor values that differ."""
    def check_schematic_mismatch(snapshot: DesignSnapshot) -> list[Finding]:
        # The sense-track check rides here because it needs pin names, which only the schematic has.
        return _schematic_mismatch(schematic, snapshot.copper) + _sense_tracks(schematic, snapshot.copper)
    return check_schematic_mismatch


# --- pcb.track_width ---------------------------------------------------------------------------

# Motor-driver current-sense pins: DRV88xx ISENA/AISEN, A49xx SENSE1, TMC2xxx BRA, L298 SENSA.
_SENSE_PIN = re.compile(r"(?i)[AB]?ISENS?E?[AB12]?|SENSE?_?[AB12]?|BR[AB12]|SR[AB12]")
_OHM_MM = 1.72e-5 / 0.035   # 1 oz (35 um) copper: ohms per mm of length per mm of width


def _sense_tracks(schematic: DesignSnapshot, copper) -> list[Finding]:
    """A motor driver's sense resistor (0.5 ohm or less, to ground) joined to its sense pin only by thin track:
    the whole bridge current flows there, and the track's own resistance adds to the sense value."""
    from .audit_rules import is_ground, part_kind
    from .bom import parse_value
    if copper is None or not copper.segments:
        return []
    pins_on: dict[str, list] = {}
    for comp in schematic.components:
        for pin in comp.pins:
            pins_on.setdefault(pin.net, []).append((comp, pin))
    zoned = {zone.net for zone in copper.zones}
    findings = []
    for comp in schematic.components:
        if part_kind(comp) != "resistor" or len(comp.pins) != 2:
            continue
        value = parse_value(comp.value, "resistor")
        nets = [pin.net for pin in comp.pins]
        if not value or not 0 < value.value <= 0.5 or sum(map(is_ground, nets)) != 1:
            continue
        net = next(n for n in nets if not is_ground(n))
        driver = [(c, p) for c, p in pins_on.get(net, ()) if c is not comp and p.name and _SENSE_PIN.fullmatch(p.name)]
        tracks = [s for s in copper.segments if s.net == net]
        if not driver or not tracks or net in zoned:
            continue
        width, length = max(s.width_mm for s in tracks), sum(s.length_mm for s in tracks)
        ohms = sum(_OHM_MM * s.length_mm / s.width_mm for s in tracks)
        sense = float(value.value)
        if ohms < 0.10 * sense:   # under ~10% the current limit only shifts a little
            continue
        ic, pin = driver[0]
        findings.append(Finding(
            "pcb.track_width", Severity.WARNING, f"{ic.reference} {pin.name} sense track to {comp.reference} is too thin",
            (ic.reference, comp.reference), (net,),
            f"{net} ({ic.reference} {pin.name} to {comp.reference} {comp.value}) is only track up to {width:.2f} mm wide, "
            f"{length:.1f} mm long: about {ohms * 1000:.0f} mOhm on 1 oz copper, {ohms / sense:.0%} of the sense "
            "resistor, carrying the full motor current.",
            "Widen the sense track (1 mm or more) or use a short pour, and keep the resistor next to the pin so the "
            "current limit matches the calculation."))
    return findings


def _schematic_mismatch(schematic: DesignSnapshot, copper) -> list[Finding]:
    from collections import Counter
    from .audit_rules import part_kind
    from .bom import parse_value
    if copper is None or not copper.pads or not schematic.components:
        return []
    sch = {(c.reference, p.number): p.net for c in schematic.components for p in c.pins}
    pcb: dict[tuple[str, str], str] = {}
    for pad in copper.pads:
        key = (pad.ref, pad.number)
        if pad.ref and pad.number and (key not in pcb or _open(pcb[key])):
            pcb[key] = pad.net
    # Connections are compared by grouping, not by name: KiCad renames auto-named nets between versions.
    # Open pads on either side are left out (pcb.exposed_pad_open covers a centre pad left without a net).
    pairs = [(key, sch[key], pcb[key]) for key in sorted(sch) if key in pcb and not _open(sch[key]) and not _open(pcb[key])]
    by_sch, by_pcb = Counter(), Counter()
    for _, s, q in pairs:
        by_sch[s, q] += 1
        by_pcb[q, s] += 1
    # Most pads win; a net that kept the schematic's name wins first, so a split names the pads that moved away.
    same = lambda a, b: a.rsplit("/", 1)[-1].upper() == b.rsplit("/", 1)[-1].upper()
    main_q = {s: q for (s, q), n in sorted(by_sch.items(), key=lambda kv: (same(*kv[0]), kv[1]))}
    main_s = {q: s for (q, s), n in sorted(by_pcb.items(), key=lambda kv: (same(*kv[0]), kv[1]))}
    odd = [(key, s, q) for key, s, q in pairs if main_q[s] != q or main_s[q] != s]
    # A resistor, inductor or non-polar capacitor turned end for end is the same circuit.
    parts = {c.reference: c for c in schematic.components}
    def symmetric(ref: str) -> bool:
        comp = parts.get(ref)
        if comp is None or len(comp.pins) != 2:
            return False
        kind = part_kind(comp)
        return kind in ("resistor", "inductor", "ferrite") or (
            kind == "capacitor" and not re.search(r"(?i)CP_|ELEC|TANT|POLAR", comp.footprint))
    swapped = Counter(key[0] for key, s, q in odd)
    odd = [row for row in odd if not (swapped[row[0][0]] == 2 and symmetric(row[0][0]))]
    findings = []
    if odd:
        refs = tuple(sorted({key[0] for key, _, _ in odd}))
        # Pads whose net names differ say most; a pad on a same-named net only shows the other side of a swap.
        leaf = lambda net: net.rsplit("/", 1)[-1].upper()
        rows = sorted(odd, key=lambda row: leaf(row[1]) == leaf(row[2]))
        shown = "; ".join(f"{ref} pad {num} is on {q} (schematic: {s})" for (ref, num), s, q in rows[:4])
        findings.append(Finding(
            "pcb.schematic_mismatch", Severity.WARNING,
            f"The PCB connects {len(odd)} pad{'s' if len(odd) != 1 else ''} differently from the schematic",
            refs[:8], tuple(dict.fromkeys(n for _, s, q in odd for n in (s, q)))[:8],
            f"Compared with the saved schematic, these pads join other pads than the schematic says: {shown}"
            f"{'; ...' if len(odd) > 4 else ''}. The board will be built as the PCB is drawn.",
            "Run Update PCB from Schematic (F8) and check the ratsnest, or fix the schematic if the PCB is right; "
            "KiCad's DRC with 'Test for parity between PCB and schematic' lists every difference."))
    board_values = dict(copper.values)
    changed = []
    for comp in schematic.components:
        kind = part_kind(comp)
        if kind not in ("resistor", "capacitor", "inductor") or comp.reference not in board_values:
            continue
        here, there = parse_value(comp.value, kind), parse_value(board_values[comp.reference], kind)
        if here and there and abs(here.value - there.value) > here.value / 200:
            changed.append((comp.reference, comp.value, board_values[comp.reference]))
    if changed:
        shown = "; ".join(f"{ref} {a} in the schematic, {b} on the PCB" for ref, a, b in changed[:5])
        findings.append(Finding(
            "pcb.schematic_mismatch", Severity.WARNING,
            f"{len(changed)} part value{'s differ' if len(changed) != 1 else ' differs'} between schematic and PCB",
            tuple(ref for ref, _, _ in changed)[:8], (),
            f"{shown}{'; ...' if len(changed) > 5 else ''}. The assembly files come from the PCB, so the PCB value "
            "is the one that gets fitted.",
            "Decide which value is right, then Update PCB from Schematic (F8) so both agree before ordering."))
    # A connected schematic pin whose footprint has no pad of that number is simply not on the board
    # (e.g. a TP4056's thermal pad on a plain SOIC-8 footprint).
    # A duplicate pin (a connector's extra GND, a MOSFET's second drain) is no loss while another pad of the same
    # part keeps that net; an exposed/thermal pad is, since its job is the solder joint itself.
    # A board value that differs from the schematic's means the reference names a different part on the PCB
    # (generated or re-annotated boards), so its pads cannot be compared pin by pin.
    board_value = dict(copper.values)
    renamed = {c.reference for c in schematic.components
               if board_value.get(c.reference, c.value).strip().upper() != c.value.strip().upper()}
    placed = {ref for ref, _ in pcb} - renamed
    members = Counter(net for net in sch.values())
    names = {(c.reference, p.number): p.name for c in schematic.components for p in c.pins}
    kept = {(ref, net) for (ref, num), net in sch.items() if (ref, num) in pcb}
    thermal = re.compile(r"(?i)E?PAD|EP\d*|THERMAL\w*|GND_?PAD|TAB")
    lost = [(ref, num, net) for (ref, num), net in sorted(sch.items())
            if ref in placed and (ref, num) not in pcb and not _open(net) and members[net] > 1
            and ((ref, net) not in kept or thermal.fullmatch(num) or thermal.fullmatch(names.get((ref, num)) or ""))
            and not _EP_MAY_FLOAT.search(parts[ref].value if ref in parts else "")]
    if lost:
        shown = "; ".join(f"{ref} pin {num} ({net})" for ref, num, net in lost[:5])
        findings.append(Finding(
            "pcb.schematic_mismatch", Severity.WARNING,
            f"{len(lost)} schematic pin{'s have' if len(lost) != 1 else ' has'} no pad on the footprint",
            tuple(dict.fromkeys(ref for ref, _, _ in lost))[:8], tuple(dict.fromkeys(n for _, _, n in lost))[:8],
            f"{shown}{'; ...' if len(lost) > 5 else ''}: the footprint has no pad with that number, so the connection "
            "does not exist on the board.",
            "Pick a footprint with that pad (e.g. the exposed-pad package variant) or fix the symbol's pin numbers, "
            "then Update PCB from Schematic (F8)."))
    return findings


# --- pcb.inner_layers_empty --------------------------------------------------------------------

def check_inner_layers_empty(snapshot: DesignSnapshot) -> list[Finding]:
    """A 4+ layer board whose inner copper layers carry no track and no filled zone: it is ordered and paid
    for as multilayer but routes as a 2-layer board. Boards with unfilled zones are skipped (those may sit inside)."""
    copper = snapshot.copper
    if copper is None or len(copper.layers) < 4 or copper.unfilled_nets or not copper.segments:
        return []
    inner = copper.layers[1:-1]
    used = {s.layer for s in copper.segments} | {z.layer for z in copper.zones}
    if used & set(inner):
        return []
    return [Finding(
        "pcb.inner_layers_empty", Severity.SAVING, f"The {len(copper.layers)}-layer board's inner layers are empty",
        (), (), f"{', '.join(inner)} carry no track and no filled zone; all {len(copper.segments)} track segments are on "
        f"{' and '.join(sorted(used))}.",
        "Either pour ground/power planes on the inner layers (better return paths), or set the board to 2 layers and "
        "order it as such: a 2-layer board costs noticeably less.")]
