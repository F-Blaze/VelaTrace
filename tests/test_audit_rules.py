"""Deterministic audit rules: one positive and one negative case per rule, no AI."""
from decimal import Decimal
from pathlib import Path

import pytest

from velatrace import audit_rules
from velatrace.audit_rules import (is_ground, is_supply_name, i2c_line, parse_resistance,
                                   register_provider, run_rules, summarize)
from velatrace.findings import Finding, Severity
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.netlist import read_xml_netlist

FIXTURES = Path(__file__).parent / "fixtures" / "audit"


def part(ref, value, footprint, *pins, pos=None, fields=None):
    """pins: (number, net[, name[, electrical_type[, position]]])"""
    made = []
    for row in pins:
        number, net, *rest = row
        name = rest[0] if len(rest) > 0 else ""
        kind = rest[1] if len(rest) > 1 else ""
        at = rest[2] if len(rest) > 2 else None
        made.append(Pin(number, net, name, kind, at))
    return Component(ref, value, footprint, tuple(made), fields or {}, position_mm=pos)


def design(*components):
    return DesignSnapshot(tuple(components), "fixture")


def rules(snapshot, rule_prefix):
    return [item for item in run_rules(snapshot, providers=()) if item.rule.startswith(rule_prefix)]


def ic(ref="U1", *extra, value="MCU", footprint="Package_QFP:LQFP-32"):
    return part(ref, value, footprint, ("1", "+3V3", "VDD"), ("2", "GND", "GND"), *extra)


def cap(ref="C1", a="+3V3", b="GND", value="100nF", pos=None, pin_pos=(None, None)):
    return part(ref, value, "Capacitor_SMD:C_0402", ("1", a, "", "", pin_pos[0]),
                ("2", b, "", "", pin_pos[1]), pos=pos)


def res(ref, a, b, value="4.7k"):
    return part(ref, value, "Resistor_SMD:R_0402", ("1", a), ("2", b))


# --- heuristics -------------------------------------------------------------------------

@pytest.mark.parametrize("net", ["GND", "AGND", "GNDA", "/power/GND", "VSS", "0V"])
def test_ground_names(net):
    assert is_ground(net)


@pytest.mark.parametrize("net,supply", [("+3V3", True), ("/sheet/+5V", True), ("3V3", True),
                                        ("VCC", True), ("VBUS", True), ("3.3V", True),
                                        ("3V3_EN", False), ("VBUS_DET", False), ("GND", False),
                                        ("Net-(U1-VDD)", False), ("SDA", False), ("VOUT", False)])
def test_supply_names(net, supply):
    assert is_supply_name(net) is supply


@pytest.mark.parametrize("net,line", [("SDA", "SDA"), ("I2C1_SDA", "SDA"), ("/io/SCL0", "SCL"),
                                      ("SCLK", None), ("Net-(U1-SDA)", None), ("MOSI", None),
                                      ("SPI_SCL", None), ("SDA_CTRL", None)])
def test_i2c_names(net, line):
    assert i2c_line(net) == line


@pytest.mark.parametrize("text,ohms", [("10k", 10000), ("4k7", 4700), ("4.7kΩ", 4700), ("100R", 100),
                                       ("4R7", Decimal("4.7")), ("0R", 0), ("0", 0), ("1M", 1000000),
                                       ("10m", Decimal("0.010")), ("10 kOhm", 10000), ("10 k", 10000), ("10k 1%", 10000),
                                       ("R", None), ("DNP", None), ("100nF", None)])
def test_parse_resistance(text, ohms):
    assert parse_resistance(text) == (None if ohms is None else Decimal(ohms))


# --- decoupling -------------------------------------------------------------------------

def test_decoupling_missing_and_present():
    found = rules(design(ic()), "decoupling")
    assert [(f.rule, f.severity, f.refs, f.nets) for f in found] == [
        ("decoupling.missing", Severity.INFO, ("U1",), ("+3V3",))]  # beta rule
    assert found[0].title == "U1 has no decoupling capacitor on +3V3"
    assert found[0].cost_delta > 0
    assert rules(design(ic(), cap()), "decoupling") == []


def test_decoupling_uses_pin_types_when_present():
    typed = part("U1", "SENSOR", "Package_SO:SOIC-8", ("1", "+3V3", "EN", "input"),
                 ("2", "GND", "GND", "power_in"), ("3", "Net-(U1-VDD)", "VDD", "power_in"))
    found = rules(design(typed, cap("C1", "+3V3", "GND")), "decoupling")
    # EN tied to +3V3 is not a supply pin; the filtered VDD net has no capacitor.
    assert [f.nets for f in found] == [("Net-(U1-VDD)",)]
    assert rules(design(typed, cap("C2", "Net-(U1-VDD)", "GND")), "decoupling") == []


def test_decoupling_skips_modules_and_boards_without_ground_names():
    module = ic(footprint="RF_Module:ESP32-WROOM-32")
    assert rules(design(module), "decoupling") == []
    unnamed = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "+3V3"), ("2", "Net-(U1-GND)"))
    assert rules(design(unnamed), "decoupling") == []


