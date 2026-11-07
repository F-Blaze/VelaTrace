"""Precision of the experimental decoupling / I2C pull-up checks: tiny parts, regulators, modules, shared rails."""
from test_audit_rules import cap, design, exp4_spare, ic, part, res, rules


def exp4_rules(prefix, *parts):
    return [(f.rule, f.refs) for f in rules(design(*parts), prefix)]


# --- decoupling.missing: parts with three or fewer pins ---------------------------------

def test_exp4_three_pin_sensor_needs_no_bypass_but_a_three_pin_regulator_does():
    sensor = part("U1", "DS18B20", "Package_TO_SOT_THT:TO-92_Inline", ("1", "GND", "GND"), ("2", "DQ", "DQ"),
                  ("3", "+5V", "VDD"))
    assert exp4_rules("decoupling", sensor) == []
    ldo = part("U1", "AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", ("1", "GND", "GND", "power_in"),
               ("2", "+3V3", "VO", "power_out"), ("3", "VIN", "VI", "power_in"))
    load = part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "+3V3"), ("2", "GND"))
    assert exp4_rules("decoupling.missing", ldo, load, cap("C1", "+3V3")) == [("decoupling.missing", ("U1",))]
    four = part("U1", "SENSOR", "Package_SO:SOIC-8", ("1", "GND", "GND"), ("2", "DQ", "DQ"), ("3", "+5V", "VDD"),
                *exp4_spare(1))
    assert exp4_rules("decoupling", four) == [("decoupling.missing", ("U1",))]


# --- decoupling.far ---------------------------------------------------------------------

def exp4_chip(ref="U1", value="MCU", footprint="Package_QFP:LQFP-32", extra=()):
    return part(ref, value, footprint, ("1", "+3V3", "VDD", "power_in", (10.0, 10.0)),
                ("2", "GND", "GND", "power_in", (10.0, 12.0)), *extra, *exp4_spare())


def exp4_far_cap(ref="C1", net="+3V3", x=22.0, value="100nF"):
    return cap(ref, net, value=value, pin_pos=((x, 10.0), (x + 1, 10.0)))


def test_exp4_far_cap_is_reported_for_a_plain_ic_but_not_for_regulators_or_modules():
    assert exp4_rules("decoupling.far", exp4_chip(), exp4_far_cap()) == [("decoupling.far", ("U1", "C1"))]
    ldo = part("U1", "AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", ("1", "GND", "GND", "power_in"),
               ("2", "+3V3", "VO", "power_out", (10.0, 10.0)), ("3", "VIN", "VI", "power_in"))
    load = part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "+3V3"), ("2", "GND"))
    assert exp4_rules("decoupling.far", ldo, load, exp4_far_cap()) == []
    charger = exp4_chip(value="BQ25170", footprint="Package_SON:WSON-8")
    assert exp4_rules("decoupling.far", charger, exp4_far_cap()) == []
    module = exp4_chip(value="ESP32-S3-WROOM-1", footprint="bitaxe:ESP32-S3-WROOM-1")
    assert exp4_rules("decoupling.far", module, exp4_far_cap()) == []


def test_exp4_power_switch_and_bare_mcu_far_caps_stay_reported():
    switch = part("U1", "CH217K", "Package_TO_SOT_SMD:SOT-23-6", ("1", "GND", "GND", "power_in"),
                  ("2", "+3V3", "VOUT", "power_out", (10.0, 10.0)), ("3", "VIN", "VIN", "power_in"),
                  *exp4_spare())
    load = part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "+3V3"), ("2", "GND"))
    assert exp4_rules("decoupling.far", switch, load, exp4_far_cap()) == [("decoupling.far", ("U1", "C1"))]
    pico_like = exp4_chip(value="RP2040", footprint="Package_DFN_QFN:QFN-56")
    assert exp4_rules("decoupling.far", pico_like, exp4_far_cap()) == [("decoupling.far", ("U1", "C1"))]


