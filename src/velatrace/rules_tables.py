"""Table-driven audit rules: facts that come straight from a datasheet, checked against the netlist.

  power.divider_voltage   adjustable regulator whose FB/ADJ divider gives a voltage that contradicts the output net name
  mcu.strap_pins          ESP32 / RP2040 boot-strap pins wired in a way the datasheet says breaks boot
  power.ldo_dropout       fixed LDO whose input voltage leaves too little headroom, or exceeds its maximum input

Each rule has a small table below and every value in it cites its datasheet. A part that is not in a
table is never judged. The rules stay quiet whenever the netlist cannot show the problem clearly: a
net name without a voltage, extra parts on the feedback node, an unreadable resistor value, an input
voltage that is not stated.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .audit_rules import _Design, _pin_text, is_supply_name, net_label, parse_resistance, two_terminal_nets
from .bom import mpn_of
from .findings import Finding, Severity
from .models import Component, DesignSnapshot, Pin
from .rules_polarity import _volts
from .rules_power import _power_design

TOLERANCE = 0.05  # report when the divider voltage and the net name are more than 5 % apart


def _text(comp: Component) -> str:
    return f"{comp.value} {mpn_of(comp) or ''}".upper()


def _pin_name(pin: Pin) -> str:
    return re.sub(r"[~{}\s]", "", pin.name.upper())


def _stated_volts(net: str) -> float | None:
    """The one voltage a supply net's name states (3V3, +5V, 1V8, 12V_IN), else None."""
    label = net_label(net)
    if not label or label.startswith("-") or not is_supply_name(net):
        return None
    volts = _volts(label)
    return float(next(iter(volts))) if len(volts) == 1 else None


def _parts_on(design: _Design, net: str, skip: Component) -> dict[str, Component]:
    return {comp.reference: comp for comp, _ in design.nodes.get(net, ()) if comp is not skip}


# --- power.divider_voltage --------------------------------------------------------------------

@dataclass(frozen=True)
class _Adjustable:
    family: re.Pattern   # matched against value + MPN
    vref: float          # volts at the feedback / adjust pin
    fb_pin: re.Pattern   # name of that pin
    iadj_max: float = 0  # amps flowing out of the adjust pin and through the bottom resistor; non-zero marks an LDO-style ADJ pin,
                         # where VREF sits across the top resistor (OUT to ADJ) and VOUT = VREF (1 + Rbottom / Rtop) + IADJ Rbottom


_ADJUSTABLE = (
    # LM317, TI SLVS044Z (Apr 2025), section 6.5: VREF 1.25 V typ (1.2 to 1.3 V), VOUT = VREF (1 + R2/R1) + IADJ R2, IADJ 50 uA typ, 100 uA max.
    _Adjustable(re.compile(r"(?<![A-Z0-9])LM317(?:[TSKH])?(?![A-Z0-9])"), 1.25, re.compile(r"ADJ(?:UST)?"), 100e-6),
    # AMS1117-ADJ, Advanced Monolithic Systems AMS1117 datasheet: reference voltage 1.250 V typ (1.238 to 1.262 V), VOUT = VREF (1 + R2/R1) + IADJ R2.
    # IADJ is 5 uA max there; clone datasheets say 50 uA typ / 120 uA max, so 120 uA is allowed for.
    _Adjustable(re.compile(r"AMS1117[-_ ]?ADJ"), 1.25, re.compile(r"ADJ"), 120e-6),
    # LM2596-ADJ, TI SNVS124G (Mar 2023), section 7.8: VFB 1.23 V typ at 4.5 V <= VIN <= 40 V, feedback bias current 10 nA typ (negligible).
    _Adjustable(re.compile(r"LM2596[A-Z]?[-_ ]?ADJ"), 1.23, re.compile(r"FB|FEEDBACK")),
    # TPS5430 / TPS5431, TI SLVS632L (Apr 2026), section 6.5: VFB 1.221 V typ at 25 C, pin 4 VSENSE "connect to output voltage divider".
    _Adjustable(re.compile(r"(?<![A-Z0-9])TPS543[01](?![0-9])"), 1.221, re.compile(r"VSENSE")),
    # MP1584, Monolithic Power Systems MP1584 Rev. 1.0 (8/8/2011), electrical characteristics: VFB 0.8 V typ (0.776 to 0.824 V), pin FB.
    _Adjustable(re.compile(r"(?<![A-Z0-9])MP1584"), 0.8, re.compile(r"FB")),
)