def test_decoupling_far_uses_pad_positions():
    near_ic = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "+3V3", "VDD", "", (10.0, 10.0)),
                   ("2", "GND", "GND", "", (10.0, 12.0)))
    far = cap(pin_pos=((22.0, 10.0), (23.0, 10.0)))
    found = rules(design(near_ic, far), "decoupling")
    assert [(f.rule, f.severity, f.refs) for f in found] == [("decoupling.far", Severity.WARNING, ("U1", "C1"))]
    assert "12.0 mm" in found[0].title
    close = cap(pin_pos=((12.0, 10.0), (13.0, 10.0)))
    assert rules(design(near_ic, close), "decoupling") == []
    # Schematic sources have no positions: no distance claims at all.
    assert rules(design(ic(), cap()), "decoupling") == []


def missing(*parts):
    return [(f.refs, f.nets) for f in rules(design(*parts), "decoupling.missing")]


def typed_chip(*pins, ref="U1", value="CHIP"):
    return part(ref, value, "Package_SO:SOIC-8", ("99", "GND", "GND", "power_in"), *pins)


def test_decoupling_op_amp_and_mcu_supply_without_a_capacitor_is_still_flagged():
    opamp = typed_chip(("1", "+5V", "V+", "power_in"), ("2", "OUT", "OUT", "output"), value="LM358")
    assert missing(opamp) == [(("U1",), ("+5V",))]
    assert missing(ic()) == [(("U1",), ("+3V3",))]
    assert missing(ic(), cap("C1", "+3V3", "GND", "100nF")) == []


def test_decoupling_pins_that_are_not_supply_inputs_are_ignored():
    # Switch node, enable and reference/charge-pump pins typed power, however they are named.
    chip = typed_chip(("1", "Net-(U1-SW)", "SW_NODE", "power_in"), ("2", "+5V", "~{SHDN}", "power_in"),
                      ("3", "Net-(U1-VREF)", "VREF_OUT", "power_out"), ("4", "Net-(U1-VCP)", "VCP", "power_in"),
                      ("5", "+3V3", "CAP1", "power_in"))
    assert missing(chip) == []
    # An internal-LDO output that is named like a rail still is not a supply input.
    ldo_pin = typed_chip(("1", "+1V8", "VDD_LDO", "power_out"))
    load = part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "+1V8"), ("2", "GND"))
    assert missing(ldo_pin, load) == []


def test_decoupling_ignores_pins_on_return_nets_and_pins_strapped_to_ground():
    protector = typed_chip(("1", "B-", "VM", "power_in"), ("2", "Net-(U1-VCC)", "VCC", "power_in"))
    assert missing(protector, cap("C1", "Net-(U1-VCC)", "B-")) == []
    strapped = typed_chip(("1", "Net-(U1-VDD2)", "VDD2", "power_in"))
    assert missing(strapped, res("R1", "Net-(U1-VDD2)", "GND", "470")) == []
    assert missing(strapped, res("R1", "Net-(U1-VDD2)", "+5V", "470")) == [(("U1",), ("Net-(U1-VDD2)",))]


def test_decoupling_through_series_fuse_or_ferrite_from_a_reservoir():
    reg = typed_chip(("1", "/VIN_P", "VIN", "power_in"))
    reservoir = cap("C1", "/VIN_RAW", value="470uF")
    fuse = part("F1", "2A", "Fuse:Fuse_1206", ("1", "/VIN_RAW"), ("2", "/VIN_P"))
    assert missing(reg, reservoir, fuse) == []
    bead = part("FB1", "BLM18", "Inductor_SMD:L_0603", ("1", "/VIN_RAW"), ("2", "/VIN_P"))
    assert missing(reg, reservoir, bead) == []
    assert missing(reg, fuse) == [(("U1",), ("/VIN_P",))]


def test_decoupling_cap_between_stacked_rails_counts():
    stacked = typed_chip(("1", "+12V", "VDDH", "power_in"), ("2", "+5V", "VDDL", "power_in"))
    assert missing(stacked) == [(("U1",), ("+12V",)), (("U1",), ("+5V",))]
    assert missing(stacked, cap("C1", "+12V", "+5V")) == []


def pixel(ref, rail="+5V", pos=None, footprint="LED_SMD:LED_SK6812MINI"):
    a = lambda n: (pos or {}).get(n)
    return part(ref, "SK6812", footprint, ("1", rail, "VDD", "power_in", a(1)),
                ("2", f"Net-({ref}-DOUT)", "DOUT", "output"), ("3", "GND", "VSS", "power_in"),
                ("4", f"Net-({ref}-DIN)", "DIN", "input"))


