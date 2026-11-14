"""Signal-mistake checks: crystal load capacitors, swapped USB D+/D- and CAN/RS-485 lines, and two outputs driving one net.

Same ground rules as audit_rules: deterministic, local, and silent when the netlist cannot show the
problem clearly. They run from audit_rules.RULES.
"""
from __future__ import annotations

from collections import defaultdict
import re

from .audit_rules import (_Design, _PROTECTION_VALUE, _base_type, _is_return, _pin_text, net_label,
                          terminals, two_terminal_nets)
from .bom import mpn_of
from .findings import Finding, Severity
from .models import Component, DesignSnapshot, Pin

_TOKENS = r"[^A-Z0-9_]+"


def _name(text: str) -> str:
    return re.sub(r"[~{}]", "", text.upper()).strip()


# --- crystal.load_caps_missing ----------------------------------------------------------------

_CRYSTAL_PREFIX = {"Y", "X", "XTAL"}
_CRYSTAL_FOOTPRINT = re.compile(r"CRYSTAL|HC-?49|XTAL")
# Active oscillators, TCXOs and ceramic resonators (3 pads with built-in capacitors) need no load capacitors.
_NOT_A_PLAIN_CRYSTAL = re.compile(
    r"OSCILLATOR|(?<![A-Z0-9])(?:OSC|XO|TCXO|VCXO|OCXO|VCTCXO|MEMS|CLOCK)(?![A-Z0-9])|RESONATOR|"
    r"(?<![A-Z0-9])CST[A-Z]{1,2}\d|SIT\d{4}|DSC\d{4}")
_ACTIVE_PIN = re.compile(r"V?DD|VCC|VEE|OUT|OUTPUT|OE|N?EN|ENABLE|STANDBY")
_XTAL_PIN = re.compile(
    r"XIN|XOUT|XI|XO|XT[12]|X32K\w*|XTAL[IO]?\d?|XTAL_?(?:IN|OUT|[NP])|XTAL_?32K\w*|XTAL32\w*|TOSC[12]|"
    r"OSC\d?|OSC_?(?:IN|OUT|[IO12])|OSC32_?(?:IN|OUT)\d?|32K_?X[PN]|PH[01]|PC1[45]|"
    r"X[12]|XC[12]|XL[12]|XT[AB]|XT[IO]|CKXTAL\d|XTAL\d?_(?:IN|OUT)|P[DF][01]")
_WATCH_CRYSTAL = re.compile(r"32[.,]?768|(?<![A-Z0-9])32\s*K(?:HZ)?(?![A-Z0-9])")
# RTC chips with internal load capacitors: a 32.768 kHz watch crystal goes straight on their pins.
_RTC_CHIP = re.compile(r"PCF85\d\d|PCF2\d{3}|DS13[0-9]\d|DS32\d\d|MCP794\d|RV-?\d|RX-?[48]\d{3}|ISL12\d\d|"
                       r"M41T\d|BQ32\d\d|MAX313\d\d|AB-?08\d\d|AM18\d\d|(?<![A-Z0-9])RTC")
# Chips with on-chip crystal load capacitors: no external ones are fitted. WCH USB parts (CH334/CH340 and
# relatives) integrate them per their datasheets.
_INTERNAL_CAPS = re.compile(r"NRF54[LH]|BM15|EFR32|EFM32|(?<![A-Z0-9])[BM]GM\d|(?<![A-Z0-9])CH3[2-4]\d")


def _is_crystal(comp: Component, kind: str) -> bool:
    if kind in {"connector", "module"}:
        return False
    prefix = (re.match(r"[A-Za-z]+", comp.reference) or [""])[0].upper()
    footprint = comp.footprint.upper()
    return (prefix in _CRYSTAL_PREFIX or bool(_CRYSTAL_FOOTPRINT.search(footprint))) \
        and "RESONATOR" not in footprint and "OSCILLATOR" not in footprint


def _xtal_pin(pin: Pin) -> bool:
    return any(_XTAL_PIN.fullmatch(token) for token in re.split(_TOKENS, _name(pin.name)) if token)


