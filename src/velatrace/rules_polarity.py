"""Polarity and topology checks: parts wired backwards across a rail, MOSFETs wired wrong, holes on a rail.

Each rule only fires when the nets are unambiguous: one side clearly ground, the other a clearly
positive supply, or an explicit voltage in a pin name that contradicts an explicit voltage in the net name.
audit_rules helpers are reached through the module (`ar.`) because audit_rules imports this file.
"""
from __future__ import annotations

from decimal import Decimal
import re

from . import audit_rules as ar
from .findings import Finding, Severity
from .models import Component, DesignSnapshot, Pin

# Names that are not a positive supply: negative rails and AC / rectifier nodes.
_NEGATIVE = re.compile(r"-|NEG|VEE|MINUS|VSS|(?<![A-Z])N\d+V")
_AC = re.compile(r"(?<![A-Z])AC(?![A-Z])|(?<![A-Z])AC\d|VAC|BRIDGE|RECT|MAINS")
_CLAMP_PART = re.compile(r"ZENER|(?<![A-Z0-9])ZD(?![A-Z])|(?<![A-Z0-9])Z\d|TVS|ESD|SMAJ|SMBJ|SMCJ|PESD|BZX|BZT|SCHOTTKY",
                         re.IGNORECASE)
_ANODE = re.compile(r"A\d*|ANODE\d*|\+")
_CATHODE = re.compile(r"K\d*|C\d*|CATHODE\d*|-")
_POLAR_CAP = re.compile(r"(?:^|[_:\s])(?:CP\d*|CPOL|C_POL\w*|POLARI[SZ]ED\w*|TANTALUM|C_ELEC\w*|ELEC\w*|ELECTROLYTIC)"
                        r"(?:[_\s\-]|$)", re.IGNORECASE)
_NMOS = re.compile(r"NMOS|N-?CH|N_CH|2N7002|BSS138|BSS123|AO340[02]|IRLML2502|IRLML6344|SI2302|DMN\d|IRLZ\d|IRLB\d",
                   re.IGNORECASE)
_PMOS = re.compile(r"PMOS|P-?CH|P_CH|AO3401|SI2301|SI2305|IRLML6402|IRLML5103|DMP\d|BSS84|FDN340P|FDN360P",
                   re.IGNORECASE)
_MOSFET = re.compile(r"MOS|BSS\d|AO\d{4}|SI\d{4}|DM[NP]\d|IRL?[A-Z]?\d|2N7002|FDN\d|NTR\d|CSD\d|STD\d", re.IGNORECASE)
_NOT_ENHANCEMENT = re.compile(r"JFET|NJF|PJF|(?<![A-Z])J\d{3}|2N54|BF\d{3}|MMBF|DEPLET|BSS126|BSS159|DN\d{4}|LND\d|IXTP",
                              re.IGNORECASE)


def _symbol(comp: Component) -> str:
    return comp.fields.get("lib_id", "").rsplit(":", 1)[-1]


def _text(comp: Component) -> str:
    return f"{_symbol(comp)} {comp.value} {comp.footprint.rsplit(':', 1)[-1]}"


def _positive_rail(net: str) -> bool:
    label = ar.net_label(net)
    return ar.is_supply_name(net) and not _NEGATIVE.search(label) and not _AC.search(label)


def _ac_net(comp: Component) -> bool:
    return any(_AC.search(ar.net_label(pin.net)) for pin in comp.pins if pin.net)


def _nets(pins: list[Pin]) -> set[str]:
    return {pin.net for pin in pins}


def _pin_name(pin: Pin) -> str:
    return pin.name.strip().upper()


def _reversed_finding(comp: Component, what: str, pos: Pin, neg: Pin, ground: str, rail: str) -> Finding:
    return Finding(
        "polarity.reversed", Severity.WARNING,
        f"{comp.reference} looks installed backwards: its {pos.name or 'positive'} pin is on {ground} and its "
        f"{neg.name or 'negative'} pin is on {rail}", (comp.reference,), (ground, rail),
        f"{comp.reference} ({what}) has {_pin_name(pos)} (pin {pos.number}) on {ground} and "
        f"{_pin_name(neg)} (pin {neg.number}) on {rail}, so it is reverse-biased across the supply.",
        f"Swap the connections so the positive pin faces {rail} (or flip the part's symbol/footprint polarity).")


def _led_pins(comp: Component) -> tuple[list[Pin], list[Pin]] | None:
    """(anode pins, cathode pins) when every named pin is an anode or cathode name."""
    anodes, cathodes = [], []
    for pin in comp.pins:
        name = _pin_name(pin)
        if not pin.number:
            continue
        if _ANODE.fullmatch(name):
            anodes.append(pin)
        elif _CATHODE.fullmatch(name):
            cathodes.append(pin)
        else:
            return None
    return (anodes, cathodes) if anodes and cathodes else None