def _divider(design: _Design, ic: Component, fb: Pin):
    """(top resistor, bottom resistor, top ohms, bottom ohms, output net) of a plain two-resistor divider on the pin's net, else None."""
    resistors = []
    for ref, comp in _parts_on(design, fb.net, ic).items():
        kind = design.kind[ref]
        if kind == "capacitor" or (kind == "testpoint"):
            continue  # feed-forward capacitor across the top resistor, or a probe
        if kind != "resistor" or comp.dnp:
            return None  # trim pot, diode, a part marked not fitted, anything but two plain resistors
        resistors.append(comp)
    if len(resistors) != 2:
        return None
    top = bottom = None
    for comp in resistors:
        ends = two_terminal_nets(comp)
        if ends is None or ends[0] == ends[1] or fb.net not in ends:
            return None
        other = ends[1] if ends[0] == fb.net else ends[0]
        if other in design.grounds:
            bottom = comp if bottom is None else False
        else:
            top = (comp, other) if top is None else False
    if not bottom or not top:
        return None
    r_top, r_bottom = parse_resistance(top[0].value), parse_resistance(bottom.value)
    if not r_top or not r_bottom:
        return None
    return top[0], bottom, float(r_top), float(r_bottom), top[1]


def check_divider_voltage(snapshot: DesignSnapshot) -> list[Finding]:
    """Adjustable regulators whose feedback divider contradicts the voltage in the output net's name."""
    design = _power_design(snapshot)
    findings = []
    for ic in design.of_kind("ic"):
        entry = next((e for e in _ADJUSTABLE if not ic.dnp and e.family.search(_text(ic))), None)
        if entry is None:
            continue
        pins = [pin for pin in ic.pins if entry.fb_pin.fullmatch(_pin_name(pin)) and pin.net and not pin.no_connect]
        if len({pin.net for pin in pins}) != 1 or pins[0].net in design.grounds:
            continue
        found = _divider(design, ic, pins[0])
        if found is None:
            continue
        r_top, r_bottom, ohms_top, ohms_bottom, out = found
        stated = _stated_volts(out)
        if stated is None:
            continue
        ratio = ohms_bottom / ohms_top if entry.iadj_max else ohms_top / ohms_bottom
        low = entry.vref * (1 + ratio)
        high = low + entry.iadj_max * ohms_bottom
        if low * (1 - TOLERANCE) <= stated <= high * (1 + TOLERANCE):
            continue
        shown = f"{low:.3g}" if high < low * 1.01 else f"{low:.3g} to {high:.3g}"
        findings.append(Finding(
            "power.divider_voltage", Severity.WARNING,
            f"{ic.reference} feedback divider sets about {shown} V, but {out} is named {stated:g} V",
            (ic.reference, r_top.reference, r_bottom.reference), (out, pins[0].net),
            f"{ic.reference} {_pin_text(pins[0])} sits on the divider {r_top.reference} "
            f"({r_top.value}) from {out} and {r_bottom.reference} ({r_bottom.value}) to ground, so with its {entry.vref:g} V reference the output settles "
            f"near {entry.vref:g} V x (1 + {ratio:g}) = {shown} V, not the {stated:g} V in the net name.",
            f"Change the divider so the output is {stated:g} V ({'R_bottom / R_top' if entry.iadj_max else 'R_top / R_bottom'} must be {stated / entry.vref - 1:.3g}), "
            f"or fix the net name if {shown} V is intended."))
    return findings


# --- power.ldo_dropout ------------------------------------------------------------------------

@dataclass(frozen=True)
class _Ldo:
    family: re.Pattern    # group 1 (and 2) carry the output voltage code in the part number
    vout: object          # (match) -> volts, or None when the code is not a known option
    dropout: object       # (volts out) -> dropout volts, or None when the datasheet value is not used
    vin_max: float        # highest input the datasheet allows


def _mcp1700(m): return int(m.group(1)) / 10          # MCP1700T-3302E: 33 = 3.3 V, 02 = +-2 % tolerance
def _xc6206(m): return int(m.group(1)) / 10           # XC6206P332MR: 33 = 3.3 V, last digit = accuracy
def _ht73(m): return {"18": 1.8, "25": 2.5, "27": 2.7, "30": 3.0, "33": 3.3, "35": 3.5, "41": 4.15, "50": 5.0}[m.group(1)]