def _has_ground_cap(design: _Design, net: str) -> bool:
    for comp, _ in design.nodes[net]:
        ends = two_terminal_nets(comp)
        if (design.kind[comp.reference] == "capacitor" or re.match(r"(?:TC|VC|CT)\d", comp.reference.upper())) \
                and ends and net in ends and any(_is_return(design, end) for end in ends if end != net):
            return True
    return False


def check_crystal_load_caps(snapshot: DesignSnapshot) -> list[Finding]:
    """A passive crystal whose two signal nets reach the IC's oscillator pins needs a capacitor to ground on each."""
    design = _Design(snapshot)
    if not design.grounds:
        return []
    findings = []
    for comp in snapshot.components:
        if not _is_crystal(comp, design.kind[comp.reference]) or comp.dnp:
            continue
        text = f"{comp.value} {comp.footprint}".upper()
        pads = terminals(comp)
        if (_NOT_A_PLAIN_CRYSTAL.search(text) or len(pads) not in (2, 4)
                or any(_base_type(pin) in {"power_in", "power_out", "output"}
                       or any(_ACTIVE_PIN.fullmatch(t) for t in re.split(_TOKENS, _name(pin.name)))
                       for pin in comp.pins)):
            continue
        signal = [net for net in pads.values() if net and not _is_return(design, net)]
        if len(signal) != 2 or signal[0] == signal[1] or any(net in design.supplies for net in signal):
            continue
        # Each net must reach an oscillator pin of an IC (not a module, which has its own), else stay silent.
        ics = []
        for net in signal:
            hits = [(other, pin) for other, pin in design.nodes[net]
                    if other is not comp and design.kind[other.reference] == "ic" and _xtal_pin(pin)]
            if not hits:
                break
            ics.append(hits)
        else:
            chips = [f"{other.value} {mpn_of(other) or ''} {other.footprint} {other.fields.get('lib_id', '')}".upper()
                     for hits in ics for other, _ in hits]
            if any(_INTERNAL_CAPS.search(chip) or (_WATCH_CRYSTAL.search(text) and _RTC_CHIP.search(chip))
                   for chip in chips):
                continue
            missing = [net for net in signal if not _has_ground_cap(design, net)]
            if not missing:
                continue
            chip, pin = ics[0][0]
            findings.append(Finding(
                "crystal.load_caps_missing", Severity.WARNING,
                f"Crystal {comp.reference} has no load capacitor to ground on " + " and ".join(missing),
                refs=(comp.reference, chip.reference), nets=tuple(missing),
                evidence=(f"{comp.reference} ({comp.value or 'crystal'}) connects {signal[0]} and {signal[1]} to "
                          f"{chip.reference} oscillator pins ({_pin_text(pin)}), and "
                          + ("neither net has" if len(missing) == 2 else f"{missing[0]} has no")
                          + " a capacitor to ground."),
                fix=("Add a capacitor from each crystal pin to ground, sized from the crystal's load capacitance "
                     "(roughly 2 x CL minus the board's stray capacitance, often 12-22 pF), unless the chip's "
                     "datasheet says it has them built in.")))
    return findings


# --- usb.dp_dn_swapped ------------------------------------------------------------------------

_USB_ADORN = r"(?:(?:USB|HUB|HOST|DEV|DEVICE|UP|PORT|CONN|CON|MCU|ESD|PHY|OTG|FS|HS|EXT)\d*[_\-]?)*"
_USB_TAIL = r"(?:[_\-]?(?:CONN|CON|ESD|MCU|IN|OUT|RAW|FILT|PHY|HUB|HOST|DEV|UP|J\d*|\d+))*"
_USB_PLUS = re.compile(_USB_ADORN + r"(?:D\+|DP|D_P|DPLUS|DPOS)\d?" + _USB_TAIL)
_USB_MINUS = re.compile(_USB_ADORN + r"(?:D-|DM|DN|D_N|D_M|DMINUS|DNEG)\d?" + _USB_TAIL)


