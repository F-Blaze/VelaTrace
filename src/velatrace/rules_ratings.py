"""Electrical-rating and common-circuit audit rules.

  cap.voltage_rating        capacitor rated below the nominal voltage of the rail it sits on
  resistor.power_rating     LED series resistor that dissipates more than its package rating
  usb.vbus_capacitance      more than 10 uF charging from a USB device's VBUS with no inrush limiter
  inductive.flyback_missing relay coil switched by a transistor with no flyback diode
  pin.power_unconnected     IC power_in pin left open without a no-connect flag, or fed only through a capacitor

Like the rest of audit_rules these stay quiet whenever the netlist cannot show the problem clearly.
"""
from __future__ import annotations

from decimal import Decimal
import re

from .audit_rules import (_BUILT_IN_DECOUPLING, _NOT_SUPPLY_TOKEN, _Design, _base_type, _is_return, _pin_text,
                          _series_link, is_ground, is_supply_name, parse_resistance, two_terminal_nets)
from .bom import mpn_of, package_of, parse_value, rail_voltage
from .findings import Finding, Severity
from .models import Component, DesignSnapshot, Pin
from .rules_power import _VCAP


def _name(pin: Pin) -> str:
    """Pin name upper-cased with KiCad overbar markup and spaces removed."""
    return re.sub(r"[~{}\\s]", "", pin.name.upper())


def _volts(volts: Decimal) -> str:
    return f"{volts:g} V"


# --- cap.voltage_rating -----------------------------------------------------------------------

def _rated_volts(comp: Component) -> Decimal | None:
    """The one rated voltage the value and voltage fields state; None when absent or contradictory."""
    found = set()
    texts = [comp.value] + [text for key, text in comp.fields.items() if "volt" in key.lower()]
    for text in texts:
        parsed = parse_value(f"1u {text}" if text is not comp.value else text, "capacitor")
        if parsed is not None and parsed.voltage is not None:
            found.add(parsed.voltage)
    return found.pop() if len(found) == 1 else None


# Rated voltage encoded in common MLCC part numbers.
_JIS_VOLTS = {"0G": "4", "0J": "6.3", "1A": "10", "1C": "16", "1E": "25", "1V": "35", "1H": "50", "2A": "100",
              "2D": "200", "2E": "250", "2W": "450", "2J": "630", "3A": "1000"}
_MLCC_VOLTS = (
    (re.compile(r"G[RC][MJT]\d{3}[A-Z0-9]{2}(\d[A-Z])"), _JIS_VOLTS),                                    # Murata
    (re.compile(r"C(?:0603|1005|1608|2012|3216|3225|4532)(?:X7R|X5R|C0G|X7S|X6S|NP0)(\d[A-Z])"), _JIS_VOLTS),  # TDK
    (re.compile(r"CL\d\d[A-Z]\d{3}[A-Z]([A-Z])\d"),                                                     # Samsung
     {"R": "4", "Q": "6.3", "P": "10", "O": "16", "A": "25", "L": "35", "B": "50", "C": "100", "D": "200",
      "E": "250", "G": "500", "H": "630", "I": "1000"}),
    (re.compile(r"CC\d{4}[A-Z]{2}(?:NPO|X7R|X5R|C0G|Y5V|X7S|X6S)(\d)B"),                                  # Yageo
     {"5": "6.3", "6": "10", "7": "16", "8": "25", "9": "50", "0": "100"}),
)


def _mpn_volts(comp: Component) -> tuple[Decimal, str] | None:
    for name, text in comp.fields.items():
        if not isinstance(text, str) or name in {"Footprint", "Datasheet", "lib_id"}:
            continue
        code = re.sub(r"\s+", "", text).upper()
        for pattern, table in _MLCC_VOLTS:
            if (match := pattern.match(code)) and match.group(1) in table:
                return Decimal(table[match.group(1)]), text.strip()
    return None


