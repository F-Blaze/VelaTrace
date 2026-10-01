"""Deterministic design checks: no AI, no network, nothing leaves the machine.

Each check reads a DesignSnapshot and returns actionable Findings. Checks are
deliberately conservative: when the design data cannot show a problem clearly,
they stay silent, because a wrong finding costs the designer time.

Heuristics (documented in docs/behavior-and-limits.md):
- Part kinds come from the reference prefix (R, C, L, D, LED, U, IC, J, P, F, Q ...)
  refined by the footprint library ("Connector_*", "LED_*", "Resistor_*" ...).
  A U part on a connector/module footprint is treated as a plugged-in module.
- Ground nets: GND, AGND/DGND/PGND/SGND/CGND, GND*, VSS*, 0V, EARTH, CHASSIS.
- Supply nets: names starting with "+" (KiCad power symbols such as +3V3), plain
  voltages (3V3, 5V, 3.3V, 1V8 ...), and VCC/VDD/AVDD/VBAT/VBUS/VIN/VSYS/VIO...
  When the schematic netlist gives pin types, power_in/power_out pins also mark
  their nets as supplies and decide which IC pins are supply pins.
- I2C lines: a net-name token SDA/SCL, optionally numbered or prefixed (I2C1_SDA).
  SCLK (SPI) is not SCL.
- Only the last hierarchical segment of a net name is used. Auto-generated names
  (Net-(...), unconnected-(...)) never match a heuristic.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from math import dist
import re
from typing import Callable, Iterable

from .findings import Finding, Severity
from .models import Component, DesignSnapshot, Pin

Check = Callable[[DesignSnapshot], list[Finding]]

# Illustrative placed cost of one small SMD passive at small-batch assembly.
# A placeholder for sizing a fix, never a quote. Unknown parts get no amount.
PASSIVE_USD = Decimal("0.01")
DECOUPLING_NEAR_MM = 5.0
SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.SAVING: 2, Severity.INFO: 3}

_GROUND = re.compile(r"(?:[ADPSC]?GND\w*|GND.*|VSS\w*|0V|EARTH|CHASSIS)")
_SUPPLY = re.compile(
    r"\+.+|[+-]?\d+(?:[.,]\d+)?V\d*(?:[_\-].*)?|"
    r"(?:VCC|VDD|AVDD|DVDD|AVCC|DVCC|IOVDD|PVDD|VDDA|VDDIO|VBAT|VBATT|VBUS|VIN|VSYS|VIO|"
    r"VMOT|VPP|VLED|VSUPPLY|VREG|V\+)(?:[_\-.]?[A-Z0-9.]*)")
_SIGNAL_TOKENS = {"EN", "ENA", "PG", "PGOOD", "SENSE", "SNS", "DET", "OK", "FB", "SW", "ON", "OFF",
                  "CTRL", "CTL", "MON", "ADC", "FLT", "FAULT", "ALERT", "INT", "STAT", "DIV", "LVL"}
_ORDER_FIELDS = {"mpn", "manufacturer part number", "manufacturer_part_number", "lcsc", "lcsc part",
                 "lcsc part #", "part number", "digikey", "digi-key_pn", "mouser"}
_I2C = {"SDA": re.compile(r"(?:I2C\d*)?SDA\d*"), "SCL": re.compile(r"(?:I2C\d*)?SCL\d*")}
_GPIO_PIN = re.compile(r"(?:GPIO\d+|IO\d+|P[A-K]\d{1,2}|P\d{1,2}[._]\d{1,2}|D\d{1,2}|RA\d|RB\d|RC\d)",
                       re.IGNORECASE)
_PROTECTION_VALUE = re.compile(r"TVS|ESD|USBLC|PRTR|TPD\d|SMAJ|SMBJ|PESD|SS\d{2}|1N58|B5819|POLYFUSE|PTC",
                               re.IGNORECASE)
_PLACEHOLDER_VALUES = {"", "~", "?", "R", "C", "L", "D", "LED", "R_SMALL", "C_SMALL", "L_SMALL",
                       "C_POLARIZED", "CP", "R_US", "C_US", "VAL", "VALUE", "D_SMALL"}
_PREFIX_KINDS = {"R": "resistor", "RN": "resistor_array", "RA": "resistor_array",
                 "RV": "potentiometer", "C": "capacitor", "CP": "capacitor", "CE": "capacitor",
                 "L": "inductor", "FB": "ferrite", "D": "diode", "ZD": "diode", "TVS": "diode",
                 "LED": "led", "U": "ic", "IC": "ic", "J": "connector", "P": "connector",
                 "CN": "connector", "CON": "connector", "F": "fuse", "Q": "transistor",
                 "TP": "testpoint", "H": "mounting", "MH": "mounting", "Y": "crystal",
                 "SW": "switch"}


def net_label(net: str) -> str:
    """Designer-given part of a net name; "" for KiCad auto-generated names."""
    if not net or net.startswith(("Net-(", "unconnected-(")):
        return ""
    return net.rsplit("/", 1)[-1].strip().upper()


def is_ground(net: str) -> bool:
    return bool(_GROUND.fullmatch(net_label(net)))


def is_supply_name(net: str) -> bool:
    label = net_label(net)
    if not label or is_ground(net) or not _SUPPLY.fullmatch(label):
        return False
    # 3V3_EN, VBUS_DET, 5V_PG ... are control/sense signals named after a rail.
    return not set(re.split(r"[^A-Z0-9]+", label)) & _SIGNAL_TOKENS


def i2c_line(net: str) -> str | None:
    tokens = re.split(r"[^A-Z0-9]+", net_label(net))
    for line, pattern in _I2C.items():
        if any(pattern.fullmatch(token) for token in tokens):
            return line
    return None


def part_kind(comp: Component) -> str:
    prefix = (re.match(r"[A-Za-z]+", comp.reference) or [""])[0].upper()
    library = comp.footprint.split(":", 1)[0].upper()
    text = (comp.footprint + " " + comp.value).upper()
    if library.startswith("CONNECTOR") or "PINHEADER" in text or "PINSOCKET" in text:
        return "connector"
    if library.startswith("MODULE") or library.startswith("RF_MODULE") or "MODULE" in comp.value.upper():
        return "module"
    if prefix == "LED" or library.startswith("LED") or (prefix == "D" and "LED" in text):
        return "led"
    if library.startswith("TESTPOINT"):
        return "testpoint"
    if library.startswith("MOUNTINGHOLE"):
        return "mounting"
    kind = _PREFIX_KINDS.get(prefix)
    if kind is None:
        for lib, guess in (("RESISTOR", "resistor"), ("CAPACITOR", "capacitor"),
                           ("INDUCTOR", "inductor"), ("DIODE", "diode"), ("FUSE", "fuse")):
            if library.startswith(lib):
                return guess
        return "other"
    return kind


def terminals(comp: Component) -> dict[str, str]:
    """Distinct pad number -> net (repeated pads such as a tab collapse to one).

    Unnumbered pads (mechanical/NPTH) are not electrical terminals."""
    result: dict[str, str] = {}
    for pin in comp.pins:
        if pin.number:
            result.setdefault(pin.number, pin.net)
    return result


def two_terminal_nets(comp: Component) -> tuple[str, str] | None:
    nets = list(terminals(comp).values())
    if len(nets) != 2 or not all(nets):
        return None
    return nets[0], nets[1]


def parse_resistance(value: str) -> Decimal | None:
    """Ohms from 10k, 4k7, 4.7kΩ, 100R, 4R7, 0R, 1M, 10 kOhm, "10k 1%"; None if unclear.

    SI prefixes are case-sensitive: m is milli (current sense), M is mega.
    """
    # The trailing part ("1%", "/0.1W") may not hide a unit: "10 k" must never read as 10 Ω.
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:([kKmMrRG])(\d*))?\s*(?i:Ω|ohms?)?"
                         r"(?:[\s_/,](?!\s*[kKmMrRGΩ]).*)?", value or "")
    if not match:
        return None
    number, unit, tail = match.groups()
    if tail and "." in number:
        return None
    amount = Decimal(number + ("." + tail if tail else ""))
    scale = {"k": 3, "K": 3, "M": 6, "G": 9, "m": -3, "r": 0, "R": 0, None: 0}[unit]
    return amount.scaleb(scale)


def _ohms(value: Decimal) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:g} MΩ"
    if value >= 1000:
        return f"{value / 1000:g} kΩ"
    return f"{value:g} Ω"


class _Design:
    """Indexes shared by the checks."""

    def __init__(self, snapshot: DesignSnapshot):
        self.snapshot = snapshot
        self.parts = {comp.reference: comp for comp in snapshot.components}
        self.kind = {comp.reference: part_kind(comp) for comp in snapshot.components}
        self.nodes: dict[str, list[tuple[Component, Pin]]] = defaultdict(list)
        for comp in snapshot.components:
            for pin in comp.pins:
                if pin.net:
                    self.nodes[pin.net].append((comp, pin))
        self.grounds = {net for net in self.nodes if is_ground(net)}
        typed = {pin.net for comp in snapshot.components for pin in comp.pins
                 if pin.net and _base_type(pin) in {"power_in", "power_out"}}
        self.supplies = {net for net in self.nodes
                         if net not in self.grounds and (is_supply_name(net) or net in typed)}
        self.driven = {pin.net for comp in snapshot.components for pin in comp.pins
                       if pin.net and _base_type(pin) == "power_out"}

    def of_kind(self, *kinds: str) -> list[Component]:
        return [comp for comp in self.snapshot.components if self.kind[comp.reference] in kinds]

    def refs_on(self, net: str) -> set[str]:
        return {comp.reference for comp, _ in self.nodes.get(net, ())}

    def kinds_on(self, net: str) -> set[str]:
        return {self.kind[ref] for ref in self.refs_on(net)}


def _base_type(pin: Pin) -> str:
    return pin.electrical_type.split("+", 1)[0].strip().lower()


def _pin_text(pin: Pin) -> str:
    return f"pin {pin.number}" + (f" ({pin.name})" if pin.name and pin.name != pin.number else "")


def _ground_name(design: _Design) -> str:
    return "GND" if "GND" in design.grounds else sorted(design.grounds)[0]


def check_decoupling(snapshot: DesignSnapshot) -> list[Finding]:
    """IC supply pins with no capacitor from that net to ground; far caps (placed boards)."""
    design = _Design(snapshot)
    if not design.grounds:
        return []
    caps: dict[str, list[Component]] = defaultdict(list)
    for cap in design.of_kind("capacitor"):
        nets = two_terminal_nets(cap)
        if nets:
            a, b = nets
            if a in design.grounds and b not in design.grounds:
                caps[b].append(cap)
            elif b in design.grounds and a not in design.grounds:
                caps[a].append(cap)
    missing: dict[str, list[tuple[Component, list[Pin]]]] = defaultdict(list)
    findings = []
    for ic in design.of_kind("ic"):
        typed = any(pin.electrical_type for pin in ic.pins)
        supply_pins: dict[str, list[Pin]] = defaultdict(list)
        for pin in ic.pins:
            if not pin.net or pin.net in design.grounds:
                continue
            if (_base_type(pin) in {"power_in", "power_out"}) if typed else is_supply_name(pin.net):
                supply_pins[pin.net].append(pin)
        for net, pins in sorted(supply_pins.items()):
            if not caps.get(net):
                missing[net].append((ic, pins))
                continue
            points = [pin.position_mm for pin in pins if pin.position_mm]
            if not points:
                continue
            distances = []
            for cap in caps[net]:
                cap_points = [pin.position_mm for pin in cap.pins
                              if pin.net == net and pin.position_mm] or (
                              [cap.position_mm] if cap.position_mm else [])
                distances += [(dist(a, b), cap.reference) for a in points for b in cap_points]
            if distances:
                nearest, ref = min(distances)
                if nearest > DECOUPLING_NEAR_MM:
                    findings.append(Finding(
                        "decoupling.far", Severity.WARNING,
                        f"{ic.reference}'s nearest {net} capacitor ({ref}) is {nearest:.1f} mm away",
                        (ic.reference, ref), (net,),
                        f"{ic.reference} {', '.join(_pin_text(pin) for pin in pins)} on {net}; "
                        f"closest capacitor to ground is {ref} at {nearest:.1f} mm (pad to pad, "
                        "current placement).",
                        f"Move {ref} (or add a 100 nF) within ~{DECOUPLING_NEAR_MM:g} mm of "
                        f"{ic.reference} {_pin_text(pins[0])}, with a short path to ground."))
    ground = _ground_name(design)
    for net, rows in sorted(missing.items()):
        refs = tuple(ic.reference for ic, _ in rows)
        who = refs[0] + " has" if len(refs) == 1 else ", ".join(refs) + " have"
        evidence = "; ".join(f"{ic.reference} {', '.join(_pin_text(pin) for pin in pins)}"
                             for ic, pins in rows)
        findings.append(Finding(
            "decoupling.missing", Severity.ERROR,
            f"{who} no decoupling capacitor on {net}", refs, (net,),
            f"{evidence} on {net}; no capacitor connects {net} to {ground}.",
            f"Add a capacitor from {net} to {ground} next to each supply pin: 100 nF for logic "
            "ICs; regulators need the datasheet value (often 1–10 µF).",
            PASSIVE_USD * len(refs)))
    return findings


def check_i2c_pullups(snapshot: DesignSnapshot) -> list[Finding]:
    """SDA/SCL lines: missing, redundant, mixed-rail, 0 Ω or out-of-range pull-ups."""
    design = _Design(snapshot)
    findings = []
    for net in sorted(design.nodes):
        line = i2c_line(net)
        if line is None or "resistor_array" in design.kinds_on(net):
            continue
        if "ic" not in design.kinds_on(net) or len(design.nodes[net]) < 2:
            continue  # connector pass-through or dangling: nothing to judge
        pullups: list[tuple[Component, str, Decimal | None]] = []
        for comp, _ in design.nodes[net]:
            nets = two_terminal_nets(comp) if design.kind[comp.reference] == "resistor" else None
            if not nets or net not in nets:
                continue
            other = nets[1] if nets[0] == net else nets[0]
            if other in design.supplies:
                pullups.append((comp, other, parse_resistance(comp.value)))
        zero = [row for row in pullups if row[2] == 0]
        for comp, rail, _ in zero:
            findings.append(Finding(
                "i2c.pullup.zero_ohm", Severity.ERROR,
                f"{comp.reference} (0 Ω) ties {net} straight to {rail}", (comp.reference,), (net, rail),
                f"{comp.reference} value '{comp.value}' connects {line} line {net} to {rail}; "
                "the bus can never be pulled low.",
                f"Change {comp.reference} to a pull-up value, e.g. 4.7 kΩ (100 kHz) or 2.2 kΩ (400 kHz)."))
        real = [row for row in pullups if row[2] != 0]
        devices = sorted(ref for ref in design.refs_on(net) if design.kind[ref] == "ic")
        if not pullups:
            findings.append(Finding(
                "i2c.pullup.missing", Severity.WARNING, f"{net} has no pull-up resistor",
                tuple(devices), (net,),
                f"{line} line {net} connects {', '.join(devices)}; no resistor ties it to a supply.",
                f"Add one pull-up from {net} to the bus supply, e.g. 4.7 kΩ (100 kHz) or "
                "2.2 kΩ (400 kHz), unless an off-board module already provides it.", PASSIVE_USD))
            continue
        for comp, rail, ohms in real:
            if ohms is not None and (ohms < 1000 or ohms > 100_000):
                findings.append(Finding(
                    "i2c.pullup.value", Severity.WARNING,
                    f"{comp.reference} pull-up on {net} is {_ohms(ohms)}", (comp.reference,), (net,),
                    f"{comp.reference} '{comp.value}' from {net} to {rail}; typical I2C pull-ups are "
                    "1–10 kΩ (below ~1 kΩ exceeds the 3 mA sink limit, above 100 kΩ is too slow).",
                    f"Use 2.2–4.7 kΩ for {comp.reference} unless the datasheet says otherwise."))
        if len(real) < 2:
            continue
        rails = sorted({rail for _, rail, _ in real})
        refs = tuple(sorted(comp.reference for comp, _, _ in real))
        described = ", ".join(f"{comp.reference} {comp.value} to {rail}" for comp, rail, _ in real)
        if len(rails) > 1:
            findings.append(Finding(
                "i2c.pullup.mixed_rails", Severity.WARNING,
                f"{net} is pulled up to {' and '.join(rails)}", refs, (net, *rails),
                f"Pull-ups: {described}. Different rails on one line back-feed the lower rail.",
                "Pull each I2C line up to one rail; use a level shifter between voltage domains."))
            continue
        values = [ohms for _, _, ohms in real]
        combined = ""
        if all(values):
            parallel = 1 / sum(1 / value for value in values)
            combined = f" (parallel ≈ {_ohms(parallel.quantize(Decimal('1')))})"
        keep, *extra = refs
        findings.append(Finding(
            "i2c.pullup.redundant", Severity.SAVING,
            f"{net} has {len(refs)} pull-ups ({', '.join(refs)}); one is enough", refs, (net, rails[0]),
            f"{described}{combined}.",
            f"Keep one pull-up (e.g. {keep}) and remove {', '.join(extra)}, unless the stronger "
            "combined pull-up was chosen on purpose for bus speed.",
            -PASSIVE_USD * len(extra)))
    return findings


def _led_chain(design: _Design, led: Component) -> tuple[list[Component], set[str]]:
    """Follow series LEDs/diodes through nets that join exactly two pins."""
    chain, nets, pending = [led], set(), [led]
    while pending:
        part = pending.pop()
        for net in two_terminal_nets(part) or ():
            nets.add(net)
            nodes = design.nodes.get(net, [])
            if len(nodes) != 2:
                continue
            for other, _ in nodes:
                if (other not in chain and design.kind[other.reference] in {"led", "diode"}
                        and two_terminal_nets(other)):
                    chain.append(other)
                    pending.append(other)
    return chain, nets


def check_led_resistors(snapshot: DesignSnapshot) -> list[Finding]:
    """LED paths with no series resistor: across a rail (error) or on a GPIO (warning)."""
    design = _Design(snapshot)
    # Any of these on the path may limit the current; stay silent rather than guess.
    limiting = {"resistor", "resistor_array", "potentiometer", "inductor", "transistor"}
    findings, seen = [], set()
    for led in design.of_kind("led"):
        if not two_terminal_nets(led) or led.reference in seen:
            continue
        chain, nets = _led_chain(design, led)
        refs = tuple(sorted(part.reference for part in chain))
        seen.update(refs)
        if any(design.kind[ref] in limiting for net in nets for ref in design.refs_on(net)):
            continue
        internal = {net for net in nets if design.refs_on(net) <= set(refs)
                    and len(design.nodes.get(net, ())) == 2}
        ends = sorted(nets - internal)
        rails = [net for net in ends if net in design.supplies or net in design.grounds]
        names = " → ".join(refs)
        if len(ends) == 2 and len(rails) == 2 and any(net in design.supplies for net in rails):
            findings.append(Finding(
                "led.no_resistor", Severity.ERROR,
                f"{names} sits across {ends[0]}/{ends[1]} with no series resistor", refs, tuple(ends),
                f"{names} connects {ends[0]} to {ends[1]}; no resistor on any of its nets.",
                "Add a series resistor, R = (V − Vf) / I, e.g. 1 kΩ for ~1–2 mA from 3.3 V.",
                PASSIVE_USD))
            continue
        if len(rails) != 1:
            continue
        for net in ends:
            gpio = [(comp, pin) for comp, pin in design.nodes.get(net, ())
                    if design.kind[comp.reference] == "ic" and pin.name and _GPIO_PIN.fullmatch(pin.name)]
            if net in rails or not gpio:
                continue
            comp, pin = gpio[0]
            findings.append(Finding(
                "led.no_resistor", Severity.WARNING,
                f"{names} is driven from {comp.reference} {pin.name} with no series resistor",
                (*refs, comp.reference), (net, rails[0]),
                f"{names} runs from {comp.reference} {_pin_text(pin)} to {rails[0]}; "
                "no resistor on any of its nets.",
                "Add a series resistor (e.g. 1 kΩ) so the pin's current stays within its rating.",
                PASSIVE_USD))
    return findings


def check_single_pin_nets(snapshot: DesignSnapshot) -> list[Finding]:
    """Named nets that reach one pin only: usually a mistyped or missing label."""
    design = _Design(snapshot)
    findings = []
    for net, nodes in sorted(design.nodes.items()):
        if len(nodes) != 1 or not net_label(net):
            continue
        comp, pin = nodes[0]
        quiet = design.kind[comp.reference] in {"connector", "testpoint", "mounting"}
        findings.append(Finding(
            "net.single_pin", Severity.INFO if quiet else Severity.WARNING,
            f"Net {net} reaches only {comp.reference} {_pin_text(pin)}", (comp.reference,), (net,),
            f"{net} has a single connection ({comp.reference} {_pin_text(pin)}).",
            "Check the label spelling (labels join only on identical names) or connect it; "
            "remove the label if the pin is meant to be unused."))
    return findings


def check_floating_inputs(snapshot: DesignSnapshot) -> list[Finding]:
    """IC input pins (schematic pin type) left unconnected without a no-connect flag."""
    design = _Design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        floating = [pin for pin in ic.pins if pin.net and pin.electrical_type.strip().lower() == "input"
                    and len(design.nodes.get(pin.net, ())) == 1]
        if not floating:
            continue
        names = ", ".join(pin.name or pin.number for pin in floating)
        findings.append(Finding(
            "pin.input_floating", Severity.WARNING,
            f"{ic.reference} has {len(floating)} unconnected input pin{'s' if len(floating) > 1 else ''} ({names})",
            (ic.reference,), tuple(pin.net for pin in floating),
            f"{ic.reference} {', '.join(_pin_text(pin) for pin in floating)} are inputs with no connection.",
            "Tie each unused input high or low as the datasheet says, or place a no-connect "
            "flag if it may float."))
    return findings


def check_duplicate_parts(snapshot: DesignSnapshot) -> list[Finding]:
    """Identical ICs with every pin on the same nets (parallel duplicates)."""
    design = _Design(snapshot)
    groups: dict[tuple, list[Component]] = defaultdict(list)
    for ic in design.of_kind("ic"):
        pins = terminals(ic)
        if len(pins) < 3 or not all(pins.values()) or not ic.value.strip():
            continue
        groups[(" ".join(ic.value.split()), ic.footprint.strip().casefold(),
                tuple(sorted(pins.items())))].append(ic)
    findings = []
    for (value, _, pins), parts in groups.items():
        if len(parts) < 2:
            continue
        refs = tuple(sorted(part.reference for part in parts))
        nets = tuple(sorted({net for _, net in pins}))
        bus = [net for net in nets if i2c_line(net)]
        if bus:
            findings.append(Finding(
                "duplicate.parallel_ic", Severity.WARNING,
                f"{', '.join(refs)} ({value}) are wired identically on the I2C bus", refs, nets,
                f"Same value, footprint and all {len(pins)} pins on the same nets, including address "
                "straps, so they answer at the same I2C address.",
                "Remove the duplicate, or strap its address pins differently if both are needed."))
        else:
            findings.append(Finding(
                "duplicate.parallel_ic", Severity.SAVING,
                f"{', '.join(refs[1:])} duplicate{'s' if len(refs) == 2 else ''} {refs[0]} ({value})",
                refs, nets,
                f"Same value, footprint and all {len(pins)} pins on the same nets.",
                f"If {', '.join(refs[1:])} add no function (redundancy, load sharing), remove "
                f"{'it' if len(refs) == 2 else 'them'} to save the part and its placement."))
    return findings


def check_connector_protection(snapshot: DesignSnapshot) -> list[Finding]:
    """Connectors carrying a supply and ground with no protection part on that supply."""
    design = _Design(snapshot)
    protective = {"diode", "fuse", "transistor"}
    findings = []
    for conn in design.of_kind("connector"):
        nets = set(terminals(conn).values())
        if not nets & design.grounds:
            continue
        exposed = []
        for net in sorted(nets & design.supplies):
            if net in design.driven:
                continue  # a regulator output feeds it: the connector supplies power out
            parts = [design.parts[ref] for ref in design.refs_on(net) if ref != conn.reference]
            if not any(design.kind[part.reference] in protective or _PROTECTION_VALUE.search(part.value)
                       for part in parts):
                exposed.append(net)
        if exposed:
            findings.append(Finding(
                "connector.power_unprotected", Severity.INFO,
                f"{conn.reference} carries {', '.join(exposed)} with no reverse-polarity/ESD part",
                (conn.reference,), tuple(exposed),
                f"{conn.reference} has pins on {', '.join(exposed)} and ground; no diode, fuse, "
                "transistor or TVS/ESD part touches that supply.",
                "If this is a power input, add a series Schottky/P-FET or a TVS to ground; "
                "ignore if it only supplies power to another board."))
    return findings


def check_values(snapshot: DesignSnapshot) -> list[Finding]:
    """0 Ω from a supply to ground; passives whose value was never set."""
    design = _Design(snapshot)
    findings = []
    for res in design.of_kind("resistor"):
        nets = two_terminal_nets(res)
        if not nets or parse_resistance(res.value) != 0:
            continue
        supply = [net for net in nets if net in design.supplies]
        ground = [net for net in nets if net in design.grounds]
        if supply and ground:
            findings.append(Finding(
                "value.zero_ohm_short", Severity.ERROR,
                f"{res.reference} (0 Ω) shorts {supply[0]} to {ground[0]}", (res.reference,),
                (supply[0], ground[0]),
                f"{res.reference} value '{res.value}' connects {supply[0]} directly to {ground[0]}.",
                f"Give {res.reference} its intended value, or remove it if it is a leftover jumper."))
    unset = sorted(comp.reference for comp in design.of_kind("resistor", "capacitor", "inductor", "led", "diode")
                   if comp.value.strip().upper() in _PLACEHOLDER_VALUES and not any(
                       name.strip().casefold() in _ORDER_FIELDS and text.strip()
                       for name, text in comp.fields.items()))
    if unset:
        shown = ", ".join(f"{ref} '{design.parts[ref].value}'" for ref in unset[:8])
        findings.append(Finding(
            "value.missing", Severity.WARNING,
            f"{len(unset)} part{'s have' if len(unset) > 1 else ' has'} no value set ({', '.join(unset[:6])}"
            + (", …)" if len(unset) > 6 else ")"),
            tuple(unset), (),
            f"Values still show the symbol default: {shown}" + (" …" if len(unset) > 8 else "") + ".",
            "Set each value (e.g. 100nF, 10k) so the BOM can be ordered without guessing."))
    return findings


def check_coverage(snapshot: DesignSnapshot) -> list[Finding]:
    """Say when net names are too generic for the checks, instead of a silent all-clear."""
    design = _Design(snapshot)
    if design.grounds or not design.nodes:
        return []
    return [Finding(
        "audit.coverage", Severity.INFO, "No ground net found by name; power checks were skipped",
        evidence="No net is named GND/VSS/AGND…; decoupling, LED, 0 Ω and connector checks need one.",
        fix="Name the ground and supply nets (GND, +3V3, …) with power symbols or labels, then re-run.")]


RULES: dict[str, Check] = {
    "decoupling": check_decoupling,
    "i2c": check_i2c_pullups,
    "led": check_led_resistors,
    "net": check_single_pin_nets,
    "pin": check_floating_inputs,
    "duplicate": check_duplicate_parts,
    "connector": check_connector_protection,
    "value": check_values,
    "coverage": check_coverage,
}

# Extra check providers: callables (snapshot) -> list[Finding], run after RULES.
# Registering one is a single line, e.g. for the BOM checks on feat/bom-parts:
#     register_provider(bom.bom_findings)   # bom_findings(snapshot, parts_db=None)
EXTRA_PROVIDERS: list[Check] = []


def register_provider(provider: Check) -> Check:
    if provider not in EXTRA_PROVIDERS:
        EXTRA_PROVIDERS.append(provider)
    return provider


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    unique = list(dict.fromkeys(findings))
    return sorted(unique, key=lambda item: (SEVERITY_ORDER[item.severity], item.rule, item.refs,
                                            item.nets, item.title))


def run_rules(snapshot: DesignSnapshot, providers: Iterable[Check] | None = None) -> list[Finding]:
    """All built-in checks plus extra providers, sorted by severity.

    A failing check never hides the others: it becomes an INFO finding.
    """
    checks = list(RULES.items()) + [(getattr(provider, "__name__", "extra"), provider)
                                    for provider in (EXTRA_PROVIDERS if providers is None else providers)]
    findings: list[Finding] = []
    for name, check in checks:
        try:
            findings.extend(check(snapshot))
        except Exception as exc:  # a check bug must not block the rest of the audit
            findings.append(Finding("audit.check_failed", Severity.INFO,
                                    f"The {name} check could not run", evidence=str(exc) or type(exc).__name__))
    return sort_findings(findings)


def saving_total(findings: Iterable[Finding]) -> Decimal:
    return -sum((item.cost_delta for item in findings
                 if item.cost_delta is not None and item.cost_delta < 0), Decimal("0"))


def summarize(findings: list[Finding]) -> str:
    """One line such as "3 errors · 2 warnings · est. $0.42/board saving"."""
    if not findings:
        return "No issues found by the built-in checks"
    names = {Severity.ERROR: ("error", "errors"), Severity.WARNING: ("warning", "warnings"),
             Severity.SAVING: ("saving", "savings"), Severity.INFO: ("note", "notes")}
    parts = []
    for severity, (one, many) in names.items():
        count = sum(item.severity == severity for item in findings)
        if count:
            parts.append(f"{count} {one if count == 1 else many}")
    saving = saving_total(findings)
    if saving > 0:
        parts.append(f"est. ${saving:.2f}/board saving")
    return " · ".join(parts)

