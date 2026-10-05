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
                                      ("SCLK", None), ("Net-(U1-SDA)", None), ("MOSI", None)])
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


def test_floating_ic_inputs_need_pin_types():
    def chip(kind):
        return part("U1", "LDO", "Package_TO_SOT_SMD:SOT-23-5", ("1", "+5V", "VIN", "power_in"),
                    ("2", "GND", "GND", "power_in"), ("3", "unconnected-(U1-EN-Pad3)", "EN", kind))
    found = rules(design(chip("input")), "pin")
    assert [(f.rule, f.severity, f.refs) for f in found] == [("pin.input_floating", Severity.INFO, ("U1",))]  # beta rule
    assert "EN" in found[0].title
    assert rules(design(chip("input+no_connect")), "pin") == []
    assert rules(design(chip("passive")), "pin") == []


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


def test_zero_ohm_short_and_unset_values():
    short = design(ic(), cap(), res("R1", "+3V3", "GND", "0"))
    assert [(f.rule, f.severity) for f in rules(short, "value")] == [("value.zero_ohm_short", Severity.ERROR)]
    jumper = design(ic("U1", ("3", "A"), ("4", "B")), cap(), res("R1", "A", "B", "0R"))
    assert rules(jumper, "value") == []
    unset = design(ic(), cap("C1", value="C"), res("R2", "A", "B", "R"))
    found = rules(unset, "value")
    assert [(f.rule, f.severity, f.refs) for f in found] == [("value.missing", Severity.WARNING, ("C1", "R2"))]
    ordered = design(ic(), part("C1", "C", "Capacitor_SMD:C_0402", ("1", "+3V3"), ("2", "GND"),
                                fields={"LCSC": "C1525"}))
    assert rules(ordered, "value") == []


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
