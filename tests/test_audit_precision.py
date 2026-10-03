"""Precision fixes from the benchmark adjudication: small synthetic snapshots modelled on real
board cases (no third-party netlists). Each fix has a still-fires and a now-silent case."""
from decimal import Decimal

import pytest

from velatrace.audit_rules import parse_resistance
from velatrace.findings import Severity

from test_audit_rules import cap, design, ic, led, part, res, rules


# --- parse_resistance -------------------------------------------------------------------

@pytest.mark.parametrize("text,ohms", [("2,2k", 2200), ("4,7k", 4700), ("2,2 kΩ", 2200), ("10 kΩ", 10000),
                                       ("100Ω", 100), ("0.2mR", Decimal("0.0002")), ("1,5", Decimal("1.5")), ("10k, 1%", 10000),
                                       ("100,1%", None), ("4R7", Decimal("4.7")), ("2,2nF", None)])
def test_parse_resistance_european_and_unit_forms(text, ohms):
    assert parse_resistance(text) == (None if ohms is None else Decimal(ohms))


# --- led.no_resistor --------------------------------------------------------------------

def test_led_resistor_on_shared_rail_does_not_count_as_series():
    # Seeded case: series R removed, LED now straight across +3V3/GND; other loads' resistors
    # on +3V3 used to silence the rule.
    pullup = res("R1", "+3V3", "SDA")
    found = rules(design(ic(), cap(), pullup, led("D1", "+3V3", "GND")), "led")
    assert [(f.severity, f.refs) for f in found] == [(Severity.ERROR, ("D1",))]
    series = design(ic(), cap(), pullup, led("D1", "Net-(D1-A)", "GND"), res("R2", "+3V3", "Net-(D1-A)", "1k"))
    assert rules(series, "led") == []


def test_led_zero_ohm_series_link_is_not_a_limiter():
    mcu = ic("U1", ("5", "STATE_LED", "GPIO25"))
    zero = design(mcu, cap(), led("D1", "STATE_LED", "Net-(D1-K)"), res("R9", "Net-(D1-K)", "GND", "0"))
    found = rules(zero, "led")
    assert [(f.severity, f.refs) for f in found] == [(Severity.WARNING, ("D1", "R9", "U1"))]
    real = design(mcu, cap(), led("D1", "STATE_LED", "Net-(D1-K)"), res("R9", "Net-(D1-K)", "GND", "330"))
    assert rules(real, "led") == []


def test_led_gpio_drive_with_cathode_on_ground_shared_with_resistors():
    mcu = ic("U1", ("5", "BLUE_LED", "P0.12"))
    found = rules(design(mcu, cap(), res("R1", "Net-(U1-X)", "GND"), led("D1", "BLUE_LED", "GND")), "led")
    assert [f.severity for f in found] == [Severity.WARNING]


def test_led_on_current_sink_driver_pin_is_fine():
    driver = ic("U2", ("5", "Net-(D1-K)", "OUT0"), value="TLC5947")
    assert rules(design(driver, cap(), led("D1", "+5V", "Net-(D1-K)")), "led") == []


# --- decoupling.missing -----------------------------------------------------------------

def typed_ic(ref, *pins, value="CHIP"):
    """pins: (number, net, name, type); GND pin added."""
    return part(ref, value, "Package_SO:SOIC-8", ("99", "GND", "GND", "power_in"), *pins)


def decoupling(*parts):
    return [(f.refs, f.nets) for f in rules(design(*parts), "decoupling.missing")]


def test_switch_node_and_enable_pins_typed_power_are_not_rails():
    buck = typed_ic("U1", ("1", "+5V", "VIN", "power_in"), ("2", "Net-(L1-Pad1)", "SW", "power_out"),
                    ("3", "BUCK-EN", "EN", "power_in"), ("4", "Net-(U1-DCC)", "DCC", "power_out"),
                    ("5", "Net-(U1-LX)", "LX", "power_out"))
    assert decoupling(buck, cap("C1", "+5V")) == []
    assert decoupling(buck) == [(("U1",), ("+5V",))]


def test_power_out_counts_only_on_a_named_rail_that_feeds_something():
    ldo = typed_ic("U1", ("1", "VIN", "VIN", "power_in"), ("2", "+3V3", "VOUT", "power_out"))
    load = part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "+3V3"), ("2", "GND"))
    assert decoupling(ldo, cap("C1", "VIN"), load) == [(("U1",), ("+3V3",))]
    pump = typed_ic("U1", ("1", "VIN", "VIN", "power_in"), ("2", "Net-(U1-VS+)", "VS+", "power_out"),
                    ("3", "unconnected-(U1-Vref-Pad5)", "Vref", "power_out+no_connect"))
    assert decoupling(pump, cap("C1", "VIN"), cap("C2", "Net-(U1-VS+)", "+5V")) == []