def _polarity(text: str) -> str:
    """"+" or "-" when every alternative in a net/pin name says the same USB polarity, else ""."""
    found = set()
    for alt in re.split(r"[/\s,()]+|(?<=\d)-(?=[A-Z])", _name(text)):
        if _USB_PLUS.fullmatch(alt):
            found.add("+")
        elif _USB_MINUS.fullmatch(alt):
            found.add("-")
    return found.pop() if len(found) == 1 else ""


def check_usb_dp_dn_swapped(snapshot: DesignSnapshot) -> list[Finding]:
    """A net named D+ that reaches a D- pin of an IC or connector, or the reverse; or a D+ and a D- pin sharing a net."""
    design = _Design(snapshot)
    findings = []
    for net, nodes in design.nodes.items():
        label = _polarity(net_label(net))
        polar: dict[str, list[tuple[Component, Pin]]] = {"+": [], "-": []}
        for comp, pin in nodes:
            if design.kind[comp.reference] in {"ic", "connector"} and not _PROTECTION_VALUE.search(comp.value) \
                    and (sign := _polarity(pin.name)):
                polar[sign].append((comp, pin))
        # One part with both polarities on the net is a deliberate short (charger D+/D- strap), not a swap.
        if {comp.reference for comp, _ in polar["+"]} & {comp.reference for comp, _ in polar["-"]}:
            continue
        other = "-" if label == "+" else "+"
        if label:
            hits, text = polar[other], f"Net {net} is named as USB D{label}, yet "
        elif polar["+"] and polar["-"]:
            hits, text = polar["+"] + polar["-"], f"D+ and D- pins share net {net}: "
        else:
            continue
        if not hits:
            continue
        shown = ", ".join(f"{comp.reference} {_pin_text(pin)}" for comp, pin in hits)
        title = (f"USB net {net_label(net) or net} is named D{label} but reaches a D{other} pin ({shown})"
                 if label else f"USB D+ and D- pins share net {net_label(net) or net}")
        findings.append(Finding(
            "usb.dp_dn_swapped", Severity.WARNING, title,
            refs=tuple(sorted({comp.reference for comp, _ in hits})), nets=(net,),
            evidence=text + (f"{shown} is named D{other}." if label else shown + "."),
            fix="Swap the two data lines so D+ goes to D+ and D- to D- (check both ends against the datasheets)."))
    return findings


# --- net.output_contention --------------------------------------------------------------------

# Flag/alert pins that symbols often type "output" although the chip drives them open-drain (wired-OR is normal).
_FLAG_PIN = re.compile(r"\d*(?:N?INT\d*|N?IRQ\d*|N?ALERT|SMBALERT|N?FAULT|N?FLT|N?PG|PGOOD|N?RDY|N?READY|N?BUSY|"
                       r"N?OD|N?WAKE|N?RESET|N?RST|N?SYSRST|N?OVF|N?ERR|N?ERROR|N?ACT|N?OC)\d*")


def check_output_contention(snapshot: DesignSnapshot) -> list[Finding]:
    """Two or more push-pull output pins from different parts on one net."""
    design = _Design(snapshot)
    findings = []
    for net, nodes in design.nodes.items():
        if net in design.grounds or net in design.supplies:
            continue
        drivers = [(comp, pin) for comp, pin in nodes if _base_type(pin) == "output" and not comp.dnp]
        refs = sorted({comp.reference for comp, _ in drivers})
        if len(refs) < 2:
            continue
        if any(design.kind[comp.reference] == "resistor" and (ends := two_terminal_nets(comp)) and net in ends
               and any(end in design.supplies for end in ends) for comp, _ in nodes):
            continue  # a pull-up marks a wired-OR line: the outputs are open-drain in practice
        if any(_FLAG_PIN.fullmatch(token) for _, pin in drivers
               for token in re.split(_TOKENS, _name(pin.name)) if token):
            continue
        shown = ", ".join(f"{comp.reference} {_pin_text(pin)}" for comp, pin in drivers)
        findings.append(Finding(
            "net.output_contention", Severity.WARNING,
            f"Net {net_label(net) or net} is driven by {len(refs)} outputs at once ({', '.join(refs)})",
            refs=tuple(refs), nets=(net,),
            evidence=f"Push-pull output pins on the same net: {shown}. Two outputs fighting over one line can "
                     "damage the chips or give wrong levels.",
            fix="Drive the net from one output only: separate the nets, put a series resistor or a switch/mux "
                "between them, or use open-drain/tri-state outputs with a pull-up."))
    return findings