def check_cap_voltage_rating(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings, understated = [], []
    for comp in design.of_kind("capacitor"):
        ends = two_terminal_nets(comp)
        rated = _rated_volts(comp)
        coded = None if comp.dnp else _mpn_volts(comp)
        if rated is not None and coded and coded[0] < rated:
            understated.append((comp, rated, coded))
        elif rated is None and coded:
            rated = coded[0]
        if comp.dnp or ends is None or rated is None:
            continue
        for rail, other in (ends, ends[::-1]):
            volts = rail_voltage(rail)
            if volts is None or not _is_return(design, other) or _is_return(design, rail) or rated >= volts:
                continue
            findings.append(Finding(
                "cap.voltage_rating", Severity.ERROR,
                f"{comp.reference} is rated {_volts(rated)} on the {_volts(volts)} rail {rail}",
                (comp.reference,), (rail,),
                f"{comp.reference} ({comp.value}) is rated {_volts(rated)} but sits between {rail} "
                f"(nominal {_volts(volts)}) and {other}, so it is over-stressed.",
                f"Use a capacitor rated for more than {_volts(volts)}; a 50 percent margin is common practice."))
    if understated:   # one finding for the board: the same habit of copying a part number without its voltage code
        refs = tuple(comp.reference for comp, _, _ in understated)
        findings.append(Finding(
            "cap.voltage_rating", Severity.WARNING,
            f"{', '.join(refs[:6])}: the part number is rated lower than the value states", refs, (),
            "; ".join(f"{comp.reference} ({comp.value}) {code} is a {_volts(coded)} part, the value says {_volts(rated)}"
                      for comp, rated, (coded, code) in understated[:5]) + ". The bought part is rated lower than the "
            "schematic claims.",
            "Order the part numbers for the stated voltage, or correct the values if the lower rating is enough."))
    return findings


# --- resistor.power_rating --------------------------------------------------------------------

_PACKAGE_WATTS = {"0201": Decimal("0.05"), "0402": Decimal("0.0625"), "0603": Decimal("0.1"),
                  "0805": Decimal("0.125"), "1206": Decimal("0.25"), "1210": Decimal("0.5"),
                  "2010": Decimal("0.75"), "2512": Decimal(1)}
_LED_HIGH = re.compile(r"(?<![A-Z])(?:BLUE|GREEN|WHITE|RGB)(?![A-Z])", re.IGNORECASE)
_LED_LOW = re.compile(r"(?<![A-Z])(?:RED|YELLOW|ORANGE|AMBER)(?![A-Z])", re.IGNORECASE)
_ANODE = re.compile(r"A|ANODE")
_CATHODE = re.compile(r"K|CATHODE")


def _forward_volts(led: Component) -> Decimal | None:
    text = " ".join([led.value, *led.fields.values()])
    high, low = bool(_LED_HIGH.search(text)), bool(_LED_LOW.search(text))
    return None if high and low else Decimal("2.8") if high else Decimal("1.8")


def _led_pin_net(led: Component, pattern: re.Pattern) -> str | None:
    nets = {pin.net for pin in led.pins if pattern.fullmatch(_name(pin)) and pin.net}
    return nets.pop() if len(nets) == 1 else None


def _series_resistor(design: _Design, mid: str) -> tuple[Component, str] | None:
    """The single resistor on a net that joins only it and the LED, with the net at its far end."""
    nodes = design.nodes.get(mid, ())
    resistors = [comp for comp, _ in nodes if design.kind[comp.reference] == "resistor"]
    if len(nodes) != 2 or len(resistors) != 1:
        return None
    ends = two_terminal_nets(resistors[0])
    if ends is None or mid not in ends or ends[0] == ends[1]:
        return None
    return resistors[0], ends[1] if ends[0] == mid else ends[0]


_COMMON_ANODE = re.compile(r"A|ANODE|COM|COMMON")
_CHANNEL = re.compile(r"(?:K|C|CAT|CATHODE)?[_\-]?(R|RED|G|GRN|GREEN|B|BLU|BLUE)[_\-]?(?:K|C|CAT|CATHODE)?\d?")
_DRAIN = re.compile(r"D\d?|DRAIN\d?|C\d?|COLLECTOR\d?")
_SOURCE = re.compile(r"S\d?|SOURCE\d?|E\d?|EMITTER\d?")
_P_TYPE = re.compile(r"PNP|PMOS|P[-_ ]?CHANNEL|AO3401|DMP\d|SI2301|IRLML6401|2N3906|BC807|BC857")


def led_branches(led: Component, two_pin_vf: Decimal | None) -> list[tuple[str, str, Decimal]]:
    """(anode net, cathode net, forward volts) per LED channel: the A/K pair of a 2-pin LED, or each cathode of a
    common-anode multi-colour LED whose pin name says red (1.8 V) or green/blue (2.8 V); anything else is skipped."""
    if len(led.pins) == 2:
        anode, cathode = _led_pin_net(led, _ANODE), _led_pin_net(led, _CATHODE)
        return [(anode, cathode, two_pin_vf)] if anode and cathode and two_pin_vf is not None else []
    commons = {pin.net for pin in led.pins if _COMMON_ANODE.fullmatch(_name(pin))}
    if len(commons) != 1 or "" in commons:
        return []
    branches = []
    for pin in led.pins:
        match = None if _COMMON_ANODE.fullmatch(_name(pin)) or not pin.net else _CHANNEL.fullmatch(_name(pin))
        if match:
            branches.append((*commons, pin.net, Decimal("1.8") if match[1] in {"R", "RED"} else Decimal("2.8")))
    return branches


def _return_path(design: _Design, net: str) -> tuple[str, Component | None] | None:
    """The ground a resistor's far end returns to: the net itself, or the ground on the source/emitter of a
    low-side N-MOSFET/NPN whose drain/collector is that net (and nothing else is)."""
    if net in design.grounds:
        return net, None
    nodes = design.nodes.get(net, ())
    switches = {comp.reference: comp for comp, _ in nodes if design.kind[comp.reference] == "transistor"}
    if len(switches) != 1 or len({comp.reference for comp, _ in nodes}) != 2:
        return None
    switch = next(iter(switches.values()))
    drains = [pin for pin in switch.pins if _DRAIN.fullmatch(_name(pin))]
    sources = [pin for pin in switch.pins if _SOURCE.fullmatch(_name(pin))]
    if (switch.dnp or _P_TYPE.search(f"{switch.value} {switch.footprint}".upper()) or not drains or not sources
            or any(pin.net != net for pin in drains) or any(pin.net == net and pin not in drains for pin in switch.pins)
            or any(pin.net not in design.grounds for pin in sources)):
        return None
    return sources[0].net, switch


def led_cases(design: _Design, led: Component, anode: str, cathode: str):
    """(resistor, rail, ground, low-side switch) for each way a single series resistor limits this LED channel."""
    cases = []
    if len(led.pins) == 2 and cathode in design.grounds and (found := _series_resistor(design, anode)):
        cases.append((found[0], found[1], cathode, None))
    if (found := _series_resistor(design, cathode)) and (back := _return_path(design, found[1])):
        cases.append((found[0], anode, *back))
    return cases


# Motor drivers: (maximum peak current A, sense voltage at that limit V, stepper so RMS = peak / sqrt 2).
# DRV8825: 2.5 A, ITRIP = VREF / (5 R), VREF up to 3.3 V. A4988: 2 A, VREF / (8 R), VREF up to 4 V.
# TMC2208/2209: 2.8 A peak, 0.325 V full scale. DRV8833: 2 A peak, 0.2 V trip, brushed (DC).
_SENSE_DRIVERS = ((re.compile(r"DRV8825"), 2.5, 0.66, True), (re.compile(r"A498[38]"), 2.0, 0.5, True),
                  (re.compile(r"TMC22(?:0[89]|2[456])"), 2.8, 0.325, True), (re.compile(r"DRV883[23]"), 2.0, 0.2, False))


def _sense_resistor_power(design: _Design) -> list[Finding]:
    """A motor driver's current-sense resistor whose package cannot take I^2 R at the driver's full current."""
    from .rules_copper import _SENSE_PIN
    findings = []
    for res in design.of_kind("resistor"):
        ends = two_terminal_nets(res)
        value = parse_value(res.value, "resistor")
        watts = _PACKAGE_WATTS.get(package_of(res.footprint) or "")
        if res.dnp or not ends or not value or not 0 < value.value <= Decimal("0.5") or watts is None:
            continue
        sense = [net for net in ends if net not in design.grounds]
        if len(sense) != 1:
            continue
        for ic, pin in design.nodes.get(sense[0], ()):
            spec = next((d for d in _SENSE_DRIVERS if d[0].search(ic.value.upper())), None) if pin.name else None
            if spec is None or not _SENSE_PIN.fullmatch(pin.name):
                continue
            _, peak_max, volts, stepper = spec
            ohms = float(value.value)
            peak = min(peak_max, volts / ohms)
            amps = peak / 2 ** 0.5 if stepper else peak
            power = amps * amps * ohms
            if power > float(watts) * 1.1:
                findings.append(Finding(
                    "resistor.power_rating", Severity.WARNING,
                    f"{res.reference} ({res.value}, {package_of(res.footprint)}) is too small for {ic.reference}'s motor current",
                    (res.reference, ic.reference), (sense[0],),
                    f"{res.reference} senses {ic.reference} ({ic.value}) {pin.name}: at the driver's full current "
                    f"({amps:.2g} A {'RMS' if stepper else 'DC'}) it dissipates about {power:.2g} W, above the "
                    f"{float(watts):g} W a {package_of(res.footprint)} resistor is rated for.",
                    "Use a larger package (1206/2010/2512, or a current-sense part rated for the power), or limit the "
                    "motor current well below the driver's maximum."))
            break
    return findings


def check_resistor_power_rating(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = _sense_resistor_power(design)
    for led in design.of_kind("led"):
        if led.dnp:
            continue
        for anode, cathode, vf in led_branches(led, _forward_volts(led)):
            for resistor, rail, ground, switch in led_cases(design, led, anode, cathode):
                volts = rail_voltage(rail)
                ohms = parse_resistance(resistor.value)
                parsed = parse_value(resistor.value, "resistor")
                size = package_of(resistor.footprint)
                watts = _PACKAGE_WATTS.get(size or "")
                if (volts is None or ground not in design.grounds or rail in design.grounds or resistor.dnp
                        or not ohms or watts is None or volts <= vf or (parsed is not None and parsed.power)):
                    continue
                amps = (volts - vf) / ohms
                power = amps * amps * ohms
                if power <= watts:
                    continue
                via = f", returning through {switch.reference} to ground" if switch else ""
                findings.append(Finding(
                    "resistor.power_rating", Severity.WARNING,
                    f"{resistor.reference} dissipates about {power:.2f} W, above its {watts:g} W package rating",
                    (resistor.reference, led.reference, *([switch.reference] if switch else [])), (rail,),
                    f"{resistor.reference} ({resistor.value}) limits {led.reference} from {rail}{via}: "
                    f"({volts:g} V - {vf:g} V) / {ohms:g} Ω = {amps * 1000:.0f} mA, so P = I²R = {power:.2f} W, "
                    f"but a {size} resistor is rated {watts:g} W (LED drop assumed {vf:g} V).",
                    "Raise the resistance to cut the current, or use a larger package or a higher-power resistor."))
    return findings


# --- usb.vbus_capacitance ---------------------------------------------------------------------

_USB_TEXT = re.compile(r"USB", re.IGNORECASE)
_USB_A = re.compile(r"USB[_\- ]?A(?![A-Z])|TYPE[_\- ]?A(?![A-Z])", re.IGNORECASE)  # host receptacles drive VBUS
_VBUS_PIN = re.compile(r"VBUS\d*")
_USB_LIMIT_F = Decimal("10e-6")
# Effective capacitance at 5 V above which the 10 uF attach limit is clearly exceeded (margin for tolerance).
_USB_EFFECTIVE_MAX_F = Decimal("13e-6")
# Share of a ceramic's nominal value left at 5 V DC bias, by case size (typical X5R/X7R curves); electrolytic
# and tantalum parts keep their value.
_BIAS = {"0201": Decimal("0.3"), "0402": Decimal("0.3"), "0603": Decimal("0.45"), "0805": Decimal("0.55"),
         "1206": Decimal("0.7"), "1210": Decimal("0.8"), "1812": Decimal("0.85"), "2220": Decimal("0.85")}


def _bias_factor(cap: Component) -> Decimal:
    text = f"{cap.footprint} {cap.fields.get('lib_id', '')} {cap.value}".upper()
    if re.search(r"CP_|POLARI[SZ]ED|ELEC|TANTAL|RADIAL|AXIAL", text):
        return Decimal(1)
    return _BIAS.get(package_of(cap.footprint) or "", Decimal("0.55"))
_HOST_PORT = re.compile(r"(?<![A-Z])(?:HOST|DOWNSTREAM|DFP)(?![A-Z])", re.IGNORECASE)
_NOT_RECTIFIER = re.compile(r"TVS|ESD|ZENER|SMAJ|SMBJ|SMCJ|P6KE|USBLC|PESD|TPD\d|BZ[XT]|TL431", re.IGNORECASE)


def _driven(design: _Design, net: str) -> bool:
    """An IC or module output (power_out pin) drives the net."""
    return any(design.kind[comp.reference] in {"ic", "module"} and _base_type(pin) == "power_out"
               for comp, pin in design.nodes[net])


def _power_only(design: _Design, conn: Component) -> bool:
    """A power-only USB input: its D+/D- pins are open or reach only not-fitted parts. Bulk capacitance
    on a dedicated supply input (a Raspberry Pi carrier, a power-only USB-C sink) is intended."""
    data = [pin for pin in conn.pins if re.fullmatch(r"D[+-]\d?|DP\d?|DN\d?|DM\d?", _name(pin))]
    if not data:
        return False
    return all(not pin.net or pin.net.startswith("unconnected-(") or all(
        comp is conn or comp.dnp for comp, _ in design.nodes[pin.net]) for pin in data)


def _source_port(design: _Design, conn: Component) -> bool:
    """A CC pin pulled up by a resistor to a non-ground net (Rp): the board supplies power on this port."""
    for pin in conn.pins:
        if not pin.net or not re.fullmatch(r"CC\d?", _name(pin)):
            continue
        for comp, _ in design.nodes[pin.net]:
            ends = two_terminal_nets(comp) if design.kind[comp.reference] == "resistor" else None
            if ends and pin.net in ends:
                other = ends[1] if ends[0] == pin.net else ends[0]
                if other not in design.grounds and not _is_return(design, other):
                    return True
    return False


def _ground_caps(design: _Design, net: str, skip: Component) -> dict[str, Decimal]:
    caps = {}
    for comp, _ in design.nodes[net]:
        ends = two_terminal_nets(comp)
        value = parse_value(comp.value, "capacitor") if design.kind[comp.reference] == "capacitor" else None
        if comp is not skip and ends and not comp.dnp and value is not None and any(end in design.grounds for end in ends):
            caps[comp.reference] = value.value
    return caps


def _vbus_links(design: _Design, net: str, conn: Component) -> list[tuple[Component, str]]:
    """One hop from VBUS that does not limit inrush: a diode with its anode on VBUS, or a fuse, ferrite or <=1 ohm
    resistor, leading to a net that no IC output drives."""
    links = {}
    for comp, _ in design.nodes[net]:
        ends = two_terminal_nets(comp)
        if comp is conn or comp.dnp or ends is None or net not in ends or ends[0] == ends[1]:
            continue
        far = ends[1] if ends[0] == net else ends[0]
        if design.kind[comp.reference] == "diode":
            if (_NOT_RECTIFIER.search(f"{comp.value} {comp.footprint}") or _led_pin_net(comp, _ANODE) != net
                    or _led_pin_net(comp, _CATHODE) != far):
                continue
        elif not _series_link(design, comp):
            continue
        if not _is_return(design, far) and not _driven(design, far):
            links[comp.reference] = (comp, far)
    return list(links.values())


def check_vbus_capacitance(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for conn in design.of_kind("connector"):
        text = f"{conn.value} {conn.footprint.split(':', 1)[-1]}"
        if conn.dnp or not _USB_TEXT.search(text) or _USB_A.search(text) or _HOST_PORT.search(text):
            continue
        for net in dict.fromkeys(pin.net for pin in conn.pins if pin.net and _VBUS_PIN.fullmatch(_name(pin))):
            # A load switch or eFuse on VBUS only protects what sits behind it, so it does not excuse capacitors
            # on VBUS itself; an IC output driving the net does (a hub or charger supplies, not receives).
            if net in design.grounds or _driven(design, net):
                continue
            caps = _ground_caps(design, net, conn)
            behind = {}  # series part -> its capacitors
            # Follow chains of such links (a fuse, then an OR-ing diode) up to three hops from the connector.
            seen, frontier = {net}, [net]
            for _ in range(3):
                frontier, here = [], frontier
                for near in here:
                    for part, far in _vbus_links(design, near, conn):
                        if far in seen:
                            continue
                        seen.add(far)
                        frontier.append(far)
                        counted = set(caps) | {ref for found in behind.values() for ref in found}
                        found = {ref: farad for ref, farad in _ground_caps(design, far, conn).items() if ref not in counted}
                        if found:
                            behind[part.reference] = found
            total = sum(caps.values(), Decimal(0)) + sum((sum(f.values(), Decimal(0)) for f in behind.values()),
                                                         Decimal(0))
            # Ceramics lose much of their value at 5 V (DC bias), the smaller the case the more: judge the
            # effective capacitance; a port with CC pull-ups (Rp) is a source, where the limit does not apply.
            refs = [*caps, *(ref for found in behind.values() for ref in found)]
            every = {**caps, **{ref: farad for found in behind.values() for ref, farad in found.items()}}
            effective = sum((every[ref] * _bias_factor(design.parts[ref]) for ref in refs), Decimal(0))
            if effective <= _USB_EFFECTIVE_MAX_F or _source_port(design, conn) or _power_only(design, conn):
                continue
            parts = [f"{', '.join(sorted(caps))} directly on the {conn.reference} VBUS net"] if caps else []
            parts += [f"{', '.join(sorted(found))} behind {part} ({design.parts[part].value})"
                      for part, found in behind.items()]
            findings.append(Finding(
                "usb.vbus_capacitance", Severity.INFO,
                f"{conn.reference} VBUS carries {float(total) * 1e6:.3g} µF, above the USB limit of 10 µF",
                (conn.reference, *sorted(caps), *(ref for part, found in behind.items() for ref in (part, *sorted(found)))),
                (net,),
                f"{'; '.join(parts)}: {float(total) * 1e6:.3g} µF nominal (about {float(effective) * 1e6:.3g} µF at "
                f"5 V bias) to ground charges from {net} at attach with no "
                "inrush limiter (a load switch only protects the capacitors behind it, not those on VBUS ahead of "
                "it, and a diode or ferrite does not limit the charge current); USB 2.0 allows a device at most "
                "10 µF at attach.",
                "Add a load switch or inrush limiter right after the connector and put the bulk capacitance behind "
                "it, or keep the capacitance on VBUS at 10 µF or less."))
    return findings


# --- inductive.flyback_missing ----------------------------------------------------------------

_RELAY_REF = re.compile(r"(?:K|RL)\d+", re.IGNORECASE)
_NOT_COIL_RELAY = re.compile(r"MODULE|SSR|(?<![A-Z])AQ[A-Z0-9]|G3MB|PHOTO.?MOS|TLP|OPTO", re.IGNORECASE)
_COIL_PIN = re.compile(r"COIL\d?[+\-]?|COIL_?[AB12+\-]|A[12]")
_PLAIN_COIL_PIN = re.compile(r"[+\-]|[12]")
_CONTACT_PIN = re.compile(r"COM\d?|NO\d?|NC\d?")
_SWITCH_PIN = re.compile(r"D|DRAIN|C|COLLECTOR")


def _coil_nets(relay: Component) -> tuple[str, str] | None:
    names = [_name(pin) for pin in relay.pins]
    pattern = _COIL_PIN
    if not any(pattern.fullmatch(name) for name in names):
        if not any(_CONTACT_PIN.fullmatch(name) for name in names):
            return None
        pattern = _PLAIN_COIL_PIN
    coil = [pin for pin in relay.pins if pattern.fullmatch(_name(pin))]
    nets = {pin.net for pin in coil}
    if len(coil) != 2 or len(nets) != 2 or "" in nets:
        return None
    return coil[0].net, coil[1].net


def _clamped(design: _Design, relay: Component, switched: str, other: str) -> bool:
    """A diode across the coil or from the switched net to a supply, or a capacitor snubber across it."""
    for net in (switched, other):
        for comp, _ in design.nodes[net]:
            ends = two_terminal_nets(comp)
            if comp is relay or not ends or comp.dnp or net not in ends:
                continue
            far = ends[1] if ends[0] == net else ends[0]
            kind = design.kind[comp.reference]
            if kind == "diode" and ({net, far} == {switched, other} or switched in ends and far in design.supplies):
                return True
            if kind == "capacitor" and {net, far} == {switched, other}:
                return True
    return False


def _is_coil_relay(design: _Design, relay: Component) -> bool:
    text = f"{relay.value} {relay.footprint}"
    return not (relay.dnp or not (_RELAY_REF.fullmatch(relay.reference) or "RELAY" in text.upper())
                or _NOT_COIL_RELAY.search(text) or design.kind[relay.reference] in {"resistor", "capacitor", "ic"})


def check_flyback_missing(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for relay in snapshot.components:
        if not _is_coil_relay(design, relay):
            continue
        coil = _coil_nets(relay)
        if coil is None:
            continue
        for switched, other in (coil, coil[::-1]):
            drivers = [comp for comp, pin in design.nodes[switched]
                       if design.kind[comp.reference] == "transistor" and _SWITCH_PIN.fullmatch(_name(pin))]
            unknown = [comp for comp, _ in design.nodes[switched]
                       if design.kind[comp.reference] in {"ic", "module", "other"} and comp is not relay]
            if not drivers or unknown or _clamped(design, relay, switched, other):
                continue
            refs = tuple(dict.fromkeys([relay.reference, *(comp.reference for comp in drivers)]))
            findings.append(Finding(
                "inductive.flyback_missing", Severity.ERROR,
                f"Relay {relay.reference} has no flyback diode across its coil",
                refs, (switched, other),
                f"{relay.reference} coil ({switched} to {other}) is switched by {', '.join(refs[1:])}, and no diode "
                "connects the two coil nets, so the voltage spike when the coil turns off can destroy the transistor.",
                "Add a diode (e.g. 1N4148 or 1N4007) across the coil, cathode to the supply side, close to the relay."))
            break
    return findings


# --- relay.coil_voltage -----------------------------------------------------------------------

# Standard coil voltages only: a contact rating such as 250V or 30VDC in the same text is never read as a coil.
_COIL_VOLTS = {Decimal(v) for v in ("3", "3.3", "4.5", "5", "6", "9", "12", "15", "18", "24", "48")}
_COIL_RATING = (
    re.compile(r"(?<![A-Z0-9.])(\d+(?:\.\d+)?)\s*V(?:DC)?(?![A-Z0-9])"),   # 05VDC, 12V
    re.compile(r"(?<![A-Z0-9])DC\s*(\d+(?:\.\d+)?)V?(?![A-Z0-9.])"),        # DC12, DC4.5
    re.compile(r"HF\w{1,5}/(\d{3})-"),                                       # Hongfa HF46F/024-HS1
    re.compile(r"(?<![\d.])[34]\d\.\d\d\.9\.(\d{3})(?!\d)"),                # Finder 40.52.9.024
)
_NOT_PLAIN_COIL = re.compile(r"LATCH|BISTABLE|DUAL|(?<![A-Z])AC\s*\d")


def _coil_rating(relay: Component) -> Decimal | None:
    """The one standard coil voltage the value and MPN state; None when absent or ambiguous."""
    found = set()
    for text in (relay.value, mpn_of(relay) or ""):
        text = text.upper()
        if _NOT_PLAIN_COIL.search(text):
            return None
        found.update(Decimal(m) for pattern in _COIL_RATING for m in pattern.findall(text))
    found = {volts for volts in found if volts in _COIL_VOLTS}
    return found.pop() if len(found) == 1 else None


def check_relay_coil_voltage(snapshot: DesignSnapshot) -> list[Finding]:
    """A relay whose stated coil voltage is far from the named supply rail on its coil's high side."""
    design = _Design(snapshot)
    findings = []
    for relay in snapshot.components:
        coil = _coil_nets(relay) if _is_coil_relay(design, relay) else None
        rated = _coil_rating(relay) if coil else None
        if rated is None:
            continue
        known = [(net, rail_voltage(net)) for net in coil if net in design.supplies and rail_voltage(net) is not None]
        if len(known) != 1:
            continue
        (rail, volts), = known
        low = coil[1] if coil[0] == rail else coil[0]
        if low in design.supplies or low in design.grounds:
            continue
        # The low side must reach only a switch (transistor/driver IC), clamp diode or snubber: a resistor
        # there is a deliberate series dropper, and anything else is not a plain coil drive.
        others = [(comp, pin) for comp, pin in design.nodes[low] if comp is not relay]
        kinds = {design.kind[comp.reference] for comp, _ in others}
        if kinds - {"transistor", "diode", "ic", "capacitor"} or not any(
                design.kind[comp.reference] == "ic" or _SWITCH_PIN.fullmatch(_name(pin))
                for comp, pin in others if design.kind[comp.reference] in {"transistor", "ic"}):
            continue
        if volts > rated * Decimal("1.5"):
            effect = "overdriven, so the coil overheats"
        elif volts < rated * Decimal("0.75"):
            effect = "underdriven, so the relay will not pull in reliably"
        else:
            continue
        findings.append(Finding(
            "relay.coil_voltage", Severity.ERROR,
            f"Relay {relay.reference} has a {_volts(rated)} coil on the {_volts(volts)} rail {rail}",
            (relay.reference,), (rail, low),
            f"{relay.reference} ({relay.value}) is rated for a {_volts(rated)} coil, but its coil connects {rail} "
            f"({_volts(volts)}) to {low}: {effect}.",
            f"Use a relay with a {_volts(volts)} coil, or supply the coil from a {_volts(rated)} rail."))
    return findings


# --- pin.power_unconnected --------------------------------------------------------------------

_OPTIONAL_POWER = re.compile(r"VBAT\w*|VBACKUP|VRTC|VPP")


_MODULE_BOARD = re.compile(_BUILT_IN_DECOUPLING.pattern + r"|MODULE|DEVKIT|(?<![A-Z])ZERO(?![A-Z])|ARDUINO|NANO")


# Pins that are normally just a capacitor to ground or an output: internal regulators, charge pumps, references.
_CAP_ONLY_PIN = re.compile(r"CAP|CORE|REG|DEC|LDO|OUT|BYP|VCP|VREF|VDDCR|VINT|VCCINT")


def _cap_only_pins(design: _Design, ic: Component) -> list[tuple[Pin, list[Component]]]:
    """Power inputs whose net holds, besides this IC's own power_in pins, only capacitors: no DC path feeds them.
    Fires only when every capacitor's other end is on a supply rail (a cap in series from a supply)."""
    result = []
    for pin in ic.pins:
        if (_base_type(pin) != "power_in" or pin.no_connect or not pin.net or pin.net.startswith("unconnected-(")
                or _is_return(design, pin.net) or _CAP_ONLY_PIN.search(_name(pin)) or _VCAP.fullmatch(_name(pin))
                or any(_NOT_SUPPLY_TOKEN.fullmatch(token) for token in re.split(r"[^A-Z0-9]+", _name(pin)))):
            continue
        nodes = design.nodes[pin.net]
        caps = list(dict.fromkeys(comp.reference for comp, _ in nodes if comp is not ic))
        if (not caps or any(_base_type(own) != "power_in" for comp, own in nodes if comp is ic)
                or any(design.kind[ref] != "capacitor" or design.parts[ref].dnp for ref in caps)):
            continue
        far = [_far_end(design.parts[ref], pin.net) for ref in caps]
        # Only a capacitor in series from a supply (other end on a rail, not ground) is the mistake; a pin
        # with caps to ground only is an internal regulator, charge-pump or gate-drive output typed power_in.
        if not far or any(end is None or _is_return(design, end) or not is_supply_name(end) for end in far):
            continue
        result.append((pin, [design.parts[ref] for ref in caps]))
    return result


def _far_end(cap: Component, net: str) -> str | None:
    ends = two_terminal_nets(cap)
    return None if ends is None or net not in ends else ends[1] if ends[0] == net else ends[0]


def _cap_fed_findings(design: _Design, ic: Component) -> list[Finding]:
    fed = _cap_only_pins(design, ic)
    if not fed:
        return []
    shown = []
    for pin, caps in fed:
        links = ", ".join(f"{cap.reference} (other end {_far_end(cap, pin.net) or 'open'})" for cap in caps)
        shown.append(f"{_pin_text(pin)} is fed only through {'capacitor' if len(caps) == 1 else 'capacitors'} "
                     f"{links}: no DC path")
    names = ", ".join(sorted({pin.name or pin.number for pin, _ in fed}))
    return [Finding(
        "pin.power_unconnected", Severity.ERROR,
        f"{ic.reference} power pin{'s' if len(fed) > 1 else ''} ({names}) fed only through a capacitor",
        (ic.reference, *dict.fromkeys(cap.reference for _, caps in fed for cap in caps)),
        tuple(dict.fromkeys(pin.net for pin, _ in fed)),
        f"{ic.reference} {'; '.join(shown)}. A capacitor blocks DC, so the pin gets no supply.",
        "Connect the pin to its supply (or to the rail directly), or flag it no-connect if the datasheet allows "
        "leaving it open.")]


# Outputs of an IC's own internal regulator or charge pump: the datasheet always asks for a capacitor here.
_INTERNAL_REG = re.compile(r"V3P3OUT|V5OUT|VCAP_?\d?|VREG_?OUT|VINT|INTVCC|LDO_?OUT|VCP")


def _vdda_small(design: _Design) -> list[Finding]:
    """An STM32's VDDA with under 1 uF to ground: ST's datasheets ask for 1 uF + 100 nF on VDDA (the ADC reference
    on small packages), and 100 nF alone leaves ADC readings noisy."""
    findings = []
    for ic in design.of_kind("ic"):
        pin = next((p for p in ic.pins if re.fullmatch(r"VDDA(?:/VREF\+)?|VREF\+/VDDA", _name(p)) and p.net), None)
        if ic.dnp or pin is None or not re.search(r"(?i)STM32", ic.value) or pin.net in design.grounds:
            continue
        total = Decimal(0)
        for comp, _ in design.nodes.get(pin.net, ()):
            ends = two_terminal_nets(comp)
            value = parse_value(comp.value, "capacitor") if design.kind[comp.reference] == "capacitor" else None
            if value and ends and any(end in design.grounds for end in ends):
                total += value.value
        if total < Decimal("1e-6"):
            findings.append(Finding(
                "decoupling.missing", Severity.WARNING,
                f"{ic.reference} VDDA has only {float(total) * 1e6:.2g} µF ({pin.net})", (ic.reference,), (pin.net,),
                f"{ic.reference} ({ic.value}) {_name(pin)} on {pin.net} has {float(total) * 1e6:.2g} µF to ground in all; "
                "ST recommends 1 µF plus 100 nF on VDDA, which also feeds the ADC reference.",
                "Add a 1 µF capacitor next to the 100 nF on VDDA (and on VREF+ if it is separate)."))
    return findings


def _charger_bat_bare(design: _Design) -> list[Finding]:
    """A Li-ion charger's BAT pin with no capacitor on its net: every charger datasheet puts 1-10 uF there, and with
    no cell (or a cell behind a protection FET) the charger output oscillates."""
    from .audit_rules import _LI_CHARGER
    findings = []
    for ic in design.of_kind("ic"):
        if ic.dnp or not _LI_CHARGER.search(ic.value):
            continue
        bat = next((pin.net for pin in ic.pins if re.fullmatch(r"V?BATT?\d?", re.sub(r"_", "", _name(pin)))
                    and pin.net and not pin.net.startswith("unconnected-(")), None)
        if bat and not any(design.kind[comp.reference] == "capacitor" for comp, _ in design.nodes.get(bat, ())):
            findings.append(Finding(
                "decoupling.missing", Severity.WARNING, f"{ic.reference} ({ic.value}) BAT has no capacitor",
                (ic.reference,), (bat,), f"{ic.reference} battery pin net {bat} has no capacitor at all; the {ic.value} "
                "datasheet asks for one (typically 1-10 uF) close to BAT, so with the cell absent or switched off by "
                "its protection the charger output can oscillate.",
                "Add the datasheet's BAT capacitor (e.g. 4.7 uF) from BAT to ground, next to the pin."))
    return findings


def _caps_to_nowhere(design: _Design) -> list[Finding]:
    """Capacitors whose second ends meet only each other, all first ends on one net: they bypass nothing (a ground
    or supply label missing from the shared end)."""
    findings = []
    for net, nodes in design.nodes.items():
        caps = [comp for comp, _ in nodes]
        if (len(nodes) < 2 or net in design.grounds or net in design.supplies or any(comp.dnp for comp in caps)
                or not all(design.kind[comp.reference] == "capacitor" and len(comp.pins) == 2 for comp in caps)):
            continue
        far = {pin.net for comp in caps for pin in comp.pins if pin.net != net}
        if len(far) != 1:
            continue
        refs = tuple(sorted(comp.reference for comp in caps))
        rail = next(iter(far))
        findings.append(Finding(
            "decoupling.missing", Severity.WARNING, f"{', '.join(refs)} connect {rail} to nothing", refs, (rail, net),
            f"{', '.join(refs)} each run from {rail} to {net}, and nothing else is on {net}, so they decouple nothing; "
            "the ground (or other) end was most likely meant to have a label or power symbol.",
            f"Connect the shared end ({net}) to ground, or to the net the capacitors were meant for."))
    return findings


def _regulator_pin_open(design: _Design) -> list[Finding]:
    """An internal-regulator output (DRV8825 V3P3OUT, STM32 VCAP, LTC INTVCC ...) left open or no-connect: the
    regulator has no output capacitor, so it may oscillate and the part misbehaves."""
    findings = []
    for ic in design.of_kind("ic"):
        open_pins = [pin for pin in ic.pins if _INTERNAL_REG.fullmatch(_name(pin))
                     and (not pin.net or pin.net.startswith("unconnected-("))]
        if ic.dnp or not open_pins:
            continue
        names = ", ".join(sorted({_name(pin) for pin in open_pins}))
        findings.append(Finding(
            "decoupling.missing", Severity.WARNING, f"{ic.reference} {names} has no capacitor (left open)",
            (ic.reference,), (), f"{ic.reference} ({ic.value}) {names} is the output of the part's internal regulator "
            "or charge pump and is left unconnected, so it has none of the capacitance the datasheet requires.",
            "Add the capacitor the datasheet gives for this pin (e.g. 0.47 uF for DRV8825 V3P3OUT, 2.2 uF for STM32 "
            "VCAP) from the pin to ground, close to it."))
    return findings


def _usb_ground_open(design: _Design) -> list[Finding]:
    """A USB connector whose data or VBUS pins are wired but every GND pin is open or flagged no-connect:
    the cable's ground never reaches the board, so neither power nor data has a return."""
    findings = []
    for conn in design.of_kind("connector"):
        names = {_name(pin) for pin in conn.pins}
        if conn.dnp or not (any(n.startswith("VBUS") for n in names) and names & {"D+", "D-", "DP1", "DN1", "DP", "DM", "CC1"}):
            continue
        grounds = [pin for pin in conn.pins if re.fullmatch(r"GND\d*", _name(pin))]
        live = [pin for pin in conn.pins if pin.net and not pin.net.startswith("unconnected-(") and not pin.no_connect]
        if grounds and live and all(pin.no_connect or not pin.net or pin.net.startswith("unconnected-(") for pin in grounds):
            findings.append(Finding(
                "pin.power_unconnected", Severity.ERROR, f"{conn.reference} USB ground pins are not connected",
                (conn.reference,), tuple(dict.fromkeys(pin.net for pin in live))[:4],
                f"{conn.reference} has {len(live)} wired pins ({', '.join(sorted({_name(p) for p in live})[:4])}) but all "
                f"{len(grounds)} GND pins are open or flagged no-connect, so the cable's ground never reaches the board.",
                "Connect every GND pin of the connector to the board ground."))
    return findings


def check_power_unconnected(snapshot: DesignSnapshot) -> list[Finding]:
    if not any(pin.electrical_type for comp in snapshot.components for pin in comp.pins):
        return []  # a PCB-sourced design has no pin types
    design = _Design(snapshot)
    findings = _usb_ground_open(design) + _regulator_pin_open(design) + _caps_to_nowhere(design) + _charger_bat_bare(design)
    findings += _vdda_small(design)
    for ic in design.of_kind("ic"):
        if ic.dnp:
            continue
        if not _MODULE_BOARD.search(f"{ic.value} {ic.footprint}".upper()):
            findings += _cap_fed_findings(design, ic)
        bad = [pin for pin in ic.pins if _base_type(pin) == "power_in" and not pin.no_connect
               and (not pin.net or pin.net.startswith("unconnected-("))
               and not _OPTIONAL_POWER.fullmatch(_name(pin))
               and not {"NC", "DNC"} & set(re.split(r"[^A-Z0-9]+", pin.name.upper()))]
        if not bad:
            continue
        powered = any(_base_type(pin) == "power_in" and pin.net and not pin.net.startswith("unconnected-(")
                      and not is_ground(pin.net) for pin in ic.pins)
        if powered and _MODULE_BOARD.search(f"{ic.value} {ic.footprint}".upper()):
            continue  # a dev-board module takes power on any one of its supply pins (5V or 3V3)
        names = ", ".join(sorted({pin.name or pin.number for pin in bad}))
        plural = "s" if len(bad) > 1 else ""
        findings.append(Finding(
            "pin.power_unconnected", Severity.ERROR,
            f"{ic.reference} has unconnected power pin{plural} ({names})",
            (ic.reference,), tuple(dict.fromkeys(pin.net for pin in bad if pin.net)),
            f"{ic.reference} {', '.join(_pin_text(pin) for pin in bad)} is a power input and is connected to "
            "nothing (no net, no no-connect flag).",
            "Connect the pin to its supply or ground, or flag it no-connect if the datasheet allows leaving it open."))
    return findings


# --- power.rail_split ---------------------------------------------------------------------------

def _rail_key(net: str) -> str:
    """+3.3V, 3V3 and /power/+3V3 all mean 3V3; 5V0 and +5V mean 5V. Other text in the name is kept."""
    name = net.rsplit("/", 1)[-1].upper().lstrip("+")
    name = re.sub(r"(\d+)\.(\d+)V", r"\1V\2", name)
    return re.sub(r"(\d+)V0(?!\d)", r"\1V", name)


def check_rail_split(snapshot: DesignSnapshot) -> list[Finding]:
    """Two supply nets whose names spell the same voltage differently (+3.3V and +3V3) with no part between
    them: KiCad joins only identical names, so the board has two separate rails and one may have no source."""
    design = _Design(snapshot)
    groups: dict[str, list[str]] = {}
    for net in sorted(design.supplies):
        # A net on one part only (a breakout's own 3V3 pins tied together) is not one of the board's rails.
        if len({c.reference for c, _ in design.nodes.get(net, ())}) >= 2 and re.search(r"\dV", _rail_key(net)):
            groups.setdefault(_rail_key(net), []).append(net)
    findings = []
    for nets in groups.values():
        if len({net.rsplit("/", 1)[-1] for net in nets}) < 2:
            continue   # one spelling on several sheets: local labels, separate on purpose
        joined = any(len({pin.net for pin in comp.pins} & set(nets)) > 1 for comp in snapshot.components)
        if joined:
            continue   # a ferrite, jumper or 0 ohm link between them: split on purpose
        def side(net: str) -> str:
            nodes = design.nodes[net]
            source = [f"{c.reference} {p.name}" for c, p in nodes if _base_type(p) == "power_out"]
            users = sorted({c.reference for c, p in nodes if _base_type(p) == "power_in"})
            return (f"{net}: {len(nodes)} pins" + (f", fed by {', '.join(source[:2])}" if source else ", no power output")
                    + (f", powers {', '.join(users[:5])}" if users else ""))
        findings.append(Finding(
            "power.rail_split", Severity.WARNING,
            f"{' and '.join(nets)} are separate nets but name the same rail",
            tuple(sorted({c.reference for net in nets for c, p in design.nodes[net]
                          if _base_type(p) in ("power_in", "power_out")}))[:8], tuple(nets),
            "; ".join(side(net) for net in nets) + ". Nothing connects them, so parts on one may get no supply.",
            f"Use one spelling for the rail (e.g. {nets[0]}) on every power symbol and label, or join them on purpose "
            "with a 0 ohm link or ferrite."))
    return findings


# --- connector.qwiic_pinout ----------------------------------------------------------------------

_QWIIC = re.compile(r"QWIIC|STEMMA|PRT-14417|PRT-14757", re.IGNORECASE)   # SparkFun's Qwiic connector part numbers


def check_qwiic_pinout(snapshot: DesignSnapshot) -> list[Finding]:
    """A connector labelled Qwiic / STEMMA QT must follow 1 GND, 2 VCC, 3 SDA, 4 SCL, or a standard cable
    connects the other board's pins to the wrong lines."""
    from .audit_rules import i2c_line, is_ground
    design = _Design(snapshot)
    findings = []
    for conn in design.of_kind("connector"):
        pins = {pin.number: pin.net for pin in conn.pins if pin.number in {"1", "2", "3", "4"}}
        if conn.dnp or len(pins) != 4 or not _QWIIC.search(f"{conn.value} {conn.footprint}"):
            continue
        role = {num: ("GND" if is_ground(net) else i2c_line(net) or "") for num, net in pins.items()}
        found = {r: num for num, r in role.items() if r in {"GND", "SDA", "SCL"}}
        if {"SDA", "SCL"} - set(found):
            continue   # not used as I2C here
        wrong = [f"{r} on pin {num} (should be {want})" for r, want in (("GND", "1"), ("SDA", "3"), ("SCL", "4"))
                 if (num := found.get(r)) and num != want]
        if wrong:
            findings.append(Finding(
                "connector.qwiic_pinout", Severity.ERROR,
                f"{conn.reference} Qwiic/STEMMA QT pins are in the wrong order", (conn.reference,),
                tuple(pins[n] for n in sorted(pins)),
                f"{conn.reference} ({conn.value}): " + "; ".join(wrong) + ". The standard is 1 GND, 2 VCC, 3 SDA, 4 SCL, "
                "so a standard cable joins this board's lines to the wrong pins of the other board.",
                "Rewire the connector to 1 GND, 2 3V3, 3 SDA, 4 SCL (check the footprint's pin 1 against the part)."))
    return findings


# --- transistor.p_low_side -----------------------------------------------------------------------

# P-channel / PNP by symbol or part: Vishay Si23x1/3/5/7/9 and AOS AO34x1/3/5/7/9 odd numbers are P-channel.
_P_PART = re.compile(r"PNP|PMOS|P[-_ ]?CHANNEL|Q_P[-_]|(?<![A-Z0-9])(?:SI23\d[13579]|AO34\d[13579]|DMP\d|DMG23\d5|IRLML640[12]|IRLML5203|IRLML9301|"
                     r"2N3906|BC807|BC857|MMBT3906|BSS84|FDN340P|NTR1P)", re.IGNORECASE)


def check_p_low_side(snapshot: DesignSnapshot) -> list[Finding]:
    """A P-channel MOSFET (or PNP) with its source (emitter) on ground: with the gate at 0 V or above it can
    never turn on, so as a low-side switch it never switches the load."""
    design = _Design(snapshot)
    groups: dict[str, list] = {}
    for part in design.of_kind("transistor"):
        if part.dnp or not _P_PART.search(f"{part.value} {part.fields.get('lib_id', '')}"):
            continue
        source = [pin for pin in part.pins if _SOURCE.fullmatch(_name(pin))]
        drain = [pin for pin in part.pins if _DRAIN.fullmatch(_name(pin))]
        if source and drain and all(pin.net in design.grounds for pin in source)                 and not any(pin.net in design.grounds for pin in drain):
            groups.setdefault(part.value, []).append((part, source[0].net, drain[0].net))
    findings = []
    for value, rows in groups.items():   # one finding per part number: the same wrong choice repeated
        refs = tuple(part.reference for part, _, _ in rows)
        part, ground, load = rows[0]
        findings.append(Finding(
            "transistor.p_low_side", Severity.ERROR,
            f"{', '.join(refs)} ({value}) {'is a' if len(refs) == 1 else 'are'} P-channel/PNP with the source on ground",
            refs, tuple(dict.fromkeys(n for _, g, d in rows for n in (g, d)))[:6],
            f"{part.reference} source/emitter is on {ground} and drain/collector on {load}: a P-channel MOSFET or PNP "
            "needs its gate/base below the source to conduct, which a 0-3.3 V drive on a grounded source never gives, "
            "so the load is never switched.",
            "Use an N-channel MOSFET (or NPN) for a low-side switch, or move the part to the high side with a gate "
            "pull-up and a driver."))
    return findings


# --- power.bypass_pin_cap ------------------------------------------------------------------------

def check_bypass_pin_cap(snapshot: DesignSnapshot) -> list[Finding]:
    """An LDO's noise-bypass pin (BP/BYP/NR) carrying more than 1 uF: datasheets ask for about 10 nF to 1 uF, so a
    470 uF there is almost always a pF or nF value typed as uF, and it slows the regulator's start-up badly."""
    design = _Design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        names = {_name(pin) for pin in ic.pins}
        ldo = names & {"IN", "VIN"} and names & {"OUT", "VOUT"} and not names & {"SW", "LX", "PH"}
        for pin in ic.pins:
            if ic.dnp or not ldo or not pin.net or not re.fullmatch(r"BP|BYP|BYPASS|NR|NR/SS|NR_SS|SS/NR", _name(pin)):
                continue
            for comp, _ in design.nodes.get(pin.net, ()):
                value = parse_value(comp.value, "capacitor") if design.kind[comp.reference] == "capacitor" else None
                if value and value.value > Decimal("1e-6"):
                    findings.append(Finding(
                        "power.bypass_pin_cap", Severity.WARNING,
                        f"{comp.reference} {comp.value} on {ic.reference} {_name(pin)} is far above the usual 10 nF-1 µF",
                        (comp.reference, ic.reference), (pin.net,),
                        f"{comp.reference} ({comp.value}) sits on {ic.reference} ({ic.value}) pin {_name(pin)}, the "
                        "regulator's noise-bypass pin, where datasheets use about 10 nF to 1 µF; a value this large is "
                        "most likely pF or nF typed as µF, and it makes the output rise very slowly.",
                        "Check the datasheet's bypass-capacitor value (often 470 pF to 10 nF) and correct the Value."))
    return findings


# --- reserved pins that the datasheet ties to ground ---------------------------------------------

# (part, reserved-pin names): WIZnet W5500 datasheet pin table: RSVD (23, 38-42) "tied to ground".
# ST LIS2DH12/LIS3DH: RES "connect to GND". Others (Quectel RESERVED, module RFU) are kept open and not listed.
_RESERVED_TO_GND = ((re.compile(r"W5500"), re.compile(r"RSVD\d*")), (re.compile(r"LIS2DH12|LIS3DH"), re.compile(r"RES\d*")))


def check_reserved_pins(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        names = next((pins for part, pins in _RESERVED_TO_GND if part.search(ic.value.upper())), None)
        if ic.dnp or names is None:
            continue
        loose = [pin for pin in ic.pins if names.fullmatch(_name(pin)) and pin.net not in design.grounds]
        if loose:
            numbers = ", ".join(pin.number for pin in loose)
            findings.append(Finding(
                "pin.input_floating", Severity.WARNING,
                f"{ic.reference} ({ic.value}) reserved pins {numbers} are not tied to ground", (ic.reference,),
                tuple(dict.fromkeys(pin.net for pin in loose if pin.net))[:4],
                f"{ic.reference} pins {numbers} ({_name(loose[0])}) are "
                f"{'left open' if all(not p.net or p.net.startswith('unconnected-(') for p in loose) else 'not on ground'}; "
                f"the {ic.value} datasheet says to tie its reserved pins to ground.",
                "Connect the reserved pins to GND as the datasheet's pin table says."))
    return findings
