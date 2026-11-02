"""Signal-mistake rules: crystal load capacitors, swapped USB D+/D-, output contention. Fire every time, stay quiet otherwise."""
import pytest

from test_audit_rules import cap, design, exp4_spare, part, res, rules

XTAL_FP = "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"


def mcu(xin="OSC_IN", xout="OSC_OUT", value="STM32F103", ref="U1"):
    return part(ref, value, "Package_QFP:LQFP-48", ("5", "XI", xin), ("6", "XO", xout),
                ("1", "+3V3", "VDD"), ("2", "GND", "GND"), *exp4_spare())


def xtal2(ref="Y1", value="8MHz", a="XI", b="XO"):
    return part(ref, value, "Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm", ("1", a), ("2", b))


def xtal4(ref="Y1", value="8MHz"):
    return part(ref, value, XTAL_FP, ("1", "XI"), ("2", "GND"), ("3", "XO"), ("4", "GND"))


def crystal_found(*parts):
    return [(f.refs, f.nets) for f in rules(design(*parts), "crystal.load_caps_missing")]


def both_caps():
    return [cap("C1", "XI", "GND", "12pF"), cap("C2", "XO", "GND", "12pF")]


# --- crystal.load_caps_missing ------------------------------------------------------------------

@pytest.mark.parametrize("xin,xout", [
    ("OSC_IN", "OSC_OUT"), ("XIN", "XOUT"), ("XI", "XO"), ("PH0-OSC_IN(PH0)", "PH1-OSC_OUT(PH1)"),
    ("PC14-OSC32_IN", "PC15-OSC32_OUT"), ("XTAL1", "XTAL2"), ("XTAL_N", "XTAL_P"), ("PB6/XTAL1/TOSC1", "PB7/XTAL2/TOSC2"),
    ("XTAL_32K_P", "XTAL_32K_N"), ("OSC1", "OSC2"), ("OSC32_IN", "OSC32_OUT"), ("~{XIN}", "XOUT"), ("XTALI", "XTALO"),
    ("P1.00/XL1", "P1.01/XL2"), ("XC1", "XC2"), ("PF0", "PF1"), ("XTA", "XTB")])
def test_crystal_without_caps_fires_for_each_pin_spelling(xin, xout):
    assert crystal_found(mcu(xin, xout), xtal2()) == [(("Y1", "U1"), ("XI", "XO"))]


def test_crystal_four_pin_without_caps_fires():
    assert crystal_found(mcu(), xtal4()) == [(("Y1", "U1"), ("XI", "XO"))]


def test_crystal_x_prefix_fires():
    assert crystal_found(mcu(), xtal2("X1")) == [(("X1", "U1"), ("XI", "XO"))]


def test_crystal_with_one_cap_reports_only_the_other_net():
    assert crystal_found(mcu(), xtal2(), cap("C1", "XI", "GND", "12pF")) == [(("Y1", "U1"), ("XO",))]
    assert crystal_found(mcu(), xtal4(), cap("C2", "XO", "GND", "12pF")) == [(("Y1", "U1"), ("XI",))]


def test_crystal_cap_across_the_crystal_is_not_a_load_cap():
    assert crystal_found(mcu(), xtal2(), cap("C1", "XI", "XO", "12pF")) == [(("Y1", "U1"), ("XI", "XO"))]


@pytest.mark.parametrize("crystal", [xtal2(), xtal4()])
def test_crystal_with_both_caps_is_quiet(crystal):
    assert crystal_found(mcu(), crystal, *both_caps()) == []


def test_crystal_with_a_dnp_load_cap_is_quiet():
    dnp = cap("C1", "XI", "GND", "12pF")
    dnp = type(dnp)(dnp.reference, dnp.value, dnp.footprint, dnp.pins, dnp.fields, dnp=True)
    assert crystal_found(mcu(), xtal2(), dnp, cap("C2", "XO", "GND", "12pF")) == []