def test_cap_through_fuse_ferrite_or_shunt_decouples():
    reg = typed_ic("U1", ("1", "/IN_P", "VI", "power_in"))
    bulk = cap("C1", "Net-(C1-Pad1)", value="2200uF")
    fuse = part("F1", "1A", "Fuse:Fuse_1206", ("1", "Net-(C1-Pad1)"), ("2", "/IN_P"))
    assert decoupling(reg, bulk, fuse) == []
    bead = part("FB1", "600R@100MHz", "Inductor_SMD:L_0603", ("1", "Net-(C1-Pad1)"), ("2", "/IN_P"))
    assert decoupling(reg, bulk, bead) == []
    shunt = res("R1", "Net-(C1-Pad1)", "/IN_P", "0.2mR")
    assert decoupling(reg, bulk, shunt) == []
    real_r = res("R1", "Net-(C1-Pad1)", "/IN_P", "10")
    assert decoupling(reg, bulk, real_r) == [(("U1",), ("/IN_P",))]


def test_cap_to_the_ics_own_power_or_ground_pin_net_decouples():
    stacked = typed_ic("U1", ("1", "/VDD1_0", "VDD1", "power_in"), ("2", "/VDD2_0", "VDD2", "power_in"))
    assert decoupling(stacked, cap("C1", "/VDD1_0", "/VDD2_0"), cap("C2", "/VDD2_0")) == []
    dw01 = part("U5", "DW01A", "Package_TO_SOT_SMD:SOT-23-6", ("6", "B-", "GND", "power_in"),
                ("5", "Net-(U5-VCC)", "VCC", "power_in"))
    assert decoupling(dw01, cap("C7", "Net-(U5-VCC)", "B-"), cap("C9", "+BATT")) == []
    assert decoupling(dw01, cap("C9", "+BATT")) == [(("U5",), ("Net-(U5-VCC)",))]


def test_battery_nets_and_strapped_or_bias_pins_are_not_rails():
    mod = typed_ic("U1", ("1", "bat+", "BAT+", "power_in"))
    cell = part("BT1", "LiPo", "Battery:BatteryHolder", ("1", "bat+"), ("2", "GND"))
    assert decoupling(mod, cell) == []
    efuse = typed_ic("U1", ("1", "Net-(U1-VDD_EFUSE)", "VDD_EFUSE", "power_in"))
    assert decoupling(efuse, res("R34", "Net-(U1-VDD_EFUSE)", "GND", "22R")) == []
    vint = typed_ic("U3", ("26", "VINT", "VINT", "power_in"))
    assert decoupling(vint, res("R28", "VINT", "LDO1SET", "2k2")) == []
    rc_filter = typed_ic("U1", ("1", "Net-(U1-AVDD)", "AVDD", "power_in"))
    assert decoupling(rc_filter, res("R1", "+3V3", "Net-(U1-AVDD)", "10")) == [(("U1",), ("Net-(U1-AVDD)",))]


def test_addressable_leds_are_checked_like_ics():
    def pixel(ref):
        return part(ref, "SK6812MINI-E", "LED_SMD:LED_SK6812MINI", ("1", "+5V", "VDD", "power_in"),
                    ("2", "Net-(D1-DOUT)", "DOUT", "output"), ("3", "GND", "VSS", "power_in"),
                    ("4", "DIN", "DIN", "input"))
    assert decoupling(pixel("D1"), pixel("D2")) == [(("D1", "D2"), ("+5V",))]
    assert decoupling(pixel("D1"), pixel("D2"), cap("C1", "+5V")) == []


# --- net.single_pin / net.label_scope ---------------------------------------------------

def test_single_pin_skips_no_connect_and_dangling_outputs():
    def one(kind):
        return rules(design(ic("U1", ("3", "/GPIO23", "GPIO23", kind)), cap()), "net")
    assert [f.severity for f in one("bidirectional")] == [Severity.WARNING]
    assert one("bidirectional+no_connect") == []
    assert one("output") == [] and one("power_out") == []


def test_label_split_across_scopes_is_reported_once():
    soc = ic("U1", ("3", "NMI", "NMI", "input"))
    pmic = ic("U3", ("7", "/Power/NMI", "IRQ", "output"), value="AXP209")
    pull = res("R1", "/Power/NMI", "+3V3")
    found = rules(design(soc, pmic, pull, cap()), "net")
    assert [(f.rule, f.severity, f.nets) for f in found] == [
        ("net.label_scope", Severity.WARNING, ("NMI", "/Power/NMI"))]
    # Repeated sheets whose same-named labels all dangle are plain single-pin nets.
    twins = design(ic("U1", ("3", "/A/OUT", "IN1")), ic("U2", ("3", "/B/OUT", "IN1")), cap())
    assert {f.rule for f in rules(twins, "net")} == {"net.single_pin"}