_LDOS = (
    # AMS1117 fixed, Advanced Monolithic Systems AMS1117 datasheet: options 1.2/1.5/1.8/2.5/3.3/5.0 V; "operates down to 1 V dropout"
    # (1.1 V typ, 1.3 V max at 0.8 A, lower at light load, so 1.0 V is used); absolute maximum input 15 V.
    _Ldo(re.compile(r"AMS1117[A-Z]?[-_ ]?(\d)[.V](\d{1,2})(?!\d)"),
         lambda m: float(f"{m.group(1)}.{m.group(2)}") if f"{m.group(1)}.{m.group(2)}" in {"1.2", "1.5", "1.8", "2.5", "3.3", "5.0"} else None,
         lambda vout: 1.0, 15.0),
    # MCP1700, Microchip DS21826B (2007): VIN-VOUT dropout 178 mV typ at 250 mA for VR >= 2.5 V, 150 mV typ at 200 mA below 2.5 V;
    # input operating range 2.3 V to 6.0 V. Ordering code: 1202/1802/2502/3002/3302/5002.
    _Ldo(re.compile(r"MCP1700T?[-_ ]?(\d\d)02"),
         lambda m: _mcp1700(m) if m.group(1) in {"12", "18", "25", "30", "33", "50"} else None,
         lambda vout: 0.178 if vout >= 2.5 else 0.150, 6.0),
    # XC6206, Torex ETR0305_006: "Maximum Operating Voltage: 6.0 V". The dropout chart is not read here, so only the maximum is checked.
    _Ldo(re.compile(r"XC6206[A-Z](\d\d)\d"), lambda m: _xc6206(m) if 12 <= int(m.group(1)) <= 50 else None,
         lambda vout: None, 6.0),
    # AP2112, Diodes Inc. AP2112 (Jun 2017): "Low Dropout Voltage (3.3V): 250 mV (Typ.) at IOUT = 600 mA"; recommended VIN 2.5 V to 6.0 V.
    # The 250 mV applies to the 3.3 V type only; other options are checked for the maximum input alone.
    _Ldo(re.compile(r"AP2112[KMR]\d?A?[-_ ]?(1\.2|1\.8|2\.5|2\.6|3\.3)"), lambda m: float(m.group(1)),
         lambda vout: 0.25 if vout == 3.3 else None, 6.0),
    # HT7333 (Holtek HT73xx Rev. 1.50, Jun 2016): 3.3 V type, dropout 90 mV typ at 40 mA, input voltage up to 12 V.
    # The -1/-2 suffix parts are Holtek's 30 V family and are left out.
    _Ldo(re.compile(r"HT7333(?![0-9]|[-_ ]?[12](?![0-9]))"), lambda m: 3.3, lambda vout: 0.09, 12.0),
    # LD1117, STMicroelectronics LD1117 datasheet: fixed 1.2/1.8/2.5/3.3/5.0 V (LD1117S33 = 3.3 V); dropout 1 V typ at
    # 800 mA (1.1 V at full load, lower at light load, so 1.0 V is used); absolute maximum input 15 V.
    _Ldo(re.compile(r"LD1117[A-Z]{1,3}(12|18|25|33|50)(?!\d)"), lambda m: int(m.group(1)) / 10, lambda vout: 1.0, 15.0),
)
_LDO_IN = re.compile(r"V?IN|VI")
_LDO_OUT = re.compile(r"V?OUT|VO")


def _ldo_of(ic: Component) -> tuple[float, float | None, float] | None:
    """(output volts, dropout volts or None, maximum input volts) from the part number, or None."""
    text = _text(ic)
    for entry in _LDOS:
        match = entry.family.search(text)
        if match:
            vout = entry.vout(match)
            return None if vout is None else (vout, entry.dropout(vout), entry.vin_max)
    return None


def _single_net(ic: Component, wanted: re.Pattern) -> str | None:
    nets = {pin.net for pin in ic.pins if wanted.fullmatch(_pin_name(pin)) and pin.net and not pin.no_connect}
    return next(iter(nets)) if len(nets) == 1 else None