def test_active_oscillator_is_quiet():
    osc = part("Y1", "OSC 8MHz", "Oscillator:Oscillator_SMD_3225-4Pin", ("1", "+3V3", "OE"), ("2", "GND", "GND"),
               ("3", "XI", "OUT", "output"), ("4", "+3V3", "VDD", "power_in"))
    assert crystal_found(mcu(), osc) == []
    # A 4-pad part valued like an oscillator, even with plain passive pins and no supply pin.
    assert crystal_found(mcu(), part("Y1", "TCXO-26MHz", XTAL_FP, ("1", "XI"), ("2", "GND"), ("3", "GND"),
                                     ("4", "XO"))) == []
    assert crystal_found(mcu(), part("Y1", "8MHz", XTAL_FP, ("1", "XI", "OUT", "output"), ("2", "GND"), ("3", "XO"),
                                     ("4", "GND"))) == []


def test_ceramic_resonator_with_built_in_caps_is_quiet():
    resonator = part("Y1", "8MHz", "Crystal:Resonator_SMD_muRata_CSTxExxV-3Pin_3.0x1.1mm",
                     ("1", "XI"), ("2", "GND"), ("3", "XO"))
    assert crystal_found(mcu(), resonator) == []
    assert crystal_found(mcu(), part("Y1", "CSTCE8M00G55-R0", "Crystal:Crystal_SMD_3Pin", ("1", "XI"), ("2", "GND"),
                                     ("3", "XO"))) == []


@pytest.mark.parametrize("rtc", ["PCF8563", "DS1307", "RV-3028-C7", "PCF85063A", "MCP7940N"])
def test_watch_crystal_on_an_rtc_with_internal_caps_is_quiet(rtc):
    chip = part("U2", rtc, "Package_SO:SOIC-8", ("1", "XI", "OSCI"), ("2", "XO", "OSCO"), ("3", "+3V3", "VDD"),
                ("4", "GND", "GND"), *exp4_spare())
    assert crystal_found(chip, xtal2(value="32.768kHz")) == []
    # The same chip with an 8 MHz crystal is not the watch case.
    assert len(crystal_found(chip, xtal2(value="8MHz"))) == 1


def test_watch_crystal_on_an_mcu_still_fires():
    assert crystal_found(mcu("PC14-OSC32_IN", "PC15-OSC32_OUT"), xtal2(value="32.768kHz")) == [
        (("Y1", "U1"), ("XI", "XO"))]


@pytest.mark.parametrize("soc", ["nRF54L15", "BM15x", "EFR32BG22"])
def test_soc_with_internal_load_caps_is_quiet(soc):
    assert crystal_found(mcu("XL1", "XL2", value=soc), xtal2(value="32.768kHz")) == []


def test_crystal_quiet_when_not_on_oscillator_pins_or_without_ground_or_on_a_module():
    other = part("U1", "MCU", "Package_QFP:LQFP-48", ("5", "XI", "PA0"), ("6", "XO", "PA1"), ("1", "+3V3", "VDD"),
                 ("2", "GND", "GND"), *exp4_spare())
    assert crystal_found(other, xtal2()) == []
    half = part("U1", "MCU", "Package_QFP:LQFP-48", ("5", "XI", "XIN"), ("6", "XO", "PA1"), ("1", "+3V3", "VDD"),
                ("2", "GND", "GND"), *exp4_spare())
    assert crystal_found(half, xtal2()) == []
    assert crystal_found(mcu(), xtal2("Y1", "8MHz", "XI", "GND")) == []
    module = part("U1", "ESP32-WROOM", "RF_Module:ESP32-WROOM-32", ("5", "XI", "XIN"), ("6", "XO", "XOUT"))
    assert crystal_found(module, xtal2()) == []
    no_ground = part("U1", "MCU", "Package_QFP:LQFP-48", ("5", "XI", "XIN"), ("6", "XO", "XOUT"), *exp4_spare())
    assert crystal_found(no_ground, xtal2()) == []