def test_many_dangling_labels_on_one_part_are_grouped():
    pins = [(str(n), f"MIPI_D{n}", f"D{n}") for n in range(3, 10)]
    found = rules(design(ic("U1", *pins), cap()), "net")
    assert [(f.rule, f.severity, len(f.nets)) for f in found] == [("net.single_pin", Severity.INFO, 7)]
    few = rules(design(ic("U1", *pins[:4]), cap()), "net")
    assert [f.severity for f in few] == [Severity.WARNING] * 4


# --- pin.input_floating -----------------------------------------------------------------

def test_floating_inputs_skip_trim_pins_esd_arrays_and_displays():
    def chip(value, footprint, pin_name):
        return part("U1", value, footprint, ("1", "+5V", "V+", "power_in"), ("2", "GND", "V-", "power_in"),
                    ("8", "unconnected-(U1-X-Pad8)", pin_name, "input"))
    assert rules(design(chip("OP07", "Package_SO:SOIC-8", "VOS")), "pin") == []
    assert rules(design(chip("USBLC6-4", "Package_TO_SOT_SMD:SOT-23-6", "IO2")), "pin") == []
    assert rules(design(chip("D168K", "Display_7Segment:D1X8K", "DP")), "pin") == []
    assert [f.refs for f in rules(design(chip("OP07", "Package_SO:SOIC-8", "IN+")), "pin")] == [("U1",)]


# --- connector.power_unprotected --------------------------------------------------------

def header(ref, *nets, value="Conn", footprint="Connector_PinHeader_2.54mm:PinHeader_1x04"):
    return part(ref, value, footprint, *[(str(n), net) for n, net in enumerate(nets, 1)])


def connector(*parts):
    return [(f.refs, f.nets) for f in rules(design(*parts), "connector")]


def test_rail_made_on_board_behind_ferrite_or_buck_inductor_is_an_output():
    ldo = typed_ic("U1", ("1", "+5V", "VIN", "power_in"), ("2", "+3V3", "VOUT", "power_out"))
    bead = part("FB1", "600R@100MHz", "Inductor_SMD:L_0603", ("1", "+3V3"), ("2", "+3.3VA"))
    adc = typed_ic("U2", ("1", "+3.3VA", "VDDA", "power_in"))
    assert connector(ldo, bead, adc, header("J2", "+3.3VA", "GND")) == []
    buck = typed_ic("U3", ("1", "+5V", "VIN", "power_in"), ("2", "Net-(L1-Pad1)", "SW", "power_out"))
    coil = part("L1", "3.3uH", "Inductor_SMD:L_1210", ("1", "Net-(L1-Pad1)"), ("2", "+3V3"))
    mcu = typed_ic("U4", ("1", "+3V3", "VDD", "power_in"))
    assert connector(buck, coil, mcu, header("J4", "+3V3", "GND")) == []
    switch = typed_ic("U5", ("1", "+5V", "IN", "power_in"), ("2", "+5V_OTG", "OUT", "output"))
    assert connector(switch, typed_ic("U6", ("1", "+5V_OTG", "VBUS", "power_in")),
                     header("J5", "+5V_OTG", "GND")) == []


def test_boost_input_through_its_inductor_is_still_an_input():
    boost = typed_ic("U6", ("1", "+12V", "VIN", "power_in"), ("2", "Net-(L1-Pad2)", "SW", "power_out"))
    coil = part("L1", "100uH", "Inductor_SMD:L_1210", ("1", "+12V"), ("2", "Net-(L1-Pad2)"))
    assert connector(boost, coil, header("J1", "+12V", "GND")) == [(("J1",), ("+12V",))]


def test_power_entry_on_several_connectors_is_one_finding_led_by_the_biggest():
    load = typed_ic("U1", ("1", "VBUS", "VDD", "power_in"))
    usb = header("J1", "VBUS", "GND", "D+", "D-", "CC1", "CC2", footprint="Connector_USB:USB_C_Receptacle")
    socket = header("J3", "VBUS", "GND", value="Ext_Cap")
    assert connector(load, usb, socket) == [(("J1", "J3"), ("VBUS",))]


