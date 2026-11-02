"""Loosely typed control inputs left open (pin.input_floating) and generic TVS values (value.missing)."""
import pytest

from test_audit_rules import design, part, rules
from test_audit_rules import ic as base_ic


def chip(value, pin_name, kind, net=None, footprint="Package_SO:SOIC-8", extra=()):
    return part("U1", value, footprint, ("1", "+5V", "VCC", "power_in"), ("2", "GND", "GND", "power_in"),
                ("3", net or f"unconnected-(U1-{pin_name}-Pad3)", pin_name, kind), *extra)


@pytest.mark.parametrize("name", ["~{SLEEP}", "nSLEEP", "EN", "~{EN}", "CE", "1OE", "~{OE}", "PGTH", "ILIM", "T2IN",
                                  "SHDN", "THERM", "MODE", "SEL1", "DIN"])
@pytest.mark.parametrize("kind", ["unspecified", "bidirectional", "passive", "input"])
def test_in4_open_control_input_is_floating_whatever_its_symbol_type(name, kind):
    found = rules(design(chip("DRV8000", name, kind)), "pin")
    assert [(f.rule, f.refs) for f in found] == [("pin.input_floating", ("U1",))]


def test_in4_stays_quiet_for_flags_connections_internal_pulls_and_other_names():
    assert rules(design(chip("DRV8000", "EN", "unspecified+no_connect")), "pin") == []  # no-connect flag
    wired = chip("DRV8000", "EN", "passive", net="+5V")
    assert rules(design(wired), "pin") == []
    assert rules(design(chip("MCU", "NRST", "passive")), "pin") == []  # reset: internal pull-ups, typed input only
    assert rules(design(chip("MCU", "PA1", "bidirectional")), "pin") == []
    # internal-pull table and ignore list still apply to loosely typed pins
    modem = [("3", "unconnected-(U1-CTS-Pad3)", "~{CTS}", "passive"), ("4", "unconnected-(U1-DSR-Pad4)", "~{DSR}", "passive")]
    assert rules(design(part("U1", "CH340C", "Package_SO:SOIC-16", ("1", "+5V", "VCC", "power_in"), *modem)), "pin") == []
    assert rules(design(chip("DRV8000", "INT", "passive")), "pin") == []
    # a module's EN pin is the module's own business
    module = part("U1", "ESP32-WROOM-32", "RF_Module:ESP32-WROOM-32", ("1", "+3V3", "3V3", "power_in"),
                  ("3", "unconnected-(U1-EN-Pad3)", "EN", "passive"))
    assert rules(design(module), "pin") == []


def test_in4_single_node_label_is_open_but_dnm_resistor_option_is_too():
    assert len(rules(design(chip("DRV8000", "EN", "passive", net="/EN")), "pin")) == 1
    dnm = part("R4", "0R DNM", "Resistor_SMD:R_0402", ("1", "/ILIM"), ("2", "/A0"))
    assert len(rules(design(chip("MCT8316", "ILIM", "input", net="/ILIM"), dnm), "pin")) == 1
    fitted = part("R4", "0R", "Resistor_SMD:R_0402", ("1", "/ILIM"), ("2", "/A0"))
    assert rules(design(chip("MCT8316", "ILIM", "input", net="/ILIM"), fitted), "pin") == []


@pytest.mark.parametrize("value", ["TVS", "TVSP", "ESD", "Zener", "DIODE"])
def test_in4_generic_diode_class_value_is_missing_unless_a_part_number_names_it(value):
    named = part("D1", value, "Diode_SMD:D_SOD-523", ("1", "A"), ("2", "GND"), fields={"lib_id": "Device:D_Filled"})
    found = rules(design(base_ic(), named), "value.missing")
    assert [f.refs for f in found] == [("D1",)]
    with_mpn = part("D1", value, "Diode_SMD:D_SOD-523", ("1", "A"), ("2", "GND"), fields={"MPN": "PESD5V0S1BL"})
    assert rules(design(base_ic(), with_mpn), "value.missing") == []
    real = part("D1", "SMAJ5.0A", "Diode_SMD:D_SMA", ("1", "A"), ("2", "GND"))
    assert rules(design(base_ic(), real), "value.missing") == []