def test_crystal_series_damping_resistor_net_is_quiet():
    assert crystal_found(mcu(), xtal2(a="XI", b="XO2"), res("R1", "XO", "XO2", "100")) == []


# --- usb.dp_dn_swapped --------------------------------------------------------------------------

def usb_ic(dp_net, dm_net, dp="DP", dm="DM", ref="U1", value="STM32F072"):
    return part(ref, value, "Package_QFP:LQFP-48", ("1", dp_net, dp), ("2", dm_net, dm), ("3", "+3V3", "VDD"),
                ("4", "GND", "GND"), *exp4_spare())


def usb_conn(dp_net, dm_net, dp="D+", dm="D-", ref="J1"):
    return part(ref, "USB_B_Micro", "Connector_USB:USB_Micro-B_Molex", ("2", dm_net, dm), ("3", dp_net, dp),
                ("5", "GND", "GND"))


def usb_found(*parts):
    return [(f.refs, f.nets) for f in rules(design(*parts), "usb.dp_dn_swapped")]


@pytest.mark.parametrize("net,pin", [("USB_DP", "DM"), ("USB_D+", "D-"), ("D+", "DN"), ("DP", "USB_DM"),
                                     ("D_P", "D-"), ("/usb/USB_DP", "USB_D-"), ("USB1_DP", "DMINUS")])
def test_ic_side_swap_fires(net, pin):
    assert usb_found(usb_ic("OTHER", net, dp="X1", dm=pin)) == [(("U1",), (net,))]


@pytest.mark.parametrize("net,pin", [("USB_DM", "DP"), ("USB_D-", "D+"), ("D-", "DP"), ("DM", "USB_DP"),
                                     ("D_N", "D+"), ("DN", "DP")])
def test_net_named_minus_on_a_plus_pin_fires(net, pin):
    assert usb_found(usb_ic(net, "OTHER", dp=pin, dm="X1")) == [(("U1",), (net,))]


def test_connector_side_swap_fires():
    assert usb_found(usb_conn("USB_DM", "USB_DP")) == [(("J1",), ("USB_DM",)), (("J1",), ("USB_DP",))]
    assert usb_found(usb_conn("USB_DP", "USB_DM", dp="D-", dm="D+")) == [(("J1",), ("USB_DM",)), (("J1",), ("USB_DP",))]


def test_connector_dplus_and_ic_dminus_on_an_unnamed_net_fire():
    swapped = usb_ic("Net-(J1-D+)", "Net-(J1-D-)", dp="DM", dm="DP")
    assert [f.refs for f in rules(design(usb_conn("Net-(J1-D+)", "Net-(J1-D-)"), swapped),
                                  "usb.dp_dn_swapped")] == [("J1", "U1"), ("J1", "U1")]


def test_correct_wiring_is_quiet():
    straight = (usb_conn("USB_DP", "USB_DM"), usb_ic("USB_DP", "USB_DM"))
    assert usb_found(*straight) == []
    assert usb_found(usb_conn("Net-(J1-D+)", "Net-(J1-D-)"), usb_ic("Net-(J1-D+)", "Net-(J1-D-)")) == []
    assert usb_found(usb_conn("D+", "D-"), usb_ic("D+", "D-", dp="PA12/USB_DP", dm="PA11/USB_DM")) == []


def test_usb_c_double_row_and_hub_variants_are_quiet():
    c = part("J1", "USB_C", "Connector_USB:USB_C_Receptacle", ("A6", "D+", "D+"), ("B6", "D+", "D+"),
             ("A7", "D-", "D-"), ("B7", "D-", "D-"), ("A12", "GND", "GND"))
    assert usb_found(c, usb_ic("D+", "D-", dp="DP1", dm="DM1")) == []


