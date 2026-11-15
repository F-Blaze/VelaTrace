"""Signal-mistake checks: crystal load capacitors, swapped USB D+/D- and CAN/RS-485 lines, and two outputs driving one net.

Same ground rules as audit_rules: deterministic, local, and silent when the netlist cannot show the
problem clearly. They run from audit_rules.RULES.
"""
from __future__ import annotations

from collections import defaultdict
import re

from .audit_rules import (_Design, _PROTECTION_VALUE, _base_type, _is_return, _pin_text, net_label,
                          parse_resistance, terminals, two_terminal_nets)
from .bom import mpn_of, parse_value
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


def _crystals(design: _Design):
    """(crystal, text, its two signal nets, oscillator-pin hits per net) for each passive crystal on an IC's
    oscillator pins, skipping chips with built-in load capacitors."""
    for comp in design.snapshot.components:
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
            yield comp, text, signal, ics


def check_crystal_load_caps(snapshot: DesignSnapshot) -> list[Finding]:
    """A passive crystal whose two signal nets reach the IC's oscillator pins needs a capacitor to ground on each."""
    design = _Design(snapshot)
    if not design.grounds:
        return []
    findings = []
    for comp, _, signal, ics in _crystals(design):
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


# --- crystal.load_caps_value ------------------------------------------------------------------

_KHZ = re.compile(r"(\d+(?:[.,]\d+)?)\s*KHZ")


def check_crystal_load_caps_value(snapshot: DesignSnapshot) -> list[Finding]:
    """The single load capacitor on each crystal pin is far outside the usual 6-33 pF (above 47 pF or below 4 pF)."""
    design = _Design(snapshot)
    if not design.grounds:
        return []
    findings = []
    for comp, text, signal, ics in _crystals(design):
        # Low-frequency crystals (other than 32.768 kHz watch crystals) legitimately use larger capacitors.
        if any(not 32.7 <= float(m.replace(",", ".")) <= 32.8 for m in _KHZ.findall(text)):
            continue
        caps = []
        for net in signal:
            on_net = [other for other, _ in design.nodes[net]
                      if design.kind[other.reference] == "capacitor" and (ends := two_terminal_nets(other))
                      and net in ends and any(_is_return(design, end) for end in ends if end != net)]
            if len(on_net) != 1 or on_net[0].dnp:
                break
            caps.append(on_net[0])
        else:
            parsed = [parse_value(cap.value, "capacitor") for cap in caps]
            if any(value is None or value.value <= 0 for value in parsed):
                continue
            picofarads = [value.value * 10**12 for value in parsed]
            bad = [cap for cap, pf in zip(caps, picofarads) if pf > 47 or pf < 4]
            if not bad:
                continue
            shown = ", ".join(f"{cap.reference} {cap.value}" for cap in bad)
            findings.append(Finding(
                "crystal.load_caps_value", Severity.WARNING,
                f"Crystal {comp.reference} load capacitor value looks wrong ({shown})",
                refs=(comp.reference, *(cap.reference for cap in bad)), nets=tuple(signal),
                evidence=(f"{comp.reference} ({comp.value or 'crystal'}) has {caps[0].reference} ({caps[0].value}) "
                          f"and {caps[1].reference} ({caps[1].value}) as load capacitors. Typical values are "
                          "6-33 pF; above about 47 pF most crystals fail to start, below 4 pF the load is too light."),
                fix="Size each load capacitor from the crystal's load capacitance: roughly 2 x CL minus the "
                    "board's stray capacitance (about 3-5 pF), often 12-22 pF."))
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


# Flow-through ESD arrays: each pin pair is one internal line, so the nets on both pins are the same signal.
_FLOW_THROUGH = re.compile(r"USBLC6-2")
_FLOW_PAIRS = (("1", "6"), ("3", "4"))


def _line_groups(design: _Design) -> list[list[str]]:
    """Nets grouped across flow-through ESD arrays; every other net is a group of its own."""
    parent: dict[str, str] = {}

    def find(net: str) -> str:
        while parent.get(net, net) != net:
            net = parent[net]
        return net

    for comp in design.snapshot.components:
        if comp.dnp or not _FLOW_THROUGH.search(comp.value.upper()):
            continue
        pads = terminals(comp)
        for first, second in _FLOW_PAIRS:
            na, nb = pads.get(first, ""), pads.get(second, "")
            if na and nb and na != nb and not {na, nb} & (design.grounds | design.supplies):
                parent[find(na)] = find(nb)
    groups: dict[str, list[str]] = defaultdict(list)
    for net in design.nodes:
        groups[find(net)].append(net)
    return list(groups.values())