def _cap_pins(comp: Component) -> tuple[Pin, Pin] | None:
    pins = [pin for pin in comp.pins if pin.number]
    if len({pin.number for pin in pins}) != 2:
        return None
    pos = [pin for pin in pins if _pin_name(pin) == "+"]
    neg = [pin for pin in pins if _pin_name(pin) == "-"]
    if pos and neg:
        return pos[0], neg[0]
    # Unnamed pins: KiCad polarized-cap symbols put + on pin 1. A plain C symbol with a polarized footprint
    # says nothing about which pin the designer meant as +, so only a polarized symbol counts.
    symbol = _symbol(comp)
    if all(_pin_name(pin) in {"", "~", pin.number} for pin in pins) and _POLAR_CAP.search(symbol or _text(comp)):
        first = [pin for pin in pins if pin.number == "1"]
        second = [pin for pin in pins if pin.number == "2"]
        if first and second:
            return first[0], second[0]
    return None


def check_polarity_reversed(snapshot: DesignSnapshot) -> list[Finding]:
    """LEDs and polarized capacitors sitting backwards across ground and a positive rail."""
    design = ar._Design(snapshot)
    findings = []
    for comp in snapshot.components:
        if comp.dnp or _ac_net(comp) or ar._PROTECTION_VALUE.search(comp.value) or _CLAMP_PART.search(_text(comp)):
            continue
        kind = design.kind[comp.reference]
        if kind == "led" or (kind == "diode" and _symbol(comp).upper().startswith("LED")):
            if not (found := _led_pins(comp)):
                continue
            anodes, cathodes = found
            if all(ar.is_ground(pin.net) for pin in anodes) and all(_positive_rail(pin.net) for pin in cathodes):
                findings.append(_reversed_finding(comp, "LED", anodes[0], cathodes[0], anodes[0].net, cathodes[0].net))
        elif kind == "capacitor" and _POLAR_CAP.search(_text(comp)):
            if not (found := _cap_pins(comp)):
                continue
            pos, neg = found
            if ar.is_ground(pos.net) and _positive_rail(neg.net):
                findings.append(_reversed_finding(comp, "polarized capacitor", pos, neg, pos.net, neg.net))
    return findings


def _single(pins: list[Pin]) -> str | None:
    nets = _nets(pins)
    return next(iter(nets)) if len(nets) == 1 and "" not in nets else None


def check_mosfet_topology(snapshot: DesignSnapshot) -> list[Finding]:
    """MOSFETs with a shorted gate, an N-channel drain on ground with its source on a rail, or a P-channel source on ground."""
    findings = []
    negative_rail = any(_NEGATIVE.search(ar.net_label(pin.net)) and not ar.is_ground(pin.net)
                        for comp in snapshot.components for pin in comp.pins if pin.net)
    for comp in snapshot.components:
        if comp.dnp:
            continue
        by_name = {letter: [pin for pin in comp.pins if pin.number and re.fullmatch(letter + r"\d*", _pin_name(pin))]
                   for letter in "GSD"}
        text = _text(comp)
        if not all(by_name.values()) or len(comp.pins) > 8 or _NOT_ENHANCEMENT.search(text):
            continue
        n_ch, p_ch = bool(_NMOS.search(text)), bool(_PMOS.search(text))
        if not ((n_ch or p_ch or _MOSFET.search(text)) and (comp.reference[:1].upper() in "QM" or n_ch or p_ch)):
            continue
        gate, source, drain = (_single(by_name[letter]) for letter in "GSD")
        if not (gate and source and drain):
            continue
        ref, nets = comp.reference, (gate, source, drain)
        if gate in (source, drain):
            other, name = (source, "source") if gate == source else (drain, "drain")
            findings.append(Finding(
                "polarity.mosfet_topology", Severity.WARNING,
                f"{ref} has its gate tied to its {name} on {gate}", (ref,), (gate,),
                f"{ref} gate (pin {by_name['G'][0].number}) and {name} are both on {other}, so the transistor cannot be switched.",
                f"Give the gate its own control net, or remove {ref} if the tie is intended."))
        elif n_ch and not p_ch and ar.is_ground(drain) and _positive_rail(source):
            findings.append(Finding(
                "polarity.mosfet_topology", Severity.WARNING,
                f"{ref} (N-channel) has its drain on {drain} and its source on {source}", (ref,), (drain, source),
                f"{ref} drain is on ground and source on {source}; its body diode conducts from the rail to ground "
                "and the gate can never turn it on correctly.",
                "Swap drain and source (N-channel: drain toward the rail or load, source toward ground), or check the symbol pinout."))
        elif p_ch and not n_ch and ar.is_ground(source) and not ar.is_ground(drain) and (_positive_rail(drain) or not negative_rail):
            findings.append(Finding(
                "polarity.mosfet_topology", Severity.WARNING,
                f"{ref} (P-channel) has its source on ground ({source})", (ref,), (source, drain),
                f"{ref} source is on {source} and drain on {drain}; a P-channel switch needs its source on the supply side, "
                "and with the source at ground its gate would have to go below ground to turn it on.",
                "Swap source and drain so the source is on the supply, or use an N-channel part for a low-side switch."))
    return findings


