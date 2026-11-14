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
from .bom import package_of, parse_value, rail_voltage
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


def check_cap_voltage_rating(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for comp in design.of_kind("capacitor"):
        ends = two_terminal_nets(comp)
        rated = _rated_volts(comp)
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


def check_resistor_power_rating(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
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
            for part, far in _vbus_links(design, net, conn):
                found = {ref: farad for ref, farad in _ground_caps(design, far, conn).items() if ref not in caps}
                if found:
                    behind[part.reference] = found
            total = sum(caps.values(), Decimal(0)) + sum((sum(f.values(), Decimal(0)) for f in behind.values()),
                                                         Decimal(0))
            # Ceramics lose much of their value at 5 V (DC bias), the smaller the case the more: judge the
            # effective capacitance; a port with CC pull-ups (Rp) is a source, where the limit does not apply.
            refs = [*caps, *(ref for found in behind.values() for ref in found)]
            every = {**caps, **{ref: farad for found in behind.values() for ref, farad in found.items()}}
            effective = sum((every[ref] * _bias_factor(design.parts[ref]) for ref in refs), Decimal(0))
            if effective <= _USB_EFFECTIVE_MAX_F or _source_port(design, conn):
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


def check_flyback_missing(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for relay in snapshot.components:
        text = f"{relay.value} {relay.footprint}"
        if (relay.dnp or not (_RELAY_REF.fullmatch(relay.reference) or "RELAY" in text.upper())
                or _NOT_COIL_RELAY.search(text) or design.kind[relay.reference] in {"resistor", "capacitor", "ic"}):
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


def check_power_unconnected(snapshot: DesignSnapshot) -> list[Finding]:
    if not any(pin.electrical_type for comp in snapshot.components for pin in comp.pins):
        return []  # a PCB-sourced design has no pin types
    design = _Design(snapshot)
    findings = []
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