def test_addressable_leds_without_any_capacitor_are_one_finding_per_rail():
    # D* and RGB* references alike, found by value or footprint, one finding for the whole rail.
    plain = part("RGB3", "LED_RGB", "LED_SMD:LED_WS2812B_PLCC4_5.0x5.0mm", ("1", "+5V", "VDD"),
                 ("2", "GND", "VSS"), ("3", "Net-(RGB3-DIN)", "DIN"), ("4", "Net-(RGB3-DOUT)", "DOUT"))
    assert missing(pixel("D1"), pixel("D2"), plain) == [(("D1", "D2", "RGB3"), ("+5V",))]
    assert missing(pixel("D1"), pixel("D2"), cap("C1", "+5V")) == []
    assert missing(pixel("D1", "+3V3"), pixel("D2"), cap("C1", "+5V")) == [(("D1",), ("+3V3",))]


def test_addressable_leds_far_from_the_capacitor_are_one_finding_per_rail():
    leds = [pixel(f"D{n}", pos={1: (50.0 * n, 0.0)}) for n in (1, 2)]
    near_d1 = cap("C1", "+5V", pin_pos=((51.0, 0.0), (52.0, 0.0)))
    found = rules(design(*leds, near_d1), "decoupling")
    assert [(f.rule, f.refs) for f in found] == [("decoupling.far", ("D2",))]
    both_far = cap("C1", "+5V", pin_pos=((500.0, 0.0), (501.0, 0.0)))
    found = rules(design(*leds, both_far), "decoupling")
    assert [(f.rule, f.refs, f.nets) for f in found] == [("decoupling.far", ("D1", "D2"), ("+5V",))]


# --- I2C pull-ups -----------------------------------------------------------------------

def bus(*extra):
    return design(ic("U1", ("3", "SDA", "SDA")), ic("U2", ("3", "SDA", "SDA"), value="SENSOR"),
                  cap(), *extra)


def test_i2c_missing_pullup():
    found = rules(bus(), "i2c")
    assert [(f.rule, f.severity, f.nets) for f in found] == [("i2c.pullup.missing", Severity.INFO, ("SDA",))]  # beta rule
    assert rules(bus(res("R1", "SDA", "+3V3")), "i2c") == []


def test_i2c_redundant_pullups_are_a_saving():
    found = rules(bus(res("R1", "SDA", "+3V3"), res("R2", "+3V3", "SDA")), "i2c")
    assert [(f.rule, f.severity, f.refs) for f in found] == [("i2c.pullup.redundant", Severity.SAVING, ("R1", "R2"))]
    assert found[0].cost_delta == Decimal("-0.01")
    assert "2.35 kΩ" in found[0].evidence


def test_i2c_mixed_rails_zero_ohm_and_bad_values():
    mixed = rules(bus(res("R1", "SDA", "+3V3"), res("R2", "SDA", "+5V")), "i2c")
    assert [f.rule for f in mixed] == ["i2c.pullup.mixed_rails"]
    zero = rules(bus(res("R1", "SDA", "+3V3", "0R")), "i2c")
    assert [(f.rule, f.severity) for f in zero] == [("i2c.pullup.zero_ohm", Severity.ERROR)]
    low = rules(bus(res("R1", "SDA", "+3V3", "100")), "i2c")
    assert [f.rule for f in low] == ["i2c.pullup.value"]


def test_i2c_is_silent_when_it_cannot_judge():
    array = part("RN1", "4x4.7k", "Resistor_SMD:R_Array_Convex_4x0603", ("1", "SDA"), ("8", "+3V3"))
    assert rules(bus(array), "i2c") == []
    passthrough = design(part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "SDA"), ("2", "SDA")),
                         part("J2", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x01", ("1", "SDA")))
    assert rules(passthrough, "i2c") == []
    spi = design(ic("U1", ("3", "SCLK")), ic("U2", ("3", "SCLK"), value="FLASH"), cap())
    assert rules(spi, "i2c") == []


def i2c_mcu(*extra):
    """An MCU with GPIO-named pins whose PB9 drives SDA (a likely I2C master)."""
    return part("U1", "STM32F103", "Package_QFP:LQFP-48", ("1", "+3V3", "VDD"), ("2", "GND", "GND"),
                ("3", "SDA", "PB9"), ("4", "GPIO_A", "PA0"), ("5", "GPIO_B", "PA1"), ("6", "GPIO_C", "PA2"), *extra)


def i2c_sensor(ref="U2", *extra):
    return part(ref, "SHT31", "Package_DFN_QFN:DFN-8", ("1", "+3V3", "VDD"), ("2", "GND", "GND"),
                ("3", "SDA", "SDA"), *extra)


def i2c_jumper(ref, a, b):
    return part(ref, "I2C_PUE", "Jumper:SolderJumper_2_Open", ("1", a, "A"), ("2", b, "B"))


def i2c_header():
    return part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "SDA"), ("2", "GND"))


