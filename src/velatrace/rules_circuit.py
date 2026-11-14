"""Common hobby-circuit mistakes the netlist shows clearly.

  led.current_high              indicator LED (or RGB channel) whose series resistor passes more than 30 mA
  logic.level_mismatch          5 V push-pull output wired straight to a 3.3 V-only input
  mosfet.gate_pulldown_missing  low-side N-MOSFET driven by a GPIO with no gate pull-down
  i2c.address_conflict          two parts on one I2C bus answering at the same 7-bit address

Like the rest of audit_rules these stay quiet whenever the netlist cannot show the problem clearly.
"""
from __future__ import annotations

from decimal import Decimal
import re

from .audit_rules import _Design, _base_type, parse_resistance, two_terminal_nets
from .bom import rail_voltage
from .findings import Finding, Severity
from .models import Component, DesignSnapshot, Pin
from .rules_ratings import _LED_HIGH, _LED_LOW, _name, _volts, led_branches, led_cases


def _texts(comp: Component) -> str:
    return " ".join([comp.value, comp.footprint, *comp.fields.values()]).upper()


# --- led.current_high -------------------------------------------------------------------------

_LED_MAX_A = Decimal("0.03")
_POWER_LED = re.compile(r"(?<![A-Z0-9.])(?:1|2|3|5|10)\s?W(?![A-Z0-9])|COB|XP-|XM-|LUXEON|CREE|OSLON|HIGH.?POWER")
_LED_SIZE = re.compile(r"(?<![0-9])\d{4}(?![0-9])")


def _power_led(led: Component) -> bool:
    footprint = led.footprint.split(":", 1)[-1].upper()
    return bool(_POWER_LED.search(_texts(led)) or "POWER" in footprint
                or any(int(size) > 3535 for size in _LED_SIZE.findall(footprint)))


def _led_volts(led: Component) -> Decimal:
    """Forward drop assumed: 1.8 V red-ish, 2.8 V blue-ish; an unnamed colour takes 3.2 V so the arithmetic
    can only understate the current."""
    text = " ".join([led.value, *led.fields.values()])
    high, low = bool(_LED_HIGH.search(text)), bool(_LED_LOW.search(text))
    return Decimal("3.2") if high == low else Decimal("2.8") if high else Decimal("1.8")