def check_ldo_dropout(snapshot: DesignSnapshot) -> list[Finding]:
    """Fixed LDOs with too little input-to-output headroom, or an input above the datasheet maximum."""
    design = _power_design(snapshot)
    known = {}
    for ic in design.of_kind("ic", "other"):
        spec = None if ic.dnp else _ldo_of(ic)
        if spec and (vin := _single_net(ic, _LDO_IN)) and (out := _single_net(ic, _LDO_OUT)) and vin != out:
            known[ic.reference] = (ic, spec, vin, out)
    findings = []
    for ic, (vout, dropout, vin_max), in_net, out_net in known.values():
        vin = _stated_volts(in_net)
        if vin is None:  # not named: take the output of the one table LDO that feeds the net
            feeders = {other[1][0] for other in known.values() if other[3] == in_net and other[0] is not ic}
            vin = next(iter(feeders)) if len(feeders) == 1 else None
        if vin is None:
            continue
        if dropout is not None and vin - vout < dropout - 1e-9:
            findings.append(Finding(
                "power.ldo_dropout", Severity.WARNING,
                f"{ic.reference} input {in_net} ({vin:g} V) is too close to its {vout:g} V output",
                (ic.reference,), (in_net, out_net),
                f"{ic.reference} ({ic.value}) needs the input at least {dropout * 1000:g} mV above the output, but "
                f"{in_net} is {vin:g} V and the output is {vout:g} V, only {max(vin - vout, 0) * 1000:g} mV of headroom.",
                "Feed it from a higher rail, or choose a regulator with lower dropout."))
        if vin > vin_max + 1e-9:
            findings.append(Finding(
                "power.ldo_dropout", Severity.WARNING,
                f"{ic.reference} input {in_net} ({vin:g} V) is above its {vin_max:g} V maximum",
                (ic.reference,), (in_net, out_net),
                f"{ic.reference} ({ic.value}) allows at most {vin_max:g} V at the input, and {in_net} is {vin:g} V.",
                "Step the rail down first (buck converter) or choose a regulator rated for this input."))
    return findings


# --- mcu.strap_pins ---------------------------------------------------------------------------

# ESP32 (original), Espressif ESP32-WROOM-32 datasheet v3.8, section 2 note 2: pins SCK/CLK, SDO/SD0, SDI/SD1, SHD/SD2, SWP/SD3 and
# SCS/CMD (GPIO6 to GPIO11 of the chip) are wired to the module's SPI flash and are not recommended for other uses.
# Chip pin names SD_CLK, SD_CMD, SD_DATA_0..3 (GPIO6 to GPIO11): ESP32 Series Datasheet v5.3, pin definitions.
_ESP32 = re.compile(r"ESP32[-_ ]?(?:WROOM[-_ ]?32|D0WD|S0WD|D2WD|U4WD)")
_ESP32_FLASH = re.compile(r"SD[0-3]|CMD|CLK|SHD|SWP|SCS|SCK|SDO|SDI|SD_?CLK|SD_?CMD|SD_?DATA_?[0-3]|(?:GPIO|IO)(?:[6-9]|1[01])")
# GPIO0 low at reset selects download boot (Table 5 "Chip Boot Mode Control", ESP32-WROOM-32 datasheet v3.8), so tied to ground it never runs the app.
_ESP32_GPIO0 = re.compile(r"(?:GPIO|IO)0")
# MTDI (GPIO12) high at reset selects the 1.8 V VDD_SDIO regulator (section 3.2, same datasheet); the WROOM-32 module carries a
# 3.3 V flash, so it fails to boot. Only the WROOM-32 module is judged: bare chips and WROVER may legitimately use 1.8 V flash.
_ESP32_MTDI = re.compile(r"(?:GPIO|IO)12|MTDI")
_WROOM32 = re.compile(r"ESP32[-_ ]?WROOM[-_ ]?32")
# RP2040, Raspberry Pi RP2040 datasheet (RP-008371-DS-1), section 2.8 Bootrom boot sequence: "Check if SPI CS pin is tied low
# ('bootrom button'), and skip flash boot if so." The CS pin is QSPI_SS.
_RP2040 = re.compile(r"(?<![A-Z0-9])RP2040(?![A-Z0-9])")
_RP2040_SS = re.compile(r"QSPI_?(?:SS|CS)N?")
# Parts that legitimately share the ESP32 flash pins: SPI NOR flash and PSRAM.
_MEMORY = re.compile(r"25[QLVF]\d|W25|GD25|MX25|IS25|XM25|FM25|EN25|BY25|ZD25|PY25|P25Q|N25Q|AT25|SST25|XT25|ZB25|BG25|S25FL|"
                     r"FLASH|PSRAM|APS\d|IPS\d|LY68|ESP-PSRAM|SPI.?NOR|MX66|W74M")
_MEMORY_PIN = re.compile(r"/?(?:HOLD|WP)[\w#/]*|IO[23]")
_OFFENDER_KINDS = {"connector", "led", "switch", "transistor", "module", "ic", "battery"}


def _tokens(pin: Pin) -> list[str]:
    return [token for token in re.split(r"[/,]", _pin_name(pin)) if token]


def _is_memory(design: _Design, comp: Component) -> bool:
    return bool(_MEMORY.search(_text(comp)) or any(_MEMORY_PIN.fullmatch(_pin_name(pin)) for pin in comp.pins))