def check_usb_dp_dn_swapped(snapshot: DesignSnapshot) -> list[Finding]:
    """A net named D+ that reaches a D- pin of an IC or connector, or the reverse; or a D+ and a D- pin sharing a net.

    Nets on the two sides of a flow-through ESD array (USBLC6-2) count as one line."""
    design = _Design(snapshot)
    findings = []
    for nets in _line_groups(design):
        net, joined = nets[0], len(nets) > 1
        labels = {_polarity(net_label(n)) for n in nets} - {""}
        label = labels.pop() if len(labels) == 1 else ""
        polar: dict[str, list[tuple[Component, Pin]]] = {"+": [], "-": []}
        for comp, pin in (node for n in nets for node in design.nodes[n]):
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
        if label:
            title = f"USB net {net_label(net) or net} is named D{label} but reaches a D{other} pin ({shown})"
        elif joined:
            title = f"USB D+ and D- pins are on one line through an ESD array ({shown})"
            text = "D+ and D- pins are joined through a flow-through ESD array (same internal line): "
        else:
            title = f"USB D+ and D- pins share net {net_label(net) or net}"
        findings.append(Finding(
            "usb.dp_dn_swapped", Severity.WARNING, title,
            refs=tuple(sorted({comp.reference for comp, _ in hits})), nets=tuple(nets),
            evidence=text + (f"{shown} is named D{other}." if label else shown + "."),
            fix="Swap the two data lines so D+ goes to D+ and D- to D- (check both ends against the datasheets)."))
    return findings


# --- net.output_contention --------------------------------------------------------------------

# Flag/alert pins that symbols often type "output" although the chip drives them open-drain (wired-OR is normal).
_FLAG_PIN = re.compile(r"\d*(?:N?INT\d*|N?IRQ\d*|N?ALERT|SMBALERT|N?FAULT|N?FLT|N?PG|PGOOD|N?RDY|N?READY|N?BUSY|"
                       r"N?OD|N?WAKE|N?RESET|N?RST|N?SYSRST|N?OVF|N?ERR|N?ERROR|N?ACT|N?OC)\d*")


# Serial data outputs that are high-impedance unless their chip is selected (SPI SDO/MISO, JTAG TDO).
_TRISTATE_PIN = re.compile(r"SDO\d*|MISO\d*|CIPO|SO|DO|DOUT|TDO|SPI\d*_?MISO")


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
        if all(any(_TRISTATE_PIN.fullmatch(token) for token in re.split(_TOKENS, _name(pin.name)) if token)
               for _, pin in drivers):
            continue  # SPI/JTAG data outputs are tri-stated while their chip is not selected: a shared MISO is normal
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


# CiA 303-1 D-sub 9 pinout: pin 7 is CAN_H and pin 2 is CAN_L.
_DSUB9 = re.compile(r"(?<![A-Z0-9])(?:DB-?9|DE-?9|D-?SUB[_\- ]*(?:D[BE]-?)?9)(?![0-9])")


def _dsub_can_findings(design: _Design) -> list[Finding]:
    findings = []
    for conn in design.of_kind("connector"):
        pads = terminals(conn)
        numbers = {number for number in pads if number.isdigit()}
        if (conn.dnp or not _DSUB9.search(f"{conn.value} {conn.footprint}".upper())
                or not {str(n) for n in range(1, 10)} <= numbers or any(int(n) > 9 for n in numbers)):
            continue
        low, high = pads["2"], pads["7"]
        if _can_side(net_label(low)) != "H" or _can_side(net_label(high)) != "L":
            continue
        findings.append(Finding(
            "bus.polarity_swapped", Severity.ERROR,
            f"{conn.reference} D-sub CAN lines look swapped (pin 2 on {net_label(low)}, pin 7 on {net_label(high)})",
            refs=(conn.reference,), nets=(low, high),
            evidence=f"{conn.reference}: pin 2 carries {net_label(low)} and pin 7 carries {net_label(high)}, but the "
                     "CiA 303-1 D-sub 9 pinout is pin 7 = CAN_H and pin 2 = CAN_L.",
            fix="Swap the two lines on the connector (or check the cable and the other end against the "
                "device's datasheet)."))
    return findings