def test_esd_array_pins_and_neutral_names_are_quiet():
    esd = part("U3", "USBLC6-2SC6", "Package_TO_SOT_SMD:SOT-23-6", ("1", "USB_DP", "IO1"), ("3", "USB_DM", "IO2"),
               ("2", "GND", "GND"))
    assert usb_found(esd, usb_ic("USB_DP", "USB_DM")) == []
    tvs = part("D1", "TPD2E001", "Package_TO_SOT_SMD:SOT-23-6", ("1", "USB_DP", "D-"), ("2", "USB_DM", "D+"))
    assert usb_found(tvs, usb_ic("USB_DP", "USB_DM")) == []
    assert usb_found(usb_ic("USB_DP", "USB_DM", dp="PA12", dm="PA11")) == []
    assert usb_found(usb_ic("USB_DP", "USB_DM", dp="1D+", dm="1D-")) == []


def test_charger_strap_of_dplus_to_dminus_on_one_connector_is_quiet():
    assert usb_found(usb_conn("DCP", "DCP")) == []


def test_non_usb_dp_names_are_quiet():
    display = part("U1", "SEVENSEG", "Package_DIP:DIP-16", ("1", "DP_AUX_N", "DP"), ("2", "X", "DM"), *exp4_spare())
    assert usb_found(display) == []


# --- net.output_contention ----------------------------------------------------------------------

def driver(ref, net, pin="Y", kind="output", value="74HC04", unit_pins=()):
    return part(ref, value, "Package_SO:SOIC-14", ("1", net, pin, kind), *unit_pins, ("7", "GND", "GND", "power_in"),
                ("14", "+3V3", "VCC", "power_in"))


def contention(*parts):
    return [(f.refs, f.nets) for f in rules(design(*parts), "net.output_contention")]


def test_two_push_pull_outputs_on_one_net_fire():
    assert contention(driver("U1", "SIG"), driver("U2", "SIG")) == [(("U1", "U2"), ("SIG",))]
    assert contention(driver("U1", "SIG", "Q"), driver("U2", "SIG", "OUT"), driver("U3", "SIG", "1Y")) == [
        (("U1", "U2", "U3"), ("SIG",))]
    assert contention(driver("U1", "Net-(U1-Y)"), driver("U2", "Net-(U1-Y)", "Q", "output+no_connect")) == [
        (("U1", "U2"), ("Net-(U1-Y)",))]


def test_output_against_inputs_is_quiet():
    assert contention(driver("U1", "SIG"), driver("U2", "SIG", "A", "input"), driver("U3", "SIG", "B", "input")) == []


@pytest.mark.parametrize("kind", ["open_collector", "open_emitter", "tri_state", "bidirectional", "power_out",
                                  "passive", "unspecified"])
def test_other_driver_types_are_quiet(kind):
    assert contention(driver("U1", "SIG"), driver("U2", "SIG", "Y", kind)) == []
    assert contention(driver("U1", "SIG", kind=kind), driver("U2", "SIG", kind=kind)) == []


def test_multi_unit_ic_wired_in_parallel_is_quiet():
    gates = part("U1", "74HC04", "Package_SO:SOIC-14", ("2", "SIG", "1Y", "output"), ("4", "SIG", "2Y", "output"),
                 ("7", "GND", "GND", "power_in"), ("14", "+3V3", "VCC", "power_in"))
    assert contention(gates) == []


def test_outputs_behind_a_series_resistor_are_different_nets_and_quiet():
    assert contention(driver("U1", "A"), driver("U2", "B"), res("R1", "A", "B", "100")) == []


def test_wired_or_flag_lines_and_pull_ups_are_quiet():
    assert contention(driver("U1", "IRQ", "INT"), driver("U2", "IRQ", "ALERT")) == []
    assert contention(driver("U1", "SIG"), driver("U2", "SIG"), res("R1", "SIG", "+3V3", "10k")) == []


def test_output_pins_on_supply_or_ground_nets_are_quiet():
    assert contention(driver("U1", "+3V3", "VOUT"), driver("U2", "+3V3", "VOUT")) == []


def test_dnp_driver_is_quiet():
    off = driver("U2", "SIG")
    off = type(off)(off.reference, off.value, off.footprint, off.pins, off.fields, dnp=True)
    assert contention(driver("U1", "SIG"), off) == []