def test_i2c_pullups_behind_a_jumper_are_an_optional_note():
    for chain in ((res("R3", "SDA", "Net-(JP1-B)"), i2c_jumper("JP1", "+3V3", "Net-(JP1-B)")),   # bus -> R -> JP -> rail
                  (i2c_jumper("JP1", "SDA", "Net-(JP1-B)"), res("R3", "Net-(JP1-B)", "+3V3"))):  # bus -> JP -> R -> rail
        found = rules(design(i2c_mcu(), i2c_sensor(), cap(), *chain), "i2c")
        assert [(f.rule, f.severity, f.refs) for f in found] == [("i2c.pullup.optional", Severity.INFO, ("JP1", "R3"))]
        assert "JP1" in found[0].title and "host" in found[0].fix
    assert "i2c.pullup.optional" in audit_rules.BETA_RULES
    # A zero-ohm link behind the jumper is no pull-up; a real resistor straight to the rail needs no note.
    zero = design(i2c_mcu(), i2c_sensor(), cap(), res("R3", "SDA", "Net-(JP1-B)", "0R"), i2c_jumper("JP1", "+3V3", "Net-(JP1-B)"))
    assert [f.rule for f in rules(zero, "i2c")] == ["i2c.pullup.missing"]
    direct = design(i2c_mcu(), i2c_sensor(), cap(), res("R3", "SDA", "+3V3"), i2c_jumper("JP1", "+3V3", "Net-(JP1-B)"))
    assert rules(direct, "i2c") == []


def test_i2c_without_any_pullup_and_an_onboard_mcu_is_still_missing():
    for extra in ((), (i2c_header(),)):
        found = rules(design(i2c_mcu(), i2c_sensor(), cap(), *extra), "i2c")
        assert [f.rule for f in found] == ["i2c.pullup.missing"]


def test_i2c_bus_of_a_plug_in_board_without_a_master_is_left_to_the_host():
    assert rules(design(i2c_sensor(), cap(), i2c_header()), "i2c") == []
    # The same slave alone with nothing else attached still reads as a missing pull-up.
    assert [f.rule for f in rules(design(i2c_sensor(), i2c_sensor("U3"), cap()), "i2c")] == ["i2c.pullup.missing"]


def test_i2c_named_nets_that_are_spi_are_skipped():
    lcd = part("LCD1", "ST7789", "Display:LCD-SMD", ("1", "SDA", "SDA"), ("2", "SCL", "SCL"), ("3", "CS", "CS"),
               ("4", "DC", "RS"), ("5", "RST", "RES"), ("6", "GND", "GND"))
    spi_pins = ic("U1", ("3", "SDA", "GPIO19"), ("4", "SCL", "GPIO18"), ("5", "CS", "GPIO17"), ("6", "DC", "GPIO21"),
                  ("7", "RST", "GPIO16"))
    assert rules(design(spi_pins, lcd, cap()), "i2c") == []
    # An I2C OLED whose D/C pin is only an address strap and which has just a RES line is still I2C.
    oled = part("U2", "SSD1306", "Package_QFN:QFN-24", ("1", "SDA", "SDA"), ("2", "SCL", "SCL"), ("3", "GND", "D/C"),
                ("4", "RST", "RES"), ("5", "GND", "GND"))
    i2c_pins = ic("U1", ("3", "SDA", "GPIO19"), ("4", "SCL", "GPIO18"), ("5", "RST", "GPIO16"))
    assert {f.rule for f in rules(design(i2c_pins, oled, cap()), "i2c")} == {"i2c.pullup.missing"}
    # A display line shared through a jumper crossover carries the SPI name too: SDA_SCLK, SCL_MOSI.
    dual = design(ic("U1", ("3", "SDA_SCLK", "GPIO14")), ic("U2", ("3", "SDA_SCLK", "SDA"), value="DISPLAY"), cap())
    assert rules(dual, "i2c") == []


# --- LEDs -------------------------------------------------------------------------------

def led(ref, a, k, a_name="A", k_name="K"):
    return part(ref, "Red", "LED_SMD:LED_0603", ("1", k, k_name), ("2", a, a_name))


def test_led_across_rail_without_resistor():
    found = rules(design(led("D1", "+3V3", "GND")), "led")
    assert [(f.rule, f.severity, f.refs) for f in found] == [("led.no_resistor", Severity.ERROR, ("D1",))]
    ok = design(led("D1", "Net-(D1-A)", "GND"), res("R1", "+3V3", "Net-(D1-A)", "1k"))
    assert rules(ok, "led") == []


def test_led_series_chain_is_one_finding():
    chain = design(led("D1", "+5V", "Net-(D1-K)"), led("D2", "Net-(D1-K)", "GND"))
    found = rules(chain, "led")
    assert [(f.severity, f.refs) for f in found] == [(Severity.ERROR, ("D1", "D2"))]


def test_led_on_gpio_without_resistor():
    mcu = ic("U1", ("5", "STATUS", "PA5"))
    found = rules(design(mcu, cap(), led("D1", "STATUS", "GND")), "led")
    assert [(f.severity, f.refs) for f in found] == [(Severity.WARNING, ("D1", "U1"))]
    # Without pin names (PCB/IPC source) the driver pin cannot be judged: stay silent.
    anonymous = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "+3V3"), ("2", "GND"), ("5", "STATUS"))
    assert rules(design(anonymous, cap(), led("D1", "STATUS", "GND")), "led") == []