def check_bus_polarity_swapped(snapshot: DesignSnapshot) -> list[Finding]:
    """A net named for one line of a CAN or RS-485 bus that lands on a transceiver pin named for the other."""
    design = _Design(snapshot)
    findings = _dsub_can_findings(design)
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


# --- uart.tx_to_tx ----------------------------------------------------------------------------

_UART_TX = re.compile(r"TXD\d?|TX\d?|U\dTXD|UART\d*_?TXD?|USART\d*_?TX")
_UART_RX = re.compile(r"RXD\d?|RX\d?|U\dRXD|UART\d*_?RXD?|USART\d*_?RX")
_UART_OTHER = re.compile(r"CTS|RTS|DTR|DSR|DCD")
# Transceivers whose TXD/RXD pins are not UART lines, or that translate between lines.
_NOT_UART_PART = re.compile(r"TJA1|MCP25[56]|SN65HVD|TCAN|MAX3051|ISO1050|MAX3485|MAX485|SP3485|THVD|ADM2|"
                            r"MAX232|MAX3232|SP3232|SN75176")
_PASSIVE_KINDS = {"resistor", "capacitor", "testpoint"}


def _uart_dir(pin: Pin) -> str:
    """"TX" or "RX" when the pin name is a UART line of one direction (and nothing else TX/RX-like), else ""."""
    found = set()
    for token in (t for t in re.split(_TOKENS, _name(pin.name)) if t):
        if _UART_OTHER.fullmatch(token):
            return ""
        if _UART_TX.fullmatch(token):
            found.add("TX")
        elif _UART_RX.fullmatch(token):
            found.add("RX")
        elif "TX" in token or "RX" in token:  # TX_EN, TXCAN, RXD_A ...
            return ""
    return found.pop() if len(found) == 1 else ""


def check_uart_tx_to_tx(snapshot: DesignSnapshot) -> list[Finding]:
    """UART TX wired to TX (or RX to RX) of another IC, directly or through one series resistor up to 1 kOhm."""
    design = _Design(snapshot)
    fixed = design.grounds | design.supplies
    groups: list[tuple[tuple[str, ...], Component | None]] = [((net,), None) for net in design.nodes
                                                              if net not in fixed]
    for comp in design.of_kind("resistor"):
        ends, ohms = two_terminal_nets(comp), parse_resistance(comp.value)
        if ends and ends[0] != ends[1] and ohms is not None and ohms <= 1000 and not comp.dnp \
                and not set(ends) & fixed:
            groups.append((ends, comp))
    findings, reported = [], []
    for nets, resistor in groups:
        if any("CAN" in net_label(net) for net in nets):
            continue
        nodes = [node for net in nets for node in design.nodes[net]]
        for direction in ("TX", "RX"):
            pins = [(comp, pin) for comp, pin in nodes
                    if design.kind[comp.reference] in {"ic", "module"} and not comp.dnp
                    and _uart_dir(pin) == direction]
            if len({comp.reference for comp, _ in pins}) < 2 \
                    or any(_NOT_UART_PART.search(comp.value.upper()) for comp, _ in pins):
                continue
            # Two receivers on one transmitter are normal: only call RX-RX when nothing else could drive the net.
            if direction == "RX" and any(
                    design.kind[comp.reference] not in _PASSIVE_KINDS and (comp, pin) not in pins
                    for comp, pin in nodes):
                continue
            key = {(comp.reference, pin.number) for comp, pin in pins}
            if any(key <= done for done in reported):
                continue
            reported.append(key)
            refs = tuple(sorted({comp.reference for comp, _ in pins}))
            shown = ", ".join(f"{comp.reference} {_pin_text(pin)}" for comp, pin in pins)
            via = f" through {resistor.reference}" if resistor else ""
            findings.append(Finding(
                "uart.tx_to_tx", Severity.ERROR,
                f"UART {direction} pins are wired together{via} ({', '.join(refs)})",
                refs=refs, nets=tuple(nets),
                evidence=f"{shown} are all UART {'transmit' if direction == 'TX' else 'receive'} pins on "
                         f"{' / '.join(net_label(n) or n for n in nets)}{via}. "
                         + ("Two transmitters would fight over the line." if direction == "TX"
                            else "Nothing on the net transmits."),
                fix="Cross the UART: connect each chip's TX to the other chip's RX."))
    return findings