def test_not_power_entries_are_skipped():
    load = typed_ic("U1", ("1", "Net-(J2-VDD)", "VDD", "power_in"))
    card = part("J2", "Micro_SD_Card", "Connector_Card:microSD_HC_Hirose_DM3AT", ("4", "Net-(J2-VDD)", "VDD", "power_in"),
                ("6", "GND", "VSS", "power_in"))
    assert connector(load, card) == []
    nc = part("J3", "USB_C", "Connector_USB:USB_C", ("A4", "VBUS"), ("B4", "unconnected-(J3-VBUS-PadB4)", "VBUS", "power_in"),
              ("A1", "GND"))
    fused = part("F1", "500mA", "Fuse:Fuse_1206", ("1", "VBUS"), ("2", "+5V"))
    assert connector(nc, fused, typed_ic("U1", ("1", "+5V", "VDD", "power_in"))) == []
    divider = design(res("R4", "+3V3", "/1.2V", "1.2k"), res("R5", "/1.2V", "/0.65V", "820"),
                     header("J2", "/1.2V", "/0.65V", "GND"))
    assert rules(divider, "connector") == []


def test_isolated_converter_minus_counts_as_return():
    conv = typed_ic("U7", ("2", "/mornsun_Vin", "Vin", "input"), ("1", "/mornsun_minus", "GND", "input"))
    block = header("J5", "/mornsun_minus", "/mornsun_Vin", footprint="Connector_Phoenix_MSTB:MSTBA_2")
    ldo = typed_ic("U6", ("3", "/mornsun_Vin", "VI", "power_in"))
    assert connector(conv, ldo, block) == [(("J5",), ("/mornsun_Vin",))]


def test_battery_socket_fires_even_when_the_charger_drives_the_battery_net():
    pmic = typed_ic("U1", ("1", "+BATT", "VBAT", "power_out"), ("2", "VBUS", "VBUS", "power_in"))
    socket = header("J4", "+BATT", "GND", value="JST_PH_2")
    assert connector(pmic, socket) == [(("J4",), ("+BATT",))]


# --- I2C pull-ups -----------------------------------------------------------------------

def i2c_found(*parts):
    return [(f.rule, f.severity) for f in rules(design(cap(), *parts), "i2c")]


def test_missing_pullup_on_plug_in_board_is_info_but_on_board_controller_still_warns():
    eeprom = ic("U1", ("3", "/SDA", "SDA"), value="24C02")
    host = header("J1", "/SDA", "GND", "+3V3")
    assert i2c_found(eeprom, host) == [("i2c.pullup.missing", Severity.INFO)]
    mcu = ic("U2", ("5", "/SDA", "GPIO21"), value="ESP32-S3")
    assert i2c_found(eeprom, mcu, host) == [("i2c.pullup.missing", Severity.WARNING)]
    # Two-device on-board bus relying on MCU internal pull-ups: still a warning (user decision).
    gauge = ic("U6", ("3", "BAT_I2C_SDA", "SDA"), value="MAX17048")
    nrf = ic("U1", ("5", "BAT_I2C_SDA", "P0.04"), value="nRF52840")
    found = rules(design(cap(), gauge, nrf), "i2c")
    assert [(f.severity, "internal pull-ups" in f.fix) for f in found] == [(Severity.WARNING, True)]


def compute_module(net="/IO/SDA"):
    return part("Module301", "ComputeModule5", "CM5:CM5", ("58", net, "GPIO2"), ("1", "GND", "GND"))


def test_board_pullup_in_parallel_with_known_module_pullup():
    sensor = ic("U1", ("6", "/IO/SDA", "SDA"), value="LIS3DH")
    found = rules(design(cap(), sensor, compute_module(), res("R501", "/IO/SDA", "+3V3", "2k2")), "i2c")
    assert [(f.rule, f.severity, f.refs) for f in found] == [
        ("i2c.pullup.combined", Severity.WARNING, ("R501", "Module301"))]
    assert "990 Ω" in found[0].title
    assert i2c_found(sensor, compute_module(), res("R501", "/IO/SDA", "+3V3", "10k")) == []
    # The module's pull-ups alone are enough: no "missing" finding.
    assert i2c_found(sensor, compute_module()) == []


def test_pi_header_pullups_with_moderate_hat_pullup_are_fine():
    hat = part("J2", "Raspberry_Pi", "RaspberryPi:HAT_connector", ("3", "SDA", "SDA/GPIO2"), ("6", "GND"))
    rtc = ic("U1", ("5", "SDA", "SDA"), value="MCP7940N")
    assert i2c_found(hat, rtc, res("R1", "SDA", "+3V3", "5k1")) == []
    assert i2c_found(hat, rtc, res("R1", "SDA", "+3V3", "1k5")) == [("i2c.pullup.combined", Severity.WARNING)]
