"""polarity.reversed, polarity.mosfet_topology, power.mounting_hole_rail, power.rail_voltage_mismatch."""
import pytest

from test_audit_rules import design, part, rules


def found(prefix, *parts):
    return [f.rule for f in rules(design(*parts), prefix)]


def led(a, k, ref="D1", an="A", kn="K", lib="Device:LED", value="LED", footprint="LED_SMD:LED_0603_1608Metric"):
    return part(ref, value, footprint, ("1", k, kn), ("2", a, an), fields={"lib_id": lib})


def cap(pos, neg, lib="Device:CP", ref="C1", footprint="Capacitor_THT:CP_Radial_D5.0mm_P2.00mm", pn="+", nn="-"):
    return part(ref, "100uF", footprint, ("1", pos, pn), ("2", neg, nn), fields={"lib_id": lib})


# --- polarity.reversed: LEDs --------------------------------------------------------------

@pytest.mark.parametrize("rail", ["+3V3", "+5V", "VCC", "3V3", "VBUS", "+12V", "/pwr/+3V3"])
def test_led_anode_on_ground_cathode_on_rail_fires(rail):
    assert found("polarity.reversed", led("GND", rail)) == ["polarity.reversed"]


@pytest.mark.parametrize("an,kn", [("A", "K"), ("Anode", "Cathode"), ("+", "-"), ("A", "C"), ("anode", "cathode")])
def test_led_pin_name_spellings_fire(an, kn):
    assert found("polarity.reversed", led("GND", "+3V3", an=an, kn=kn)) == ["polarity.reversed"]


def test_led_fires_with_ground_variants_and_led_prefix_ref():
    assert found("polarity.reversed", led("AGND", "+5V", ref="LED3", footprint="LED_THT:LED_D5.0mm")) == [
        "polarity.reversed"]
    assert found("polarity.reversed", led("GND", "+5V", ref="D4", lib="Device:LED_Small", value="red")) == [
        "polarity.reversed"]


def test_led_correct_orientation_and_pulled_signals_are_quiet():
    assert found("polarity.reversed", led("+3V3", "GND")) == []
    assert found("polarity.reversed", led("GND", "LED_SIG")) == []   # cathode on a signal, not a rail
    assert found("polarity.reversed", led("LED_SIG", "+3V3")) == []   # anode not on ground
    assert found("polarity.reversed", led("GND", "+3V3", an="", kn="")) == []  # no pin names: orientation unknown


@pytest.mark.parametrize("rail", ["-12V", "-5V", "VEE", "VSS-", "VNEG", "-VIN", "NEG12V"])
def test_led_on_negative_rail_is_quiet(rail):
    assert found("polarity.reversed", led("GND", rail)) == []


def test_led_on_ac_or_bridge_net_is_quiet():
    assert found("polarity.reversed", led("GND", "AC_L")) == []
    assert found("polarity.reversed", led("GND", "BRIDGE_OUT")) == []


def test_seven_segment_pin_names_are_not_read_as_anode_cathode():
    seg = part("U2", "7SEG", "Display_7Segment:HDSP", ("1", "GND", "A"), ("2", "+3V3", "C"), ("3", "NET", "B"))
    assert found("polarity.reversed", seg) == []


# --- polarity.reversed: diodes, TVS, zener, flyback -----------------------------------------

@pytest.mark.parametrize("value,lib", [
    ("SMAJ5.0A", "Device:D_TVS"), ("PESD5V0S1BL", "Device:D"), ("BZX84C3V3", "Device:D_Zener"),
    ("ZD1", "Device:D"), ("ESD9B5.0", "Device:D_TVS"), ("3V3", "Device:D_Zener")])
def test_tvs_esd_zener_across_rail_are_quiet(value, lib):
    diode = part("D1", value, "Diode_SMD:D_SMA", ("1", "+5V", "K"), ("2", "GND", "A"), fields={"lib_id": lib})
    assert found("polarity.reversed", diode) == []


def test_flyback_and_plain_diodes_are_quiet():
    plain = part("D1", "1N4148", "Diode_SMD:D_SOD-123", ("1", "+5V", "K"), ("2", "GND", "A"),
                 fields={"lib_id": "Device:D"})
    assert found("polarity.reversed", plain) == []
    plain = part("D1", "1N4007", "Diode_THT:D_DO-41", ("1", "+12V", "K"), ("2", "GND", "A"))
    assert found("polarity.reversed", plain) == []