def check_mounting_hole_rail(snapshot: DesignSnapshot) -> list[Finding]:
    """Mounting-hole pads wired to a supply rail instead of ground."""
    design = ar._Design(snapshot)
    findings = []
    for comp in snapshot.components:
        if comp.dnp or design.kind[comp.reference] != "mounting" or "HOLE" not in comp.footprint.upper():
            continue
        for net in sorted({pin.net for pin in comp.pins if pin.net and ar.is_supply_name(pin.net)}):
            findings.append(Finding(
                "power.mounting_hole_rail", Severity.WARNING,
                f"{comp.reference}, a mounting hole, is connected to {net}", (comp.reference,), (net,),
                f"{comp.reference} ({comp.footprint.rsplit(':', 1)[-1]}) has a pad on {net}; a metal screw or standoff "
                "will short that rail to the chassis or whatever it touches.",
                "Leave the hole pad unconnected, or tie it to GND/chassis."))
    return findings


def _volts(text: str) -> set[Decimal]:
    text = text.upper()
    found = {Decimal(f"{a}.{b}") for a, b in re.findall(r"(?<![\d.])(\d+)V(\d+)", text)}
    found |= {Decimal(a) for a in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)V(?![A-Z\d])", text)}
    found |= {Decimal(f"{a}.{b}") for a, b in re.findall(r"V(?:DD|CC)A?(\d)P(\d)", text)}
    return found


def _pin_volts(comp: Component, pin: Pin) -> set[Decimal]:
    name = pin.name.strip().upper()
    if name.startswith("-") or "TOL" in name or set(re.split(r"[^A-Z0-9]+", name)) & ar._SIGNAL_TOKENS:
        return set()
    if volts := _volts(name):
        return volts
    # VCC5 only counts when the part has no VCC1, VCC2 ... index pins.
    if name == "VCC5" and not any(re.fullmatch(r"VCC\d", other.name.strip().upper()) and other.name != pin.name
                                  for other in comp.pins):
        return {Decimal(5)}
    return set()


def _net_volts(net: str) -> set[Decimal]:
    label = ar.net_label(net)
    if not label or label.startswith("-") or not ar.is_supply_name(net):
        return set()
    volts = _volts(label)
    if not volts and label in {"VBUS", "+VBUS", "VUSB", "+VUSB"}:
        volts = {Decimal(5)}
    return volts


def _regulated_volts(snapshot: DesignSnapshot, net: str) -> Decimal | None:
    """The output voltage of a known fixed regulator whose output pin is on the net: the rail's real
    voltage, whatever its label says (a 3.3 V LDO feeding a net labelled +3V0)."""
    from .rules_tables import _LDO_OUT, _ldo_of
    for comp in snapshot.components:
        if any(pin.net == net and _LDO_OUT.fullmatch(re.sub(r"[^A-Z]", "", pin.name.upper())) for pin in comp.pins):
            found = _ldo_of(comp)
            if found:
                return Decimal(str(found[0]))
    return None


def check_rail_voltage_mismatch(snapshot: DesignSnapshot) -> list[Finding]:
    """A pin named for one voltage (3V3, VDD3P3, 1V8 ...) sitting on a net named for a different one."""
    findings = []
    for comp in snapshot.components:
        if comp.dnp:
            continue
        seen: dict[tuple[str, Decimal], list[Pin]] = {}
        for pin in comp.pins:
            if not pin.net or not pin.number:
                continue
            want, have = _pin_volts(comp, pin), _net_volts(pin.net)
            if len(want) == 1 and len(have) == 1 and want != have and _regulated_volts(snapshot, pin.net) not in want:
                seen.setdefault((pin.net, next(iter(want))), []).append(pin)
        for (net, volts), pins in seen.items():
            names = ", ".join(sorted({pin.name for pin in pins}))
            findings.append(Finding(
                "power.rail_voltage_mismatch", Severity.WARNING,
                f"{comp.reference} pin {names} is on {net}, a different voltage", (comp.reference,), (net,),
                f"{comp.reference} {names} (pin {', '.join(pin.number for pin in pins)}) names {volts:g} V "
                f"but its net {net} names {next(iter(_net_volts(net))):g} V.",
                f"Connect it to the {volts:g} V rail, or check the pin name and the net label."))
    return findings
