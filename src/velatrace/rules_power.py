"""Power-section audit rules: board-breaking mistakes around regulators and internal references.

  power.vcap_missing          internal-regulator pin (VCAP, DEC, REGOUT ...) with no capacitor to ground
  power.reg_out_cap_missing   linear regulator whose output net has no capacitor to ground
  power.bootstrap_cap_missing buck IC bootstrap pin (BST/BOOT) with no capacitor to the switch node
  power.exposed_pad_floating  IC exposed pad named as ground that is open or on a non-ground net

Like the rest of audit_rules these stay quiet whenever the netlist cannot show the problem clearly.
A capacitor counts when it sits on the net or behind a small series part (ferrite, inductor,
jumper, resistor up to 1 kOhm) from it, and a part marked DNP still counts: the designer placed it.
"""
from __future__ import annotations

import re

from .audit_rules import (Check, _Design, _base_type, _is_return, _pin_text, parse_resistance, terminals,
                          two_terminal_nets)
from .findings import Finding, Severity
from .models import Component, DesignSnapshot, Pin

_SERIES_KINDS = {"inductor", "ferrite", "jumper", "resistor"}
_SERIES_MAX_OHMS = 1000
_CAP_VALUE = re.compile(r"\d+(?:[.,]\d+)?\s*[pnuµμ]F", re.IGNORECASE)


def _name(pin: Pin) -> str:
    """Pin name upper-cased with KiCad overbar markup (~{RST}, ~RST) and spaces removed."""
    return re.sub(r"[~{}\\\s]", "", pin.name.upper())


_GROUND_PIN = re.compile(r"[ADPSC]?GND\d*|VSS[A-Z]?\d*")


def _power_design(snapshot: DesignSnapshot, by_pins: bool = True) -> _Design:
    """A _Design whose ground set also holds nets named with a GND/VSS token (/IMU/Q_GND) and, with
    by_pins, nets wired to a pin named GND: a capacitor returned to those still decouples."""
    design = _Design(snapshot)
    design.grounds |= {net for net in design.nodes
                       if re.search(r"GND|VSS", net.rsplit("/", 1)[-1].upper())
                       or by_pins and any(_GROUND_PIN.fullmatch(re.sub(r"[~{}\s]", "", pin.name.upper()))
                                          for _, pin in design.nodes[net])}
    return design


def _is_cap(design: _Design, comp: Component) -> bool:
    return design.kind[comp.reference] == "capacitor" or (
        design.kind[comp.reference] == "other" and bool(_CAP_VALUE.fullmatch(comp.value.strip()))
        and two_terminal_nets(comp) is not None)


def _group(design: _Design, net: str) -> set[str]:
    """The net plus nets reached through series ferrites, inductors, jumpers and small resistors."""
    seen, todo = {net}, [net]
    while todo:
        here = todo.pop()
        if here in design.grounds:
            continue
        for comp, _ in design.nodes.get(here, ()):
            if design.kind[comp.reference] not in _SERIES_KINDS:
                continue
            ends = two_terminal_nets(comp)
            if ends is None or here not in ends or ends[0] == ends[1]:
                continue
            if design.kind[comp.reference] == "resistor":
                ohms = parse_resistance(comp.value)
                if ohms is not None and ohms > _SERIES_MAX_OHMS:
                    continue
            other = ends[1] if ends[0] == here else ends[0]
            if other not in seen:
                seen.add(other)
                todo.append(other)
    return seen


def _caps_to_ground(design: _Design, nets: set[str]) -> list[Component]:
    found = {}
    for net in nets:
        for comp, _ in design.nodes.get(net, ()):
            ends = two_terminal_nets(comp)
            if ends and _is_cap(design, comp) and any(end in design.grounds for end in ends) \
                    and any(end in nets for end in ends):
                found[comp.reference] = comp
    return list(found.values())


def _alone(design: _Design, pin: Pin, ic: Component) -> bool:
    """Nothing else is wired to the pin's net."""
    return not [1 for comp, other in design.nodes.get(pin.net, ()) if not (comp is ic and other is pin)]


def _names(pins: list[Pin]) -> str:
    return ", ".join(sorted({pin.name or pin.number for pin in pins}))


