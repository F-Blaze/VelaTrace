"""Electrical-rating and common-circuit audit rules.

  cap.voltage_rating        capacitor rated below the nominal voltage of the rail it sits on
  resistor.power_rating     LED series resistor that dissipates more than its package rating
  usb.vbus_capacitance      more than 10 uF on a USB device's VBUS net with no inrush limiter
  inductive.flyback_missing relay coil switched by a transistor with no flyback diode
  pin.power_unconnected     IC power_in pin left open without a no-connect flag

Like the rest of audit_rules these stay quiet whenever the netlist cannot show the problem clearly.
"""
from __future__ import annotations

from decimal import Decimal
import re

from .audit_rules import (Check, _Design, _PROTECTING_IC, _base_type, _is_return, _pin_text, parse_resistance,
                          two_terminal_nets)
from .bom import package_of, parse_value, rail_voltage
from .findings import Finding, Severity
from .models import Component, DesignSnapshot, Pin


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


def check_resistor_power_rating(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for led in design.of_kind("led"):
        anode, cathode, vf = _led_pin_net(led, _ANODE), _led_pin_net(led, _CATHODE), _forward_volts(led)
        if not anode or not cathode or vf is None or led.dnp or len(led.pins) != 2:
            continue
        cases = []  # (resistor, rail, ground)
        if cathode in design.grounds and (found := _series_resistor(design, anode)):
            cases.append((found[0], found[1], cathode))
        if (found := _series_resistor(design, cathode)):
            cases.append((found[0], anode, found[1]))
        for resistor, rail, ground in cases:
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
            findings.append(Finding(
                "resistor.power_rating", Severity.WARNING,
                f"{resistor.reference} dissipates about {power:.2f} W, above its {watts:g} W package rating",
                (resistor.reference, led.reference), (rail,),
                f"{resistor.reference} ({resistor.value}) limits {led.reference} from {rail}: "
                f"({volts:g} V - {vf:g} V) / {ohms:g} Ω = {amps * 1000:.0f} mA, so P = I²R = {power:.2f} W, "
                f"but a {size} resistor is rated {watts:g} W (LED drop assumed {vf:g} V).",
                "Raise the resistance to cut the current, or use a larger package or a higher-power resistor."))
    return findings


# --- usb.vbus_capacitance ---------------------------------------------------------------------

_USB_TEXT = re.compile(r"USB", re.IGNORECASE)
_USB_A = re.compile(r"USB[_\- ]?A(?![A-Z])|TYPE[_\- ]?A(?![A-Z])", re.IGNORECASE)  # host receptacles drive VBUS
_VBUS_PIN = re.compile(r"VBUS\d*")
_INRUSH_PART = re.compile(
    r"LOAD.?SWITCH|SWITCH|E-?FUSE|HOT.?SWAP|TPS2\d{3}|TPS22\d{3}|TPS259|AP2[0-5]\d\d|SY6280|RT9742|MIC20\d\d|"
    r"MIC94\d\d|STMPS|TCK\d{3}|NCP3\d\d|FPF\d|SIP32|CH217|BQ2\d{4}|MCP738|TP405|CHARGER|HUB|HOST|"
    r"USB2517|CH334|GL850|FE1\.1", re.IGNORECASE)
_USB_LIMIT_F = Decimal("10e-6")


def _limits_inrush(design: _Design, comp: Component, pin: Pin) -> bool:
    """A load switch, eFuse, charger or hub on the net, or any IC output that drives it."""
    text = f"{comp.value} {comp.footprint}"
    if _PROTECTING_IC.search(text) or _INRUSH_PART.search(comp.value):
        return True
    if design.kind[comp.reference] not in {"ic", "module"}:
        return False
    return _base_type(pin) == "power_out" or (_name(pin) in {"VIN", "IN"} and bool(_INRUSH_PART.search(text)))


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


def check_vbus_capacitance(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for conn in design.of_kind("connector"):
        text = f"{conn.value} {conn.footprint.split(':', 1)[-1]}"
        if conn.dnp or not _USB_TEXT.search(text) or _USB_A.search(text):
            continue
        for net in dict.fromkeys(pin.net for pin in conn.pins if pin.net and _VBUS_PIN.fullmatch(_name(pin))):
            if net in design.grounds:
                continue
            others = [(comp, pin) for comp, pin in design.nodes[net] if comp is not conn]
            if any(_limits_inrush(design, comp, pin) for comp, pin in others):
                continue
            caps = {}
            for comp, _ in others:
                ends = two_terminal_nets(comp)
                value = parse_value(comp.value, "capacitor") if design.kind[comp.reference] == "capacitor" else None
                if (ends and not comp.dnp and value is not None and net in ends
                        and any(end in design.grounds for end in ends)):
                    caps[comp.reference] = value.value
            total = sum(caps.values(), Decimal(0))
            # Ceramics lose much of their value at 5 V, so only a clear excess is reported; a port with
            # CC pull-ups (Rp) is a source, where the limit does not apply.
            if total <= _USB_LIMIT_F * Decimal("1.5") or _source_port(design, conn):
                continue
            findings.append(Finding(
                "usb.vbus_capacitance", Severity.INFO,
                f"{conn.reference} VBUS carries {float(total) * 1e6:.3g} µF, above the USB limit of 10 µF",
                (conn.reference, *sorted(caps)), (net,),
                f"{', '.join(sorted(caps))} total {float(total) * 1e6:.3g} µF from {net} to ground, directly on the "
                f"{conn.reference} VBUS net with no load switch or current limiter; USB 2.0 allows a device "
                "at most 10 µF at attach.",
                "Add a load switch or inrush limiter after the connector, or keep the capacitance on VBUS at "
                "10 µF or less and put the bulk capacitance behind the switch."))
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


def check_power_unconnected(snapshot: DesignSnapshot) -> list[Finding]:
    if not any(pin.electrical_type for comp in snapshot.components for pin in comp.pins):
        return []  # a PCB-sourced design has no pin types
    design = _Design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        if ic.dnp:
            continue
        bad = [pin for pin in ic.pins if _base_type(pin) == "power_in" and not pin.no_connect
               and (not pin.net or pin.net.startswith("unconnected-("))
               and not _OPTIONAL_POWER.fullmatch(_name(pin))
               and not {"NC", "DNC"} & set(re.split(r"[^A-Z0-9]+", pin.name.upper()))]
        if not bad:
            continue
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


RATINGS_RULES: dict[str, Check] = {
    "cap.voltage_rating": check_cap_voltage_rating,
    "resistor.power_rating": check_resistor_power_rating,
    "usb.vbus_capacitance": check_vbus_capacitance,
    "inductive.flyback_missing": check_flyback_missing,
    "pin.power_unconnected": check_power_unconnected,
}