def test_led_value_naming_tvs_is_quiet():
    assert found("polarity.reversed", led("GND", "+5V", value="TVS")) == []


# --- polarity.reversed: polarized capacitors ----------------------------------------------

@pytest.mark.parametrize("lib,footprint", [
    ("Device:CP", "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm"),
    ("Device:C_Polarized", "Capacitor_SMD:CP_Elec_6.3x5.4"),
    ("Device:CP_Small", "Capacitor_SMD:C_0805_2012Metric"),
    ("Device:CP_Elec", "Capacitor_SMD:C_0805_2012Metric"),
    ("Device:C_Polarized_Small", "Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A"),
    ("Device:Tantalum", "Capacitor_SMD:C_0805_2012Metric"),
])
@pytest.mark.parametrize("rail", ["+3V3", "+5V", "VCC"])
def test_polarized_cap_plus_on_ground_fires(lib, footprint, rail):
    assert found("polarity.reversed", cap("GND", rail, lib=lib, footprint=footprint)) == ["polarity.reversed"]


def test_polarized_cap_unnamed_pins_use_pin_one_as_plus():
    assert found("polarity.reversed", cap("GND", "+5V", pn="~", nn="~")) == ["polarity.reversed"]
    assert found("polarity.reversed", cap("+5V", "GND", pn="~", nn="~")) == []


def test_polarized_cap_correct_negative_rail_and_plain_cap_are_quiet():
    assert found("polarity.reversed", cap("+5V", "GND")) == []
    assert found("polarity.reversed", cap("GND", "-12V")) == []
    assert found("polarity.reversed", cap("GND", "VEE")) == []
    assert found("polarity.reversed", cap("GND", "AC_IN")) == []
    plain = part("C1", "100nF", "Capacitor_SMD:C_0402", ("1", "GND"), ("2", "+5V"), fields={"lib_id": "Device:C"})
    assert found("polarity.reversed", plain) == []
    assert found("polarity.reversed", cap("GND", "SIG")) == []


# --- polarity.mosfet_topology ---------------------------------------------------------------

def fet(drain, source, gate="CTRL", ref="Q1", value="2N7002", lib="Device:Q_NMOS_GSD", footprint="Package_TO_SOT_SMD:SOT-23"):
    return part(ref, value, footprint, ("1", gate, "G"), ("2", source, "S"), ("3", drain, "D"), fields={"lib_id": lib})


@pytest.mark.parametrize("source", ["+3V3", "+5V", "VCC", "VBUS"])
@pytest.mark.parametrize("value,lib", [("2N7002", "Device:Q_NMOS_GSD"), ("BSS138", "Device:Q"), ("AO3400", "Device:Q"),
                                       ("IRLML2502", "Device:Q"), ("SI2302", "Device:Q"), ("X", "Device:Q_NMOS_GDS")])
def test_nmos_drain_on_ground_source_on_rail_fires(source, value, lib):
    assert found("polarity.mosfet_topology", fet("GND", source, value=value, lib=lib)) == ["polarity.mosfet_topology"]


def test_nmos_normal_topologies_are_quiet():
    assert found("polarity.mosfet_topology", fet("LOAD", "GND")) == []             # low-side switch
    assert found("polarity.mosfet_topology", fet("+5V", "LOAD")) == []             # high-side N switch
    assert found("polarity.mosfet_topology", fet("GND", "LOAD")) == []             # drain on ground only
    assert found("polarity.mosfet_topology", fet("GND", "-12V")) == []


@pytest.mark.parametrize("gate_with", ["source", "drain"])
@pytest.mark.parametrize("value,lib", [("2N7002", "Device:Q_NMOS_GSD"), ("AO3401", "Device:Q_PMOS_GSD"),
                                       ("IRF540", "Device:Q_NMOS_GDS")])
def test_gate_shorted_to_source_or_drain_fires(gate_with, value, lib):
    drain, source = "LOAD", "GND"
    gate = source if gate_with == "source" else drain
    assert found("polarity.mosfet_topology", fet(drain, source, gate=gate, value=value, lib=lib)) == [
        "polarity.mosfet_topology"]