# --- nets and pins ----------------------------------------------------------------------

def test_single_pin_named_net():
    found = rules(design(ic("U1", ("3", "SDA1", "SDA")), cap()), "net")
    assert [(f.rule, f.severity, f.nets) for f in found] == [("net.single_pin", Severity.INFO, ("SDA1",))]
    auto = design(ic("U1", ("3", "unconnected-(U1-NC-Pad3)"), ("4", "Net-(U1-Pad4)")), cap())
    assert rules(auto, "net") == []
    header = design(part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x01", ("1", "SPARE")))
    assert [f.severity for f in rules(header, "net")] == [Severity.INFO]


def test_single_pin_label_repeating_the_gpio_name_is_documentation():
    for label, pin in (("GP22", "GPIO22"), ("GPIO22", "GPIO22"), ("IO35", "IO35"), ("PB4", "PB4"), ("B4", "PB4")):
        assert rules(design(ic("U1", ("3", label, pin)), cap()), "net") == [], label
    # A functional name on an MCU pin is still a possible dangling label.
    found = rules(design(ic("U1", ("3", "LED3", "PB10")), cap()), "net")
    assert [(f.rule, f.nets) for f in found] == [("net.single_pin", ("LED3",))]
    other_pin = rules(design(ic("U1", ("3", "GPIO22", "GPIO23")), cap()), "net")
    assert [f.rule for f in other_pin] == ["net.single_pin"]


def test_floating_ic_inputs_need_pin_types():
    def chip(kind):
        return part("U1", "LDO", "Package_TO_SOT_SMD:SOT-23-5", ("1", "+5V", "VIN", "power_in"),
                    ("2", "GND", "GND", "power_in"), ("3", "unconnected-(U1-EN-Pad3)", "EN", kind))
    found = rules(design(chip("input")), "pin")
    assert [(f.rule, f.severity, f.refs) for f in found] == [("pin.input_floating", Severity.INFO, ("U1",))]  # beta rule
    assert "EN" in found[0].title
    assert rules(design(chip("input+no_connect")), "pin") == []
    assert rules(design(chip("passive")), "pin") == []


def eeprom_or_bridge(value, *pins, mpn=None):
    return part("U1", value, "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", ("1", "+3V3", "VCC", "power_in"),
                ("2", "GND", "GND", "power_in"), *pins, fields={"MPN": mpn} if mpn else None)


def open_input(number, name):
    return (number, f"unconnected-(U1-{name}-Pad{number})", name, "input")


def test_floating_inputs_with_internal_pulls_are_allowed_per_part_family():
    eeprom = eeprom_or_bridge("24C02", open_input("3", "A0"), open_input("4", "A1"), open_input("5", "A2"),
                              open_input("6", "WP"))
    assert rules(design(eeprom), "pin") == []
    by_mpn = eeprom_or_bridge("EEPROM", open_input("3", "A0"), open_input("6", "WP"), mpn="FM24C64B-G")
    assert rules(design(by_mpn), "pin") == []
    modem = [open_input(str(n), name) for n, name in enumerate(["~{CTS}", "~{DSR}", "~{RI}", "~{DCD}", "R232"], 3)]
    assert rules(design(eeprom_or_bridge("CH340C", *modem)), "pin") == []
    # Only the listed pins: another open input of the same part is still reported.
    found = rules(design(eeprom_or_bridge("CH340C", *modem, open_input("9", "RXD"))), "pin")
    assert [(f.rule, f.refs) for f in found] == [("pin.input_floating", ("U1",))] and "RXD" in found[0].title
    assert "A3" in rules(design(eeprom_or_bridge("24C02", open_input("3", "A3"))), "pin")[0].title


def test_floating_inputs_stay_flagged_where_a_pin_must_be_driven():
    reset = part("U1", "STM32F103", "Package_QFP:LQFP-48", ("1", "+3V3", "VDD", "power_in"), ("2", "GND", "GND", "power_in"),
                 open_input("3", "NRST"))
    opamp = part("U1", "LM358", "Package_SO:SOIC-8", ("1", "+5V", "V+", "power_in"), ("2", "GND", "V-", "power_in"),
                 open_input("3", "+"))
    gate = part("U1", "74HC00", "Package_SO:SOIC-14", ("1", "+3V3", "VCC", "power_in"), ("2", "GND", "GND", "power_in"),
                open_input("3", "A"))
    for chip in (reset, opamp, gate):
        assert [f.rule for f in rules(design(chip), "pin")] == ["pin.input_floating"]


# --- duplicates -------------------------------------------------------------------------

def test_identical_parallel_ics():
    twins = design(ic("U1", ("3", "OUT"), value="TMP36"), ic("U2", ("3", "OUT"), value="TMP36"), cap())
    found = rules(twins, "duplicate")
    assert [(f.severity, f.refs) for f in found] == [(Severity.SAVING, ("U1", "U2"))]
    on_bus = design(ic("U1", ("3", "SDA"), value="TMP102"), ic("U2", ("3", "SDA"), value="TMP102"), cap())
    assert [f.severity for f in rules(on_bus, "duplicate")] == [Severity.WARNING]
    strapped = design(ic("U1", ("3", "SDA"), ("4", "GND"), value="TMP102"),
                      ic("U2", ("3", "SDA"), ("4", "+3V3"), value="TMP102"), cap())
    assert rules(strapped, "duplicate") == []
    parallel_caps = design(ic(), cap("C1"), cap("C2"))
    assert rules(parallel_caps, "duplicate") == []


# --- connectors and values --------------------------------------------------------------

def test_connector_power_protection_note():
    jack = part("J1", "Barrel", "Connector_BarrelJack:BarrelJack", ("1", "VIN"), ("2", "GND"))
    load = ic("U1", ("3", "VIN", "VIN"))
    found = rules(design(jack, load, cap(), cap("C2", "VIN")), "connector")
    assert [(f.severity, f.nets) for f in found] == [(Severity.INFO, ("VIN",))]
    diode = part("D1", "SS14", "Diode_SMD:D_SMA", ("1", "VIN"), ("2", "Net-(D1-K)"))
    assert rules(design(jack, load, cap(), cap("C2", "VIN"), diode), "connector") == []
    regulator = part("U2", "LDO", "Package_TO_SOT_SMD:SOT-23-5", ("1", "VIN", "VIN", "power_in"),
                     ("2", "GND", "GND", "power_in"), ("3", "+3V3", "VOUT", "power_out"))
    header = part("J2", "Out", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "+3V3"), ("2", "GND"))
    driven = [f for f in rules(design(regulator, header, cap(), cap("C2", "VIN")), "connector")]
    assert driven == []