def _flash_offenders(design: _Design, ic: Component, pin: Pin) -> list[Component]:
    return [comp for comp in _parts_on(design, pin.net, ic).values()
            if not comp.dnp and design.kind[comp.reference] in _OFFENDER_KINDS and not _is_memory(design, comp)]


def _tied_high(design: _Design, net: str) -> bool:
    """The net is a supply rail, or is pulled up (100 kohm or less) with no pull-down on it."""
    if net in design.supplies:
        return True
    ups = downs = 0
    for comp, _ in design.nodes.get(net, ()):
        if design.kind[comp.reference] != "resistor":
            continue
        ends = two_terminal_nets(comp)
        ohms = parse_resistance(comp.value)
        if ends is None or ohms is None or net not in ends:
            continue
        other = ends[1] if ends[0] == net else ends[0]
        if other in design.grounds:
            downs += 1
        elif other in design.supplies and ohms <= 100_000:
            ups += 1
    return bool(ups) and not downs


def _strap_finding(rule_ic: Component, pins: list[Pin], title: str, evidence: str, fix: str) -> Finding:
    return Finding("mcu.strap_pins", Severity.WARNING, f"{rule_ic.reference} {title}", (rule_ic.reference,),
                   tuple(dict.fromkeys(pin.net for pin in pins)),
                   f"{rule_ic.reference} {', '.join(_pin_text(pin) for pin in pins)} {evidence}", fix)


def check_strap_pins(snapshot: DesignSnapshot) -> list[Finding]:
    """ESP32 flash pins / GPIO0 / GPIO12 and RP2040 QSPI_SS wired against the datasheet's boot rules."""
    design = _Design(snapshot)
    findings = []
    for ic in snapshot.components:
        if ic.dnp:
            continue
        text = _text(ic)
        live = [pin for pin in ic.pins if pin.net and not pin.no_connect]
        if _ESP32.search(text):
            flash = [pin for pin in live if any(_ESP32_FLASH.fullmatch(t) for t in _tokens(pin)) and _flash_offenders(design, ic, pin)]
            if flash:
                findings.append(_strap_finding(
                    ic, flash, "uses its flash pins (GPIO6 to GPIO11) for something else",
                    "belongs to the ESP32's SPI flash interface (GPIO6 to GPIO11), but its net also reaches "
                    + ", ".join(sorted({c.reference for pin in flash for c in _flash_offenders(design, ic, pin)}))
                    + ". Those pins are in use by the flash, so the chip will not boot or the flash will crash.",
                    "Move the signal to a free GPIO and leave these pins unconnected."))
            gpio0 = [pin for pin in live if any(_ESP32_GPIO0.fullmatch(t) for t in _tokens(pin)) and pin.net in design.grounds]
            if gpio0:
                findings.append(_strap_finding(
                    ic, gpio0, "has GPIO0 tied to ground",
                    "is the boot-mode strap (GPIO0 low at reset selects download mode) and is wired straight to ground, "
                    "so the ESP32 never starts the application.",
                    "Pull GPIO0 up to 3V3 and add a push button to ground for programming."))
            mtdi = [pin for pin in live if _WROOM32.search(text) and any(_ESP32_MTDI.fullmatch(t) for t in _tokens(pin))
                    and pin.net not in design.grounds and _tied_high(design, pin.net)]
            if mtdi:
                findings.append(_strap_finding(
                    ic, mtdi, "has GPIO12 (MTDI) pulled high",
                    "is the VDD_SDIO voltage strap (MTDI high at reset selects the 1.8 V regulator), and it is tied or "
                    "pulled high; the module's 3.3 V flash then fails to boot.",
                    "Remove the pull-up (keep GPIO12 low or open at reset), or burn the VDD_SDIO eFuse for 3.3 V."))
        if _RP2040.search(text):
            ss = [pin for pin in live if _RP2040_SS.fullmatch(_pin_name(pin)) and pin.net in design.grounds]
            if ss:
                findings.append(_strap_finding(
                    ic, ss, "has QSPI_SS (BOOTSEL) tied to ground",
                    "is the flash chip-select that the boot ROM samples (low = skip flash, enter USB boot), and it is "
                    "wired straight to ground, so the RP2040 always starts in BOOTSEL mode and the flash is never selected.",
                    "Connect QSPI_SS to the flash chip-select; for a BOOTSEL button, switch that line to ground through about 1 kohm."))
    return findings


TABLE_RULES = {
    "power.divider_voltage": check_divider_voltage,
    "mcu.strap_pins": check_strap_pins,
    "power.ldo_dropout": check_ldo_dropout,
}
