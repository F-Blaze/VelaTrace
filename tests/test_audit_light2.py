"""Second precision/recall pass on connector, floating-input and decoupling rules."""
from dataclasses import replace

import pytest

from test_audit_precision import header, typed_ic
from test_audit_rules import cap, design, part, res, rules


# --- connector.power_unprotected --------------------------------------------------------

@pytest.mark.parametrize("pin", ["VBAT", "BATT", "BAT+", "VBACKUP", "VRTC"])
def test_conn2_backup_cell_into_a_module_battery_pin_is_managed(pin):
    cell = header("J1", "VCELL", "GND", value="JST_PH")
    module = typed_ic("U1", ("1", "VCELL", pin, "power_in"))
    assert rules(design(cell, module, cap("C1", "VCELL")), "connector") == []


def test_conn2_screw_terminal_into_a_regulator_input_is_still_flagged():
    terminal = header("J1", "VCELL", "GND", value="Screw_Terminal")
    reg = typed_ic("U1", ("1", "VCELL", "VIN", "power_in"), ("2", "+3V3", "VOUT", "power_out"))
    assert [f.nets for f in rules(design(terminal, reg, cap("C1", "VCELL")), "connector")] == [("VCELL",)]


@pytest.mark.parametrize("pin", ["+Vo", "Vo+", "+VOUT", "VOUT+"])
def test_conn2_dc_dc_module_output_spellings_drive_the_rail(pin):
    module = part("PS1", "B0505S", "Converter_DCDC:Mornsun", ("1", "+12V", "+Vin", "power_in"),
                  ("2", "GND", "GND", "power_in"), ("3", "+5V", pin, "power_out"))
    load = typed_ic("U1", ("1", "+5V", "VDD", "power_in"))
    assert rules(design(module, load, header("J2", "+5V", "GND"), cap("C1", "+5V")), "connector") == []


# --- pin.input_floating -----------------------------------------------------------------

def float2_chip(*others):
    chip = part("U1", "CHIP", "Package_SO:SOIC-8", ("1", "+5V", "VCC", "power_in"),
                ("2", "GND", "GND", "power_in"), ("3", "SENSE", "SENSE", "input"))
    return design(chip, cap("C1", "+5V"), *others)


def test_float2_input_whose_only_other_connections_are_dnp_is_floating():
    dnp_res = replace(res("R1", "SENSE", "GND"), dnp=True)
    assert [f.refs for f in rules(float2_chip(dnp_res), "pin")] == [("U1",)]
    assert rules(float2_chip(res("R1", "SENSE", "GND")), "pin") == []
    # An unfitted header or test point alone is an optional access point to an open pin.
    dnp_tp = replace(part("TP1", "TP", "TestPoint:TestPoint_Pad", ("1", "SENSE")), dnp=True)
    assert rules(float2_chip(dnp_tp), "pin") == []
    # One fitted part among the DNP ones keeps the pin connected.
    assert rules(float2_chip(dnp_res, res("R2", "SENSE", "+5V")), "pin") == []


# --- decoupling.missing -----------------------------------------------------------------

def test_dec2_supply_pin_with_only_a_bias_resistor_is_an_incomplete_rail():
    amp = typed_ic("U5", ("8", "+5V", "V+", "power_in"), ("1", "OUT", "OUT", "output"), value="LM358")
    bias = res("R19", "+5V", "Net-(U5A--)", "10k")
    assert [f.refs for f in rules(design(amp, bias), "decoupling")] == [("U5",)]
    # A set pin (not a main supply name) behind a resistor stays quiet.
    vint = typed_ic("U3", ("26", "VINT", "VINT", "power_in"))
    assert rules(design(vint, res("R28", "VINT", "LDO1SET", "2k2")), "decoupling") == []