def ldo(out="+3V3"):
    return part("U2", "LDO", "Package_TO_SOT_SMD:SOT-23-5", ("1", "VIN", "VIN", "power_in"),
                ("2", "GND", "GND", "power_in"), ("3", out, "VOUT", "power_out"))


def test_connector_sending_a_rail_out_is_not_a_power_input():
    header = part("J2", "Out", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "+3V3"), ("2", "GND"))
    load = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "+3V3", "VDD", "power_in"), ("2", "GND", "GND", "power_in"))
    assert rules(design(ldo(), header, load, cap(), cap("C2", "VIN")), "connector") == []
    # Module with a power_out pin and no regulator part: the pin produces the rail.
    module = part("U3", "PSU", "Module:PSU", ("1", "+3V3", "3V3", "power_out"), ("2", "GND", "GND", "power_in"))
    assert rules(design(module, header, load, cap()), "connector") == []


def test_debug_connector_is_not_a_power_input():
    swd = part("J3", "Conn_ARM_JTAG_SWD_10", "Connector_PinHeader_1.27mm:PinHeader_2x05",
               ("1", "+3V3"), ("2", "SWDIO"), ("3", "GND"))
    load = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "+3V3", "VDD", "power_in"), ("2", "GND", "GND", "power_in"))
    assert rules(design(swd, load, cap()), "connector") == []


def test_connector_net_fed_through_a_series_resistor_is_driven():
    sensor = part("J4", "Sensor", "Connector_JST:JST_PH_B3B", ("1", "+3V3_S"), ("2", "GND"))
    load = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "+3V3_S", "VDD", "power_in"), ("2", "GND", "GND", "power_in"))
    feed = res("R1", "+3V3", "+3V3_S", "4.7")
    assert rules(design(ldo(), sensor, load, feed, cap(), cap("C2", "+3V3_S")), "connector") == []
    # The same connector with nothing feeding the rail is still a power input.
    assert [f.nets for f in rules(design(sensor, load, cap("C2", "+3V3_S")), "connector")] == [("+3V3_S",)]


def test_screw_terminal_into_a_regulator_without_protection_is_flagged():
    terminal = part("J1", "Screw", "TerminalBlock:TerminalBlock_1x02", ("1", "+12V"), ("2", "GND"))
    reg = part("U2", "LDO", "Package_TO_SOT_SMD:SOT-23-5", ("1", "+12V", "VIN", "power_in"),
               ("2", "GND", "GND", "power_in"), ("3", "+3V3", "VOUT", "power_out"))
    found = rules(design(terminal, reg, cap(), cap("C2", "+12V")), "connector")
    assert [(f.rule, f.nets) for f in found] == [("connector.power_unprotected", ("+12V",))]


@pytest.mark.parametrize("value,pin", [("CH217A", "IN"), ("TPS2115A", "IN1"), ("LM66100", "VIN"),
                                       ("TPS25940", "IN"), ("Load_Switch", "VIN")])