# --- power.vcap_missing -----------------------------------------------------------------------

_VCAP = re.compile(r"VCAP\d*|VCAP_?\d|VREG_?VOUT|VREG_OUT|DEC\d*|REGOUT\d*|DCC|BYP(?:ASS)?|VDDCORE|VCORE|LDO_?OUT")
# Optional or chip-variant pins (nRF DCC/DEC, LDO bypass): leaving them open is often allowed, so only judge them once wired.
_VCAP_OPEN_OK = re.compile(r"DCC|DEC\d*|BYP(?:ASS)?")


def check_vcap_missing(snapshot: DesignSnapshot) -> list[Finding]:
    """Internal-regulator or reference pins (VCAP, DEC, REGOUT ...) that need a capacitor to ground."""
    design = _power_design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        if ic.dnp:
            continue
        bad = []
        for pin in ic.pins:
            name = _name(pin)
            if not _VCAP.fullmatch(name) or pin.no_connect or pin.net in design.grounds:
                continue
            if _VCAP_OPEN_OK.fullmatch(name) and _alone(design, pin, ic):
                continue
            if not _caps_to_ground(design, _group(design, pin.net)):
                bad.append(pin)
        if not bad:
            continue
        nets = tuple(dict.fromkeys(pin.net for pin in bad))
        findings.append(Finding(
            "power.vcap_missing", Severity.WARNING,
            f"{ic.reference} has no capacitor on its internal-regulator pin{'s' if len(bad) > 1 else ''} ({_names(bad)})",
            (ic.reference,), nets,
            f"{ic.reference} {', '.join(_pin_text(pin) for pin in bad)} is the chip's own regulator or reference "
            f"output, and no capacitor connects its net to ground.",
            "Add the capacitor the datasheet asks for (often 1 uF to 4.7 uF, low-ESR) from each of these pins "
            "to ground, close to the pin."))
    return findings


# --- power.reg_out_cap_missing ----------------------------------------------------------------

_LDO_FAMILY = re.compile(
    r"(?<!\d)(?:78|79)[LMS]?\d\d(?!\d)|1117|LM317|AP2112|AP2210|MCP170[02]|MCP1825|XC62(?:06|20)|RT9013|ME6211|"
    r"HT73\d\d|TLV7\d\d(?!\d)|LP2985|AP7333|LP5907|LP298\d|TPS7A\d|TPS73\d|TPS709|MIC5219|MIC5205|RT9193|"
    r"AP2127|SPX3819")
# Switching or isolated converters, charge pumps and modules carry their own output capacitor or are out of scope.
_NOT_LINEAR = re.compile(r"R-?78|TSR[ -]?\d|CONVERTER|DC-?DC|MODULE|BUCK|BOOST|CHARGE.?PUMP|ISOLAT")
# Parts with IN/OUT pins that are not regulators: switches, ideal diodes, references, chargers, amplifiers.
_NOT_REGULATOR = re.compile(
    r"SWITCH|EFUSE|E-FUSE|HOT.?SWAP|IDEAL|OR-?ING|MUX|LIMIT|REF\d|\bREF|ADR\d|LT66|CHARGER|AMP|OPA\d|"
    r"TPS2\d{3}|TPS3|LTC44|LM66|MIC20\d\d|STMPS|AP2[1-5]\d\d|AP73\d{2}S|TCK\d|NCP3\d\d|MIC94\d\d|ISO|MHZ|KHZ|XTAL|OSC|CRYSTAL")
_REG_IN = re.compile(r"V?IN|VI")
_REG_OUT = re.compile(r"V?OUT|VO")
_REG_OUT_FAMILY = re.compile(r"V?OUT\d?|VO\d?")
_NOT_LDO_PIN = re.compile(
    r"SW\d?|LX\d?|PH|BST|BOOT|FLG|FLAG|FAULT|N?FLT|ILIM|ISET|OCB?|SLEW|CT|DISCH|STAT|STATUS|LIM|GATE|VBAT|BAT|VBUS|"
    r"C\d?[+-]|CFLY|[+-]IN|IN[+-]|IN[AB]|IN[12]|OUT[+-]|SYS|CHG|PROG|TS|ISNS")