def check_led_current_high(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for led in design.of_kind("led"):
        if led.dnp or _power_led(led):
            continue
        hits = []  # (amps, resistor, rail, volts, ohms, vf, switch): the first case that fires per channel
        for anode, cathode, vf in led_branches(led, _led_volts(led)):
            for resistor, rail, ground, switch in led_cases(design, led, anode, cathode):
                volts = rail_voltage(rail)
                ohms = parse_resistance(resistor.value)
                if (volts is None or ground not in design.grounds or rail in design.grounds or resistor.dnp
                        or not ohms or volts <= vf):
                    continue
                amps = (volts - vf) / ohms
                if amps > _LED_MAX_A:
                    hits.append((amps, resistor, rail, volts, ohms, vf, switch))
                    break
        if not hits:
            continue
        hits.sort(key=lambda hit: -hit[0])
        amps = hits[0][0]
        evidence = []
        for amps_, resistor, rail, volts, ohms, vf, switch in hits:
            via = f", returning through {switch.reference} to ground" if switch else ""
            evidence.append(
                f"{resistor.reference} ({resistor.value}) limits {led.reference} from {rail}{via}: "
                f"({volts:g} V - {vf:g} V) / {ohms:g} Ω = {amps_ * 1000:.0f} mA (LED drop assumed {vf:g} V)")
        findings.append(Finding(
            "led.current_high", Severity.WARNING,
            f"{led.reference} runs at about {amps * 1000:.0f} mA, above the 30 mA of an indicator LED",
            tuple(dict.fromkeys([led.reference, *(ref for hit in hits for ref in
                                                  (hit[1].reference, *([hit[6].reference] if hit[6] else [])))])),
            tuple(dict.fromkeys(hit[2] for hit in hits)),
            "; ".join(evidence) + "; a small indicator LED is rated about 20 mA.",
            "Raise the resistor so the current stays near 2-10 mA for an indicator, or name the LED as a "
            "power LED if it is meant to carry this current."))
    return findings


# --- logic.level_mismatch ---------------------------------------------------------------------

_SHIFTER = re.compile(r"TXS|TXB|TXU|LSF|NLSX|PCA9306|PCA9509|(?<![A-Z0-9])(?:SN)?74[A-Z]*\d|LVC|AHCT|BSS138|GTL|MAX300|ADG3")
_NOT_5V_TOLERANT = re.compile(
    r"ESP32|ESP8266|ESP-?(?:WROOM|WROVER|12)|RP2040|RP2350|NRF5[23]\d*|SAMD[25]\d|ATSAMD|"
    r"RASPBERRY.?PI.?CM[45]|COMPUTE.?MODULE.?[45]")
_VCC_PIN = re.compile(r"V(?:CC|DD)\d*|V\+")
_IO_SUPPLY = re.compile(r"V\w*IO\w*|VL|VLOGIC|VDDQ")


def _rails(design: _Design, comp: Component, names: re.Pattern | None) -> list[Decimal | None]:
    """Voltage of the net of every power_in pin (named like `names` when given); ground counts as 0."""
    return [Decimal(0) if pin.net in design.grounds else rail_voltage(pin.net)
            for pin in comp.pins if _base_type(pin) == "power_in" and (names is None or names.fullmatch(_name(pin)))]


def check_level_mismatch(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for net, nodes in design.nodes.items():
        if net in design.grounds or net in design.supplies or len(nodes) != 2:
            continue
        (a, pa), (b, pb) = nodes
        if a is b:
            continue
        for (drv, out), (rcv, inp) in (((a, pa), (b, pb)), ((b, pb), (a, pa))):
            if (_base_type(out) != "output" or _base_type(inp) not in {"input", "bidirectional"} or drv.dnp
                    or rcv.dnp or design.kind[drv.reference] != "ic" or design.kind[rcv.reference] not in {"ic", "module"}
                    or _SHIFTER.search(_texts(drv)) or not _NOT_5V_TOLERANT.search(_texts(rcv))):
                continue
            if any(_IO_SUPPLY.fullmatch(_name(pin)) for pin in drv.pins if _base_type(pin) == "power_in"):
                continue  # a separate I/O supply sets the output level
            high, low = _rails(design, drv, _VCC_PIN), [v for v in _rails(design, rcv, None) if v != 0]
            if (not high or any(v is None or v < Decimal("4.5") for v in high)
                    or not low or any(v is None or v > Decimal("3.6") for v in low)):
                continue
            supply = next(pin.net for pin in drv.pins if _base_type(pin) == "power_in" and _VCC_PIN.fullmatch(_name(pin)))
            logic = next(pin.net for pin in rcv.pins if _base_type(pin) == "power_in" and pin.net not in design.grounds)
            findings.append(Finding(
                "logic.level_mismatch", Severity.WARNING,
                f"{drv.reference} ({_volts(rail_voltage(supply))} logic) drives {rcv.reference} "
                f"({_volts(rail_voltage(logic))} only) directly",
                (drv.reference, rcv.reference), (net,),
                f"{drv.reference} {out.name or out.number} is a push-pull output powered from {supply} "
                f"({_volts(rail_voltage(supply))}) and is wired straight to {rcv.reference} {inp.name or inp.number}, "
                f"which runs from {logic} ({_volts(rail_voltage(logic))}) and is not 5 V tolerant; nothing else is on "
                f"{net}.",
                "Add a level shifter or a resistor divider, or power the driver from 3.3 V."))
    return findings


# --- mosfet.gate_pulldown_missing -------------------------------------------------------------

_NMOS = re.compile(
    r"N[-_ ]?CHANNEL|NMOS|AO3400|AO3416|IRLML(?:2502|6344|0030|2060)|2N7000|2N7002|BSS138|BSS123|SI2302|IRLZ44N|"
    r"IRLZ34N|IRF540N?|FDN337N|IRLB8721|DMN\d")
_PMOS = re.compile(r"P[-_ ]?CHANNEL|PMOS")
_GATE_DRIVER = re.compile(r"DRIVER|IR21|IR44|TC44|MCP14|UCC27|LM510|TPS28|MIC44|FAN73|NCP51|2EDL|1EDN|LTC4440|DRV\d|GATE")
_GATE_RESISTOR_MAX = Decimal(1000)


def _single_net(comp: Component, name: str) -> str | None:
    nets = {pin.net for pin in comp.pins if _name(pin) == name}
    return nets.pop() if len(nets) == 1 and "" not in nets else None


def _gpio_on(design: _Design, net: str, exclude: Component) -> tuple[Component, Pin] | None:
    """The one part on the net besides `exclude`, when it is an IC or module bidirectional (GPIO) pin."""
    others = [(comp, pin) for comp, pin in design.nodes[net] if comp is not exclude]
    if len(others) != 1:
        return None
    comp, pin = others[0]
    if (design.kind[comp.reference] not in {"ic", "module"} or _base_type(pin) != "bidirectional"
            or _GATE_DRIVER.search(_texts(comp))):
        return None
    return comp, pin


# MCUs whose GPIOs reset with internal pull-downs enabled (RP2040/RP2350 datasheets: default pad state).
_PULLED_DOWN_AT_RESET = re.compile(r"RP2040|RP2350|(?<![A-Z])PICO")


def check_gate_pulldown_missing(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    findings = []
    for q in design.of_kind("transistor"):
        text = _texts(q)
        names = {_name(pin) for pin in q.pins}
        if (q.dnp or not q.reference.upper().startswith("Q") or not {"G", "S", "D"} <= names
                or not names <= {"G", "S", "D"} or not _NMOS.search(text) or _PMOS.search(text)):
            continue
        gate, source, drain = _single_net(q, "G"), _single_net(q, "S"), _single_net(q, "D")
        if not gate or source not in design.grounds or not drain:
            continue
        driver, series = _gpio_on(design, gate, q), None
        if driver is None:
            nodes = [comp for comp, _ in design.nodes[gate] if comp is not q]
            if len(nodes) != 1 or design.kind[nodes[0].reference] != "resistor":
                continue
            series = nodes[0]
            ends = two_terminal_nets(series)
            ohms = parse_resistance(series.value)
            if series.dnp or ends is None or not ohms or ohms > _GATE_RESISTOR_MAX or gate not in ends:
                continue
            far = ends[1] if ends[0] == gate else ends[0]
            if far == gate or (driver := _gpio_on(design, far, series)) is None:
                continue
        mcu, pin = driver
        if _PULLED_DOWN_AT_RESET.search(_texts(mcu)):
            continue  # its GPIOs come out of reset with the internal pull-down on
        via = f" through {series.reference} ({series.value})" if series else ""
        findings.append(Finding(
            "mosfet.gate_pulldown_missing", Severity.INFO,
            f"{q.reference} gate has no pull-down",
            (q.reference, mcu.reference), (gate,),
            f"{q.reference} switches its load to ground (source on {source}) and its gate is driven only by "
            f"{mcu.reference} {pin.name or pin.number}{via}, with no resistor from the gate to ground; while "
            f"{mcu.reference} resets or is unpowered the pin floats and the load can switch on.",
            "Add a 10 kΩ-100 kΩ resistor from the gate to ground, on the MOSFET side of any series resistor."))
    return findings


# --- i2c.address_conflict ---------------------------------------------------------------------

_SDA_PIN = re.compile(r"SDA|SDI|SDA/SDI|SDI/SDA")
_SCL_PIN = re.compile(r"SCL|SCK|SCLK|SCL/SCK|SCK/SCL")
_OWN_SUPPLY = re.compile(r"V(?:DD|CC|S|DDIO|IO|IN)\d*|V\+")
_HIGH, _SDA, _SCL = 1, 2, 3
_ADDR_PINS = tuple(f"A{i}" for i in range(6))


def _bits(base: int, *shifts: int):
    return lambda levels: base + sum(level << shift for level, shift in zip(levels, shifts))


def _fixed(address: int):
    return lambda levels: address


# label, value pattern, address pins (regex each), levels per pin (2 = GND/supply, 4 = also SDA/SCL), address
_I2C_PARTS = (
    ("BME280/BMP280", r"BME280|BMP280", ("SDO",), 2, _bits(0x76, 0)),
    ("SHT3x", r"SHT3\d", ("ADDR",), 2, _bits(0x44, 0)),
    ("SHT4x", r"SHT4\d(?!.*[BC]D\d)", (), 2, _fixed(0x44)),
    ("SSD1306/SH1106", r"SSD1306|SH1106", ("SA0|D/C#?",), 2, _bits(0x3C, 0)),
    ("MPU/ICM IMU", r"MPU-?(?:6050|9250)|ICM-?20948", ("(?:SDO/)?AD0(?:/SDO)?",), 2, _bits(0x68, 0)),
    ("DS3231/DS1307", r"DS3231|DS1307", (), 2, _fixed(0x68)),
    ("PCF8563", r"PCF8563", (), 2, _fixed(0x51)),
    ("INA219/INA226", r"INA219|INA226", ("A0", "A1"), 4, _bits(0x40, 0, 2)),
    ("ADS1115", r"ADS1115", ("ADDR",), 4, _bits(0x48, 0)),
    ("24Cxx EEPROM", r"24(?:LC|AA|FC|C)(?:02|32|64|128|256|512)(?!\d)", _ADDR_PINS[:3], 2, _bits(0x50, 0, 1, 2)),
    ("PCA9685", r"PCA9685", _ADDR_PINS, 2, _bits(0x40, 0, 1, 2, 3, 4, 5)),
    ("MCP23017", r"MCP23017", _ADDR_PINS[:3], 2, _bits(0x20, 0, 1, 2)),
    ("VEML7700", r"VEML7700", (), 2, _fixed(0x10)),
    ("BH1750", r"BH1750", ("ADDR",), 2, lambda levels: 0x5C if levels[0] else 0x23),
    ("AHT20", r"AHT20", (), 2, _fixed(0x38)),
    ("SCD4x", r"SCD4\d", (), 2, _fixed(0x62)),
)


def _pin_level(design: _Design, comp: Component, pattern: str, states: int, sda: str, scl: str):
    """(level, pin name, net) of the one pin matching `pattern`: 0 on ground, 1 on a supply or the part's own
    supply pin net, 2/3 on the part's own SDA/SCL (states == 4); None when open or on anything else."""
    pins = [pin for pin in comp.pins if re.fullmatch(pattern, _name(pin))]
    nets = {pin.net for pin in pins}
    if not pins or len(nets) != 1 or "" in nets:
        return None
    net = nets.pop()
    own = {pin.net for pin in comp.pins if pin.net and _OWN_SUPPLY.fullmatch(_name(pin)) and _base_type(pin) == "power_in"}
    if net in design.grounds:
        level = 0
    elif states == 4 and net == sda:
        level = _SDA
    elif states == 4 and net == scl:
        level = _SCL
    elif net in design.supplies or net in own:
        level = _HIGH
    else:
        return None
    return level, pins[0].name or pins[0].number, net


def _i2c_device(design: _Design, comp: Component):
    """(part label, sda, scl, address, address-pin text) when the netlist fixes this part's address."""
    if (comp.dnp or design.kind[comp.reference] in {"module", "connector"}
            or re.search(r"MODULE|BREAKOUT", comp.value, re.IGNORECASE)):
        return None
    for label, pattern, pins, states, address in _I2C_PARTS:
        if re.search(pattern, comp.value.upper()):
            break
    else:
        return None
    sda = {pin.net for pin in comp.pins if _SDA_PIN.fullmatch(_name(pin))}
    scl = {pin.net for pin in comp.pins if _SCL_PIN.fullmatch(_name(pin))}
    if len(sda) != 1 or len(scl) != 1 or "" in sda | scl or sda == scl:
        return None
    sda, scl = sda.pop(), scl.pop()
    levels = [_pin_level(design, comp, pin, states, sda, scl) for pin in pins]
    if any(level is None for level in levels):
        return None
    where = ", ".join(f"{name} on {net}" for _, name, net in levels) or "fixed address"
    return label, sda, scl, address(tuple(level for level, _, _ in levels)), where


def check_i2c_address_conflict(snapshot: DesignSnapshot) -> list[Finding]:
    design = _Design(snapshot)
    buses: dict[tuple[str, str, int], list[tuple[Component, str]]] = {}
    for comp in snapshot.components:
        if found := _i2c_device(design, comp):
            _, sda, scl, address, where = found
            buses.setdefault((sda, scl, address), []).append((comp, where))
    findings = []
    for (sda, scl, address), devices in buses.items():
        if len(devices) < 2:
            continue
        refs = tuple(sorted(comp.reference for comp, _ in devices))
        listing = "; ".join(f"{comp.reference} ({comp.value}, {where})" for comp, where in devices)
        findings.append(Finding(
            "i2c.address_conflict", Severity.ERROR,
            f"{' and '.join(refs)} both answer at I2C address 0x{address:02X} on {sda}/{scl}",
            refs, (sda, scl),
            f"{listing} share the bus {sda}/{scl} and all resolve to the 7-bit address 0x{address:02X}, "
            "so only one of them can be addressed.",
            "Change the address-select pin of one part, move it to its own bus, or use an I2C multiplexer."))
    return findings