def test_connector_rail_into_a_protecting_ic_is_not_flagged(value, pin):
    usb = part("J1", "USB_A", "Connector_USB:USB_A", ("1", "VBUS"), ("2", "GND"))
    switch = part("U2", value, "Package_TO_SOT_SMD:SOT-23-5", ("1", "VBUS", pin, "power_in"),
                  ("2", "GND", "GND", "power_in"), ("3", "VSW", "OUT", "power_out"))
    load = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "VSW", "VDD", "power_in"), ("2", "GND", "GND", "power_in"))
    assert rules(design(usb, switch, load, cap("C1", "VBUS")), "connector") == []
    # The same rail into an ordinary regulator is still an unprotected input.
    reg = part("U3", "LDO", "Package_TO_SOT_SMD:SOT-23-5", ("1", "VBUS", "VIN", "power_in"),
               ("2", "GND", "GND", "power_in"), ("3", "VSW", "VOUT", "power_out"))
    assert [f.rule for f in rules(design(usb, reg, load, cap("C1", "VBUS")), "connector")] == [
        "connector.power_unprotected"]


def test_battery_connector_into_a_charger_without_protection_is_flagged():
    batt = part("J5", "Battery", "Connector_JST:JST_PH_B2B", ("1", "VBAT"), ("2", "GND"))
    charger = part("U4", "Charger", "Package_SO:SOIC-8", ("1", "VBAT", "BAT", "power_out"),
                   ("2", "VBUS", "IN", "power_in"), ("3", "GND", "GND", "power_in"))
    found = rules(design(batt, charger, cap("C1", "VBAT")), "connector")
    assert [(f.rule, f.nets) for f in found] == [("connector.power_unprotected", ("VBAT",))]


def test_zero_ohm_short_and_unset_values():
    short = design(ic(), cap(), res("R1", "+3V3", "GND", "0"))
    assert [(f.rule, f.severity) for f in rules(short, "value")] == [("value.zero_ohm_short", Severity.ERROR)]
    jumper = design(ic("U1", ("3", "A"), ("4", "B")), cap(), res("R1", "A", "B", "0R"))
    assert rules(jumper, "value") == []
    unset = design(ic(), cap("C1", value="C"), res("R2", "A", "B", "R"))
    found = rules(unset, "value")
    assert [(f.rule, f.severity, f.refs) for f in found] == [("value.missing", Severity.WARNING, ("C1", "R2"))]
    assert rules(design(ic(), cap("C1", value="100nF"), res("R2", "A", "B", "10k"),
                        part("TP1", "~", "TestPoint:TestPoint_Pad_D1.0mm", ("1", "A")),
                        part("H1", "~", "MountingHole:MountingHole_3.2mm_M3", ("1", "")),
                        part("FID1", "~", "Fiducial:Fiducial_1mm_Mask2mm"),
                        part("NT1", "~", "NetTie:NetTie-2_SMD_Pad0.5mm", ("1", "A"), ("2", "B"))), "value") == []


def values_missing(*parts):
    return [(f.severity, f.refs) for f in rules(design(ic(), cap(), *parts), "value.missing")]


def test_value_missing_covers_more_kinds_and_symbol_names():
    bare = "Package_SO:SOIC-8"
    found = values_missing(
        part("U2", "~", bare, ("1", "A")),
        part("J1", "~", "Connector_RJ:RJ45", ("1", "A")),
        part("K1", "Relay_SPDT", "Relay_THT:Relay", ("1", "A")),
        part("Y1", "Crystal", "Crystal:Crystal_SMD", ("1", "A")),
        part("F1", "Polyfuse", "Fuse:Fuse_1206", ("1", "A")),
        part("FB1", "FerriteBead", "Inductor_SMD:L_0603", ("1", "A")),
        part("R3", "R_Small_US", "Resistor_SMD:R_0402", ("1", "A")),
        part("D2", "D_Schottky", "Diode_SMD:D_SOD-123", ("1", "A")),
        part("R4", "Rset", "Resistor_SMD:R_0402", ("1", "A")),
        part("R5", "Custom_Sym", "Resistor_SMD:R_0402", ("1", "A"), fields={"lib_id": "Device:Custom_Sym"}),
        part("U3", "LM358", bare, ("1", "A"), fields={"lib_id": "Amplifier_Operational:LM358"}))
    assert found == [(Severity.WARNING, ("D2", "F1", "FB1", "J1", "K1", "R3", "R4", "R5", "U2", "Y1"))]


def test_value_missing_skips_copper_parts_and_part_number_symbols():
    # Solder jumpers and PCB trace antennas are copper, not orderable parts.
    assert values_missing(
        part("JP1", "SolderJumper_2_Open", "Jumper:SolderJumper-2_P1.3mm_Open", ("1", "A")),
        part("AE1", "Antenna", "footprints:2.4GHz-meander-antenna-v2", ("1", "A")),
        # A connector or switch whose symbol name is its part number / a commodity generic name.
        part("AE2", "KH-5220-A56_U.FL", "otter:KH-5220-A56", ("1", "A"), fields={"lib_id": "otter:KH-5220-A56_U.FL"}),
        part("SW1", "SW_Push", "Button_Switch_SMD:SW_SPST_TL3342", ("1", "A"),
             fields={"lib_id": "Switch:SW_Push"})) == []
    # Still flagged when the value is empty.
    assert values_missing(part("SW2", "~", "Button_Switch_SMD:SW_SPST_TL3342", ("1", "A"))) == [
        (Severity.WARNING, ("SW2",))]