def test_gate_shorted_ignores_jfets_depletion_and_distinct_gate():
    assert found("polarity.mosfet_topology", fet("LOAD", "GND", gate="GND", value="BSS126")) == []
    assert found("polarity.mosfet_topology", fet("LOAD", "GND", gate="GND", value="J201", lib="Device:Q_NJFET_DGS")) == []
    assert found("polarity.mosfet_topology", fet("LOAD", "GND", gate="CTRL")) == []


def test_pmos_source_on_ground_fires():
    assert found("polarity.mosfet_topology",
                 fet("+5V", "GND", value="AO3401", lib="Device:Q_PMOS_GSD")) == ["polarity.mosfet_topology"]
    assert found("polarity.mosfet_topology", fet("LOAD", "GND", value="DMP2035U", lib="Device:Q_PMOS_GDS")) == [
        "polarity.mosfet_topology"]


def test_pmos_reverse_protection_and_high_side_switch_are_quiet():
    # input rail on the drain, protected rail on the source, gate to ground
    assert found("polarity.mosfet_topology",
                 fet("VIN_RAW", "+12V", gate="GND", value="AO3401", lib="Device:Q_PMOS_GSD")) == []
    assert found("polarity.mosfet_topology",
                 fet("VIN_RAW", "+12V", gate="GND", value="IRLML6402", lib="Device:Q_PMOS_GDS")) == []
    assert found("polarity.mosfet_topology", fet("LOAD", "+5V", value="AO3401", lib="Device:Q_PMOS_GSD")) == []


def test_pmos_source_on_ground_in_a_negative_rail_design_is_quiet_unless_drain_is_positive():
    negative = part("U9", "REG", "Package_SO:SOIC-8", ("1", "-12V", "OUT"))
    pm = fet("LOAD", "GND", value="AO3401", lib="Device:Q_PMOS_GSD")
    assert found("polarity.mosfet_topology", pm, negative) == []
    pm = fet("+5V", "GND", value="AO3401", lib="Device:Q_PMOS_GSD")
    assert found("polarity.mosfet_topology", pm, negative) == ["polarity.mosfet_topology"]


def test_non_mosfet_with_gds_pins_is_quiet():
    odd = part("U3", "MYSTERY", "Package_SO:SOIC-8", ("1", "GND", "G"), ("2", "+5V", "S"), ("3", "GND", "D"))
    assert found("polarity.mosfet_topology", odd) == []


# --- power.mounting_hole_rail ---------------------------------------------------------------

def hole(net, ref="H1", footprint="MountingHole:MountingHole_3.2mm_M3_Pad"):
    return part(ref, "MountingHole_Pad", footprint, ("1", net, "1"))


@pytest.mark.parametrize("net", ["+3V3", "+5V", "VCC", "VBUS", "+12V", "/sheet/+3V3"])
@pytest.mark.parametrize("ref,footprint", [("H1", "MountingHole:MountingHole_3.2mm_M3_Pad"),
                                           ("MH2", "MountingHole:MountingHole_2.7mm_M2.5_Pad_Via"),
                                           ("H3", "MountingHole:MountingHole_3.2mm_M3_ISO7380_Pad")])
def test_mounting_hole_on_rail_fires(net, ref, footprint):
    assert found("power.mounting_hole_rail", hole(net, ref, footprint)) == ["power.mounting_hole_rail"]


@pytest.mark.parametrize("net", ["GND", "AGND", "CHASSIS", "EARTH", "", "Net-(H1-Pad1)", "SIGNAL"])
def test_mounting_hole_on_ground_or_unconnected_is_quiet(net):
    assert found("power.mounting_hole_rail", hole(net)) == []


def test_non_hole_part_on_rail_is_quiet():
    pad = part("H4", "Heatsink", "Heatsink:Heatsink_Fischer_FK", ("1", "+5V"))
    assert found("power.mounting_hole_rail", pad) == []
    assert found("power.mounting_hole_rail", part("TP1", "TP", "TestPoint:TestPoint_Pad_D1.0mm", ("1", "+5V"))) == []


# --- power.rail_voltage_mismatch ------------------------------------------------------------