_MAX_PAIR_PINS = 8


def _regulator_outputs(design: _Design, ic: Component) -> list[Pin]:
    """Output pins of a linear regulator, or [] when the part is not clearly one."""
    text = f"{ic.value} {ic.footprint.split(':', 1)[-1]}".upper()
    if _NOT_LINEAR.search(text):
        return []
    names = {_name(pin) for pin in ic.pins}
    if any(_NOT_LDO_PIN.fullmatch(name) for name in names):
        return []
    typed_out = False
    if _LDO_FAMILY.search(ic.value.upper()):
        wanted = _REG_OUT_FAMILY
    elif (not _NOT_REGULATOR.search(text) and len(terminals(ic)) <= _MAX_PAIR_PINS
          and any(_REG_IN.fullmatch(name) for name in names)):
        wanted = _REG_OUT
        typed_out = True  # no known family: trust only an output the symbol types as a power output
    else:
        return []
    return [pin for pin in ic.pins if wanted.fullmatch(_name(pin)) and pin.net and not pin.no_connect
            and (_base_type(pin) == "power_out" if typed_out else _base_type(pin) not in {"input", "bidirectional"})]


def check_reg_out_cap_missing(snapshot: DesignSnapshot) -> list[Finding]:
    """Linear regulators whose output net has no capacitor to ground anywhere."""
    design = _power_design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        if ic.dnp:
            continue
        bare = {}
        for pin in _regulator_outputs(design, ic):
            if pin.net in design.grounds or _alone(design, pin, ic):
                continue  # grounded, or not wired to anything: not a powered output
            if not _caps_to_ground(design, _group(design, pin.net)):
                bare.setdefault(pin.net, pin)
        for net, pin in bare.items():
            findings.append(Finding(
                "power.reg_out_cap_missing", Severity.WARNING,
                f"Regulator {ic.reference} has no capacitor on its output {net}",
                (ic.reference,), (net,),
                f"{ic.reference} {_pin_text(pin)} is the regulator output, and no capacitor connects {net} to ground.",
                "Add the output capacitor the datasheet requires (typically 1 uF to 22 uF, mind the allowed ESR) "
                "from the output to ground, right at the regulator; many linear regulators oscillate without it."))
    return findings


# --- power.bootstrap_cap_missing --------------------------------------------------------------

_BOOT_PIN = re.compile(r"BST\d?|BOOT|BOOTSTRAP|CB|BS|BTST|BOOTCAP")
_SWITCH_PIN = re.compile(r"SW\d?|LX\d?|PH|PHASE\d?|SWITCH\d?|SW_?[A-D]")


def check_bootstrap_cap_missing(snapshot: DesignSnapshot) -> list[Finding]:
    """Buck-converter bootstrap pins (BST, BOOT ...) with no capacitor to the switch node."""
    design = _power_design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        if ic.dnp:
            continue
        switch_nets = {pin.net for pin in ic.pins
                       if _SWITCH_PIN.fullmatch(_name(pin)) and pin.net and not _alone(design, pin, ic)
                       and pin.net not in design.grounds}
        if not switch_nets:
            continue
        sw_group = set().union(*(_group(design, net) for net in switch_nets))
        bad = []
        for pin in ic.pins:
            if not _BOOT_PIN.fullmatch(_name(pin)) or pin.no_connect or pin.net in design.grounds:
                continue
            boot_group = _group(design, pin.net)
            if not any(_is_cap(design, comp) and (ends := two_terminal_nets(comp))
                       and (ends[0] in boot_group and ends[1] in sw_group
                            or ends[1] in boot_group and ends[0] in sw_group)
                       for net in boot_group for comp, _ in design.nodes.get(net, ())):
                bad.append(pin)
        if not bad:
            continue
        findings.append(Finding(
            "power.bootstrap_cap_missing", Severity.WARNING,
            f"{ic.reference} has no bootstrap capacitor on {_names(bad)}",
            (ic.reference,), tuple(dict.fromkeys(pin.net for pin in bad)),
            f"{ic.reference} {', '.join(_pin_text(pin) for pin in bad)} is a bootstrap pin, and no capacitor connects "
            f"it to the switch-node net ({', '.join(sorted(switch_nets))}).",
            "Place the bootstrap capacitor the datasheet gives (usually 100 nF) between the bootstrap pin and "
            "the switch pin; without it the high-side switch never turns on."))
    return findings