def test_exp4_shared_well_decoupled_rail_is_not_judged_per_ic():
    near = [cap("C2", "+3V3", pin_pos=((11.0, 10.0), (12.0, 10.0))),
            cap("C3", "+3V3", pin_pos=((9.0, 10.0), (8.0, 10.0)))]
    far_ic = part("U2", "MCU", "Package_QFP:LQFP-32", ("1", "+3V3", "VDD", "power_in", (60.0, 10.0)),
                  ("2", "GND", "GND", "power_in", (60.0, 12.0)), *exp4_spare())
    bulk = cap("C4", "+3V3", value="10uF", pin_pos=((70.0, 10.0), (71.0, 10.0)))
    # U2's own pin is far from every cap, but the rail has two caps beside U1 and a bulk cap.
    assert exp4_rules("decoupling.far", exp4_chip(), far_ic, *near, bulk) == []
    # Without the bulk capacitor, or with only one nearby capacitor, the far IC is still reported.
    assert [r for r in exp4_rules("decoupling.far", exp4_chip(), far_ic, *near) if "U2" in r[1]]
    assert [r for r in exp4_rules("decoupling.far", exp4_chip(), far_ic, near[0], bulk) if "U2" in r[1]]


def test_exp4_rc_filtered_supply_pin_is_not_judged_by_distance():
    filtered = part("U1", "SGP40", "Package_DFN_QFN:DFN-6", ("1", "Net-(U1-VDD)", "VDD", "power_in", (10.0, 10.0)),
                    ("2", "GND", "GND", "power_in", (10.0, 12.0)), *exp4_spare())
    rc = res("R1", "+3V3", "Net-(U1-VDD)", "100")
    local = exp4_far_cap("C1", "Net-(U1-VDD)")
    assert exp4_rules("decoupling.far", filtered, rc, local) == []
    # A 10 kΩ divider/bias resistor on the net is not an RC supply filter.
    assert exp4_rules("decoupling.far", filtered, res("R1", "+3V3", "Net-(U1-VDD)", "10k"), local) == [
        ("decoupling.far", ("U1", "C1"))]


# --- i2c.pullup.missing: OLED / display modules -----------------------------------------

def exp4_bus(*parts):
    mcu = ic("U1", ("3", "SDA", "GPIO21"), ("4", "SCL", "GPIO22"), value="RP2040")
    return [(f.rule, f.severity) for f in rules(design(cap(), mcu, *parts), "i2c")]


def test_exp4_oled_module_counts_as_a_pullup_source_but_a_bare_panel_does_not():
    oled = part("U2", "~", "Library:128x64OLED", ("1", "GND", "GND"), ("2", "+3V3", "VCC"), ("3", "SCL", "SCL"),
                ("4", "SDA", "SDA"))
    sensor = ic("U3", ("3", "SDA", "SDA"), ("4", "SCL", "SCL"), value="BME280")
    assert exp4_bus(oled) == []
    assert [rule for rule, _ in exp4_bus(sensor)] == ["i2c.pullup.missing"] * 2
    assert exp4_bus(sensor, oled) == []
    bare = part("U2", "SSD1306", "Package_QFP:QFP-64", ("3", "SCL", "SCL"), ("4", "SDA", "SDA"))
    assert [rule for rule, _ in exp4_bus(bare)] == ["i2c.pullup.missing"] * 2
    panel = part("DS1", "OLED_FPC", "Connector_FFC-FPC:Hirose_FH12-30S", ("3", "SCL", "SCL"), ("4", "SDA", "SDA"))
    assert [rule for rule, _ in exp4_bus(panel)] == ["i2c.pullup.missing"] * 2


def test_exp4_board_pullups_beside_an_oled_module_stay_quiet():
    oled = part("U2", "SSD1306 0.96in", "Connector_PinHeader_2.54mm:PinHeader_1x04", ("3", "SCL", "SCL"),
                ("4", "SDA", "SDA"))
    pull = [res("R1", "SDA", "+3V3", "4.7k"), res("R2", "SCL", "+3V3", "4.7k")]
    assert exp4_bus(oled, *pull) == []
