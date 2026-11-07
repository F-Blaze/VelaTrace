"""Round-6 precision: connector documentation labels, decoupling.far over all pads of a net, low-current parts."""
import pytest

from test_audit_rules import cap, design, exp4_spare, part, rules


def r6_found(prefix, *parts):
    return [(f.rule, f.refs) for f in rules(design(*parts), prefix)]


def r6_header(label, name="Pin_32"):
    return part("J2", "Raspberry_Pi_2_3", "Connector_PinSocket_2.54mm:PinSocket_2x20", ("32", label, name))


@pytest.mark.parametrize("label", [
    "/GPIO[12]/PWM0", "/GPIO[16]", "/GPIO[2]/SDA1", "/GPIO[7]/SPI0.CE1", "/GPIO[18]/PCM.CLK", "GPIO_17", "GPIO4",
    "ID_SD", "ID_SC", "TXD", "RXD", "IO_5", "D13", "A0", "MOSI", "SCLK"])
def test_r6_connector_standard_function_label_is_documentation(label):
    assert r6_found("net.single_pin", r6_header(label)) == []


def test_r6_functional_connector_label_and_non_connector_labels_stay_flagged():
    assert r6_found("net.single_pin", r6_header("MOTOR_EN")) == [("net.single_pin", ("J2",))]
    assert r6_found("net.single_pin", r6_header("/sheet/MOTOR_EN")) == [("net.single_pin", ("J2",))]
    chip = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "+3V3", "VDD"), ("2", "GND", "GND"),
                ("3", "TXD", "PB10"), *exp4_spare())
    assert [f.rule for f in rules(design(chip, cap()), "net")] == ["net.single_pin"]


def r6_ldo(pad_pos=((10.0, 10.0), (14.0, 10.0))):
    """Supply pad 2 plus a second, unnamed pad of the same net (like a SOT-223 tab)."""
    return part("U1", "XYZ-123", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", ("1", "GND", "GND", "power_in"),
                ("2", "+3V3", "VDD", "power_in", pad_pos[0]), ("3", "VIN", "VI", "passive"),
                ("2", "+3V3", "", "", pad_pos[1]), *exp4_spare())


def r6_chip(value="MCU", pins=2):
    return part("U1", value, "Package_QFP:LQFP-32", ("1", "+3V3", "VDD", "power_in", (10.0, 10.0)),
                ("2", "GND", "GND", "power_in", (10.0, 12.0)), *exp4_spare(pins))


def r6_load():
    return part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "+3V3"), ("2", "GND"))


def test_r6_decoupling_far_measures_from_the_nearest_pad_on_the_net():
    near_tab = cap("C1", pin_pos=((16.0, 10.0), (17.0, 10.0)))  # 2 mm from the tab pad, 6 mm from pin 2
    assert r6_found("decoupling.far", r6_ldo(), r6_load(), near_tab) == []
    far = cap("C1", pin_pos=((30.0, 10.0), (31.0, 10.0)))
    assert r6_found("decoupling.far", r6_ldo(), r6_load(), far) == [("decoupling.far", ("U1", "C1"))]


def test_r6_decoupling_far_still_reported_when_every_pad_is_far():
    chip = part("U1", "ESP32-S3", "Package_DFN_QFN:QFN-56", ("2", "+3V3_RF", "VDD3P3", "power_in", (10.0, 10.0)),
                ("3", "+3V3_RF", "VDD3P3", "power_in", (10.0, 11.0)), ("4", "GND", "GND", "power_in"),
                *exp4_spare(4))
    c8 = cap("C8", "+3V3_RF", pin_pos=((17.4, 10.0), (18.4, 10.0)))
    assert r6_found("decoupling.far", chip, r6_load(), c8) == [("decoupling.far", ("U1", "C8"))]


@pytest.mark.parametrize("value", ["74LVC1G00", "SN74LVC1G08", "NC7SZ00", "MAX809", "APX803", "TPS3809", "STM811", "MCP121"])
def test_r6_low_current_parts_skip_far_but_not_missing(value):
    far = cap("C1", pin_pos=((22.0, 10.0), (23.0, 10.0)))
    assert r6_found("decoupling.far", r6_chip(value), far) == []
    assert r6_found("decoupling.missing", r6_chip(value)) == [("decoupling.missing", ("U1",))]


def test_r6_big_or_ordinary_parts_keep_far():
    far = cap("C1", pin_pos=((22.0, 10.0), (23.0, 10.0)))
    assert r6_found("decoupling.far", r6_chip("W25Q128"), far) == [("decoupling.far", ("U1", "C1"))]
    assert r6_found("decoupling.far", r6_chip("74LVC1G00", pins=6), far) == [("decoupling.far", ("U1", "C1"))]