# --- power.exposed_pad_floating ---------------------------------------------------------------

_EP_NEUTRAL = re.compile(r"EP\d?|EPAD\d?|PAD\d?|EXP(?:OSED)?_?PAD|THERMAL(?:_?PAD)?|TPAD")
_EP_GROUND = re.compile(r"(?:GND|VSS|PGND|AGND|DGND)_?(?:EP|PAD|EPAD|THERMAL)|(?:EP|PAD|EPAD|THERMAL)_?(?:GND|VSS)|THERMAL_?GND")
_PLAIN_GROUND = re.compile(r"[ADP]?GND|VSS|GND_?\d*")
_EP_FOOTPRINT = re.compile(r"(?<![A-Z])\d?EP(?![A-Z])|THERMAL|EXPOSED|POWERPAD|[-_]PAD|(?<![A-Z])TAB(?![A-Z])")


def _exposed_pads(ic: Component) -> list[tuple[Pin, bool]]:
    """(pin, named_ground) for each exposed/thermal pad the symbol shows."""
    pads = [(pin, bool(_EP_GROUND.fullmatch(_name(pin)))) for pin in ic.pins
            if _EP_NEUTRAL.fullmatch(_name(pin)) or _EP_GROUND.fullmatch(_name(pin))]
    if pads:
        return pads
    # A plain GND pad numbered after all the others (pad N+1 of an N-pin QFN) on an exposed-pad footprint.
    numbers = {pin.number for pin in ic.pins if pin.number.isdigit()}
    if not numbers or len(numbers) != len(terminals(ic)) or not _EP_FOOTPRINT.search(ic.footprint.split(":", 1)[-1].upper()):
        return []
    last = max(numbers, key=int)
    if int(last) != len(numbers) or len(numbers) < 5:  # contiguous 1..N, so the last pad is the extra one
        return []
    return [(pin, True) for pin in ic.pins if pin.number == last and _PLAIN_GROUND.fullmatch(_name(pin))]


def check_exposed_pad_floating(snapshot: DesignSnapshot) -> list[Finding]:
    """Exposed pads the symbol calls a ground/thermal pad that are open or not on ground."""
    design = _power_design(snapshot, by_pins=False)
    findings = []
    for ic in design.of_kind("ic"):
        if ic.dnp:
            continue
        open_pads, wrong = [], []
        for pin, named_ground in _exposed_pads(ic):
            if pin.no_connect:
                continue
            if not pin.net or _alone(design, pin, ic):
                open_pads.append(pin)
            elif named_ground and not _is_return(design, pin.net):
                wrong.append(pin)
        for pins, why, fix in (
                (open_pads, "has no connection",
                 "Connect the exposed pad to the ground plane (with thermal vias), or add a no-connect flag if "
                 "the datasheet says to leave it open."),
                (wrong, "is on a net that is not ground",
                 "Connect the pad to ground, as the symbol's pin name says.")):
            if pins:
                findings.append(Finding(
                    "power.exposed_pad_floating", Severity.WARNING,
                    f"{ic.reference} exposed pad {_names(pins)} {why}",
                    (ic.reference,), tuple(dict.fromkeys(pin.net for pin in pins if pin.net)),
                    f"{ic.reference} {', '.join(_pin_text(pin) for pin in pins)} is the exposed/thermal pad and "
                    f"{why}" + (f" ({', '.join(sorted({pin.net for pin in pins}))})." if wrong is pins else "."),
                    fix))
    return findings


POWER_RULES: dict[str, Check] = {
    "power.vcap_missing": check_vcap_missing,
    "power.reg_out_cap_missing": check_reg_out_cap_missing,
    "power.bootstrap_cap_missing": check_bootstrap_cap_missing,
    "power.exposed_pad_floating": check_exposed_pad_floating,
}