def chip(pin_name, net, number="1"):
    return part("U1", "MCU", "Package_QFP:LQFP-32", (number, net, pin_name, "power_in"), ("2", "GND", "GND", "power_in"))


@pytest.mark.parametrize("pin,net", [
    ("3V3", "+5V"), ("3V3", "5V"), ("3.3V", "VBUS"), ("VDD3P3", "+5V"), ("VCC3V3", "+12V"), ("VDD_3V3", "+5V"),
    ("1V8", "+3V3"), ("1V2", "+3V3"), ("5V", "+3V3"), ("5V", "+12V"), ("VCC5", "+3V3"), ("VCC5", "24V"),
    ("3V3", "+9V"), ("VDD3P3_RTC", "VBUS"), ("1V8", "/pwr/+5V"), ("3V3", "+5V_USB"), ("2V5", "+3.3V")])
def test_pin_voltage_contradicts_net_voltage_fires(pin, net):
    assert found("power.rail_voltage_mismatch", chip(pin, net)) == ["power.rail_voltage_mismatch"]


@pytest.mark.parametrize("pin,net", [
    ("3V3", "+3V3"), ("3V3", "3.3V"), ("3.3V", "VCC_3V3"), ("VDD3P3", "+3V3"), ("5V", "VBUS"), ("5V", "+5V"),
    ("1V8", "+1V8"), ("VCC5", "+5V"),
    ("VDD", "+5V"), ("VCC", "+3V3"), ("VIN", "+12V"), ("VDDIO", "+1V8"),   # no explicit voltage in the pin name
    ("3V3", "VCC"), ("3V3", "VIN"), ("3V3", "VBAT"), ("3V3", "Net-(U1-3V3)"), ("3V3", "SIG"),  # none in the net name
    ("3V3", "5V_EN"), ("5V_TOL", "+3V3"), ("3V3_EN", "+5V"), ("3V3", "5V_TO_3V3"),
    ("3V3", "-5V"), ("-3V3", "+3V3"), ("VDD1", "+3V3"), ("VDD2", "+5V"),
])
def test_pin_voltage_matching_or_not_explicit_is_quiet(pin, net):
    assert found("power.rail_voltage_mismatch", chip(pin, net)) == []


def test_vcc5_index_pin_is_quiet():
    multi = part("U1", "IC", "Package_QFP:LQFP-32", ("1", "+3V3", "VCC5", "power_in"), ("2", "+3V3", "VCC4", "power_in"),
                 ("3", "GND", "GND", "power_in"))
    assert found("power.rail_voltage_mismatch", multi) == []


def test_mismatch_reports_each_part_and_net_once():
    two = part("U1", "REG", "Package_SO:SOIC-8", ("1", "+5V", "3V3"), ("2", "+5V", "3V3"), ("3", "+3V3", "3V3"))
    results = rules(design(two), "power.rail_voltage_mismatch")
    assert len(results) == 1 and results[0].nets == ("+5V",)


def test_dnp_parts_are_quiet():
    from dataclasses import replace
    dnp = replace(chip("3V3", "+5V"), dnp=True)
    assert found("power.rail_voltage_mismatch", dnp) == []


def test_plain_c_symbol_with_polarized_footprint_and_unnamed_pins_is_quiet():
    assert found("polarity.reversed", cap("GND", "+5V", lib="Device:C", pn="", nn="")) == []


@pytest.mark.parametrize("pin", ["U3VDD10", "U2VDD10", "5VSB", "3VDD"])
def test_letters_after_the_v_are_not_a_voltage(pin):
    assert found("power.rail_voltage_mismatch", chip(pin, "+1V0")) == []


def test_rail_label_contradicted_by_its_fixed_regulator_is_not_a_mismatch():
    ldo = part("U5", "LD1117S33TR", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", ("1", "GND", "GND", "power_in"),
               ("2", "+3V0", "VO", "power_out"), ("3", "+5V", "VI", "power_in"))
    mcu = part("U6", "ESP32-S3-WROOM-1", "RF_Module:ESP32-S3-WROOM-1", ("2", "+3V0", "3V3", "power_in"),
               ("1", "GND", "GND", "power_in"))
    assert found("power.rail_voltage_mismatch", ldo, mcu) == []
    assert found("power.rail_voltage_mismatch", mcu) == ["power.rail_voltage_mismatch"]