# --- bus.polarity_swapped ---------------------------------------------------------------------

_CAN_SIDE = re.compile(r"(?<![A-Z0-9])CAN\d?[_\-]?(H(?:IGH)?|L(?:OW)?|\+|-)(?![A-Z])")
_RS485_SIDE = re.compile(r"(?<![A-Z0-9])(?:RS[_\-]?)?485[_\-]?([AB])(?![A-Z0-9]|[_\-][AB](?![A-Z0-9]))")
# Only the exact pin names A/B count, and only on a part that also has driver/receiver control or data pins.
_RS485_CONTROL = {"DE", "RE", "NRE", "RO", "DI", "RXD", "TXD"}
_NOT_A_TRANSCEIVER = re.compile(r"CHOKE|COMMON.?MODE|ACT\d|DLW|SDCW|ESD|TVS|PESD|TPD\d|USBLC", re.IGNORECASE)


def _can_side(text: str) -> str:
    """"H" or "L" when every CAN token in a name says the same side, else ""."""
    sides = {"H" if token[0] in "H+" else "L" for token in _CAN_SIDE.findall(text)}
    return sides.pop() if len(sides) == 1 else ""


def _rs485_side(text: str) -> str:
    sides = set(_RS485_SIDE.findall(text))
    return sides.pop() if len(sides) == 1 else ""


def check_bus_polarity_swapped(snapshot: DesignSnapshot) -> list[Finding]:
    """A net named for one line of a CAN or RS-485 bus that lands on a transceiver pin named for the other."""
    design = _Design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        if ic.dnp or _PROTECTION_VALUE.search(ic.value) or _NOT_A_TRANSCEIVER.search(ic.value):
            continue
        names = {_name(pin.name) for pin in ic.pins}
        buses = (("CAN", lambda pin: _can_side(_name(pin.name)), _can_side),
                 ("RS-485", lambda pin: _name(pin.name) if _name(pin.name) in {"A", "B"} else "", _rs485_side))
        for bus, pin_side, net_side in buses:
            if bus == "RS-485" and not names & _RS485_CONTROL:
                continue
            pins = [(pin, pin_side(pin)) for pin in ic.pins if pin.net and not pin.no_connect]
            pins = [(pin, side) for pin, side in pins if side]
            hits = [(pin, side) for pin, side in pins
                    if (wired := net_side(net_label(pin.net))) and wired != side]
            if not hits:
                continue
            # One bad pin next to an unnamed partner net is too thin: the single net name may be the mistake.
            if len(hits) == 1 and not any(net_label(pin.net) for pin, side in pins if side != hits[0][1]):
                continue
            nets = tuple(dict.fromkeys(pin.net for pin, _ in hits))
            conns = sorted({comp.reference for net in nets for comp, _ in design.nodes[net]
                            if design.kind[comp.reference] == "connector"})
            shown = "; ".join(f"{_pin_text(pin)} on {net_label(pin.net)}" for pin, _ in hits)
            findings.append(Finding(
                "bus.polarity_swapped", Severity.ERROR,
                f"{ic.reference} {bus} lines look swapped ({shown})",
                refs=(ic.reference, *conns), nets=nets,
                evidence=f"{ic.reference}: {shown}. Each net is named for the opposite {bus} line to the pin it "
                         "reaches." + (f" Connectors on these nets: {', '.join(conns)}." if conns else ""),
                fix=f"Swap the two {bus} lines so each net reaches the pin of the same name "
                    "(check both ends against the datasheets)."))
    return findings