def test_value_missing_part_number_symbol_name_is_a_real_value():
    rgb = lambda ref, value, symbol: part(ref, value, "LED_SMD:LED_RGB", ("1", "A"),  # noqa: E731
                                     fields={"lib_id": f"LED:{symbol}"})
    assert values_missing(rgb("D3", "ASMB-MTB0-0A3A2", "ASMB-MTB0-0A3A2"), part(
        "D4", "WS2812B", "LED_SMD:LED_5050", ("1", "A"), fields={"lib_id": "LED:WS2812B"})) == []
    # A generic symbol name left as the value is still missing.
    assert values_missing(rgb("D5", "LED_RGB_Common", "LED_RGB_Common"), rgb("D6", "LED", "LED")) == [
        (Severity.WARNING, ("D5", "D6"))]
    assert values_missing(part("RN1", "R_Pack04", "Resistor_SMD:R_Array", ("1", "A"),
                               fields={"lib_id": "Device:R_Pack04"})) == [(Severity.WARNING, ("RN1",))]


def test_value_missing_order_code_only_is_softer():
    found = rules(design(ic(), cap(), part("R1", "R_Small", "Resistor_SMD:R_0402", ("1", "A"),
                                           fields={"LCSC": "C12345"}),
                         part("R2", "~", "Resistor_SMD:R_0402", ("1", "A"), fields={"LCSC": "C999"}),
                         part("R3", "10k", "Resistor_SMD:R_0402", ("1", "A"), fields={"LCSC": "C25744"})),
                  "value.missing")
    assert [(f.severity, f.refs) for f in found] == [(Severity.INFO, ("R1", "R2"))]
    assert "only the order code identifies it" in found[0].evidence and "C12345" in found[0].evidence


def test_value_missing_generic_value_with_mpn_or_stock_id_is_fine():
    named = lambda ref, fields: part(ref, "LED", "LED_SMD:LED_0603", ("1", "A"), fields=fields)  # noqa: E731
    assert values_missing(named("D1", {"MPN": "19-217/GHC"}), named("D2", {"UST_ID": "42"}),
                   named("D3", {"Stock": "S-7"})) == []
    assert values_missing(named("D4", {"UST_ID": ""})) == [(Severity.WARNING, ("D4",))]
    # An empty value stays missing whatever else identifies the part.
    assert values_missing(part("D5", "~", "LED_SMD:LED_0603", ("1", "A"), fields={"MPN": "19-217"})) == [
        (Severity.WARNING, ("D5",))]


def test_coverage_note_when_no_ground_is_named():
    unnamed = design(part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02",
                          ("1", "Net-(J1-Pin_1)"), ("2", "Net-(J1-Pin_2)")))
    assert [f.rule for f in rules(unnamed, "audit")] == ["audit.coverage"]
    assert rules(design(ic(), cap()), "audit") == []


# --- engine -----------------------------------------------------------------------------

def test_run_rules_sorts_by_severity_and_isolates_failing_providers():
    def extra(snapshot):
        return [Finding("bom.example", Severity.ERROR, "extra provider finding")]

    def broken(snapshot):
        raise RuntimeError("provider bug")

    found = run_rules(design(ic(), led("D1", "+3V3", "GND"), ic("U2", ("3", "SDA1"))),
                      providers=(extra, broken))
    order = [audit_rules.SEVERITY_ORDER[item.severity] for item in found]
    assert order == sorted(order)
    assert "bom.example" in {item.rule for item in found}
    failed = [item for item in found if item.rule == "audit.check_failed"]
    assert failed and "provider bug" in failed[0].evidence and failed[0].severity == Severity.INFO


def test_register_provider_is_one_line(monkeypatch):
    monkeypatch.setattr(audit_rules, "EXTRA_PROVIDERS", [])
    seen = []
    register_provider(lambda snapshot: seen.append(snapshot) or [])
    snapshot = design(ic(), cap())
    run_rules(snapshot)
    assert seen == [snapshot]


def test_summary_line():
    findings = [Finding("a", Severity.ERROR, "x"), Finding("b", Severity.ERROR, "y"),
                Finding("c", Severity.WARNING, "z"),
                Finding("d", Severity.SAVING, "s", cost_delta=Decimal("-0.40")),
                Finding("e", Severity.SAVING, "t", cost_delta=Decimal("-0.02"))]
    assert summarize(findings) == "2 errors · 1 warning · 2 savings · est. $0.42/board saving"
    assert summarize([]) == "No issues found by the built-in checks"


def test_fixture_netlist_flags_the_duplicate_sensor_only():
    found = run_rules(read_xml_netlist(FIXTURES / "necessity.xml"), providers=())
    assert [(f.rule, f.severity, f.refs) for f in found] == [
        ("duplicate.parallel_ic", Severity.SAVING, ("U1", "U2"))]
