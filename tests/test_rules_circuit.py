"""Circuit rules: each fires every time the mistake is on the board and stays quiet otherwise."""
import pytest

from test_audit_rules import design, part, rules
from velatrace.findings import Severity


def found(prefix, *parts):
    return [(f.rule, f.refs) for f in rules(design(*parts), prefix)]


def sized(ref, a, b, value, size="0402"):
    return part(ref, value, f"Resistor_SMD:R_{size}_1005Metric", ("1", a), ("2", b))


def led(ref="D1", a="LA", k="GND", value="LED", footprint="LED_SMD:LED_0603_1608Metric", fields=None):
    return part(ref, value, footprint, ("1", k, "K"), ("2", a, "A"), fields=fields)


# --- led.current_high -------------------------------------------------------------------------

def test_led_current_fires_with_arithmetic():
    hits = rules(design(sized("R1", "+5V", "LA", "100"), led(value="LED_Red")), "led.current_high")
    assert [(f.severity, f.refs, f.nets) for f in hits] == [(Severity.WARNING, ("D1", "R1"), ("+5V",))]
    assert "32 mA" in hits[0].evidence and "(5 V - 1.8 V) / 100 Ω" in hits[0].evidence


@pytest.mark.parametrize("rail,ohms,value", [("+5V", "100", "red"), ("+3V3", "33", "yellow"), ("+12V", "220", "red"),
                                            ("+5V", "47R", "blue"), ("+5V", "47", "LED"), ("VBUS", "100", "Red LED")])
def test_led_current_fires_anode_side(rail, ohms, value):
    assert found("led.current_high", sized("R1", rail, "LA", ohms), led(value=value)) == [
        ("led.current_high", ("D1", "R1"))]


def test_led_current_fires_cathode_side():
    parts = (led(a="+12V", k="LK", value="red"), sized("R1", "LK", "GND", "220"))
    assert found("led.current_high", *parts) == [("led.current_high", ("D1", "R1"))]


@pytest.mark.parametrize("rail,ohms,value", [("+3V3", "50", "red"),  # exactly 30 mA
                                            ("+5V", "330", "red"), ("+3V3", "100", "green"), ("+5V", "100", "LED"),
                                            ("+5V", "100", "blue"), ("GPIO", "10", "red"), ("Net-(U1-X)", "10", "red")])
def test_led_current_quiet(rail, ohms, value):
    assert found("led.current_high", sized("R1", rail, "LA", ohms), led(value=value)) == []


@pytest.mark.parametrize("kw", [dict(value="1W red"), dict(value="LED", fields={"Description": "3W power LED Cree XP-E"}),
                                dict(value="LED", fields={"Type": "COB"}),
                                dict(footprint="LED_SMD:LED_5050_5050Metric"),
                                dict(footprint="LED_SMD:LED_Power_X")])
def test_led_current_quiet_for_power_leds(kw):
    assert found("led.current_high", sized("R1", "+5V", "LA", "47"), led(**kw)) == []


def test_led_current_quiet_for_sinks_branches_and_zero_ohm():
    sink = (led(a="+5V", k="LK"), part("Q1", "2N7002", "SOT-23", ("1", "G", "G"), ("2", "GND", "S"), ("3", "LK", "D")))
    assert found("led.current_high", *sink) == []
    fed = (sized("R1", "+5V", "LA", "47"), led(), sized("R2", "LA", "GND", "10k"))
    assert found("led.current_high", *fed) == []
    assert found("led.current_high", sized("R1", "+5V", "LA", "0"), led(value="red")) == []


# --- logic.level_mismatch ---------------------------------------------------------------------

def ch340(net="UART_RX", vcc="+5V", extra=()):
    return part("U1", "CH340C", "Package_SO:SOIC-16", ("1", vcc, "VCC", "power_in"), ("2", "GND", "GND", "power_in"),
                ("3", net, "TXD", "output"), *extra)


def esp(net="UART_RX", value="ESP32-WROOM-32", rail="+3V3", kind="input", footprint="RF_Module:ESP32-WROOM-32"):
    return part("U2", value, footprint, ("1", rail, "3V3", "power_in"), ("2", "GND", "GND", "power_in"),
                ("3", net, "RXD0", kind))


def test_level_mismatch_fires_on_the_example():
    hits = rules(design(ch340(), esp()), "logic.level_mismatch")
    assert [(f.severity, f.refs, f.nets) for f in hits] == [(Severity.WARNING, ("U1", "U2"), ("UART_RX",))]
    assert "5 V" in hits[0].evidence and "3.3 V" in hits[0].evidence


@pytest.mark.parametrize("value,kind", [("ESP32-S3-WROOM-1", "input"), ("ESP8266EX", "bidirectional"),
                                        ("RP2040", "bidirectional"), ("RP2350A", "input"),
                                        ("nRF52840", "bidirectional"), ("ATSAMD21G18", "bidirectional"),
                                        ("ATSAMD51J19", "input"), ("ESP-WROOM-02", "input"), ("RP2040", "input")])
def test_level_mismatch_fires_for_each_family(value, kind):
    receiver = esp(value=value, kind=kind, footprint="Package_DFN_QFN:QFN-56")
    assert found("logic.level_mismatch", ch340(), receiver) == [("logic.level_mismatch", ("U1", "U2"))]


def test_level_mismatch_fires_from_a_vdd_part_on_vbus():
    driver = part("U1", "SOME-IC", "SOT-23", ("1", "VBUS", "VDD", "power_in"), ("2", "GND", "VSS", "power_in"),
                  ("3", "N", "OUT", "output"))
    assert found("logic.level_mismatch", driver, esp(net="N")) == [("logic.level_mismatch", ("U1", "U2"))]


def test_level_mismatch_quiet_with_anything_on_the_net():
    divider = [sized("R1", "UART_RX", "RX3", "1k"), sized("R2", "RX3", "GND", "2k")]
    assert found("logic.level_mismatch", ch340(), esp(net="RX3"), *divider) == []
    assert found("logic.level_mismatch", ch340(), esp(net="RX3"), sized("R1", "UART_RX", "RX3", "4.7k")) == []
    assert found("logic.level_mismatch", ch340(), esp(), sized("R1", "UART_RX", "GND", "10k")) == []
    diode = part("D1", "BAT54", "SOT-23", ("1", "UART_RX"), ("2", "+3V3"))
    assert found("logic.level_mismatch", ch340(), esp(), diode) == []
    other = part("U3", "74LVC1G17", "SOT-23", ("1", "UART_RX", "A", "input"))
    assert found("logic.level_mismatch", ch340(), esp(), other) == []


def test_level_mismatch_quiet_for_safe_pairs():
    assert found("logic.level_mismatch", ch340(), esp(value="STM32F103C8T6", footprint="LQFP-48")) == []
    assert found("logic.level_mismatch", ch340(), esp(rail="+5V")) == []
    assert found("logic.level_mismatch", ch340(vcc="+3V3"), esp()) == []
    assert found("logic.level_mismatch", ch340(vcc="VCC_X"), esp()) == []
    assert found("logic.level_mismatch", ch340(), esp(rail="Net-(U2-3V3)")) == []
    assert found("logic.level_mismatch", ch340(), esp(kind="output")) == []


def test_level_mismatch_quiet_for_open_drain_untyped_shifters_and_io_supplies():
    od = part("U1", "CH340C", "SOIC-16", ("1", "+5V", "VCC", "power_in"), ("3", "UART_RX", "TXD", "open_collector"))
    assert found("logic.level_mismatch", od, esp()) == []
    untyped = part("U1", "CH340C", "SOIC-16", ("1", "+5V", "VCC"), ("3", "UART_RX", "TXD"))
    assert found("logic.level_mismatch", untyped, esp(kind="")) == []
    for value in ("TXB0104", "TXS0102", "SN74LVC245", "74AHCT125"):
        shifter = part("U1", value, "TSSOP-14", ("1", "+5V", "VCC", "power_in"), ("3", "UART_RX", "B1", "output"))
        assert found("logic.level_mismatch", shifter, esp()) == []
    ftdi = ch340(extra=[("9", "+3V3", "VCCIO", "power_in")])
    assert found("logic.level_mismatch", ftdi, esp()) == []


# --- mosfet.gate_pulldown_missing -------------------------------------------------------------

def mcu(net="GATE", kind="bidirectional", value="ESP32-WROOM-32"):
    return part("U1", value, "RF_Module:ESP32-WROOM-32", ("1", net, "IO5", kind))


def nmos(value="AO3400A", gate="G1", source="GND", fields=None):
    return part("Q1", value, "Package_TO_SOT_SMD:SOT-23", ("1", gate, "G"), ("2", source, "S"), ("3", "LOAD-", "D"),
                fields=fields)


@pytest.mark.parametrize("value", ["AO3400A", "IRLML6344", "2N7002", "BSS138", "SI2302CDS", "IRLZ44N", "IRF540N",
                                   "IRLML2502", "FDN337N", "Q_NMOS_GSD", "N-Channel MOSFET"])
def test_gate_pulldown_fires_direct(value):
    assert found("mosfet.gate_pulldown_missing", mcu("G1"), nmos(value=value)) == [
        ("mosfet.gate_pulldown_missing", ("Q1", "U1"))]


def test_gate_pulldown_fires_for_fields_and_series_resistor():
    hits = rules(design(mcu("G1"), nmos(value="IRFX", fields={"ki_description": "N-Channel MOSFET"})),
                 "mosfet.gate_pulldown_missing")
    assert [f.severity for f in hits] == [Severity.INFO]
    for ohms in ("100", "1k", "220R"):
        assert found("mosfet.gate_pulldown_missing", mcu("GATE"), sized("R1", "GATE", "G1", ohms), nmos()) == [
            ("mosfet.gate_pulldown_missing", ("Q1", "U1"))]
    assert found("mosfet.gate_pulldown_missing", mcu("GATE"), sized("R1", "G1", "GATE", "100"), nmos()) != []


def test_gate_pulldown_quiet_for_mcus_that_reset_with_pull_downs():
    for value in ("RP2040", "RP2350A", "Pico"):
        assert found("mosfet.gate_pulldown_missing", mcu("G1", value=value), nmos()) == [], value


def test_gate_pulldown_quiet_when_pulled_down_or_resistor_too_big():
    assert found("mosfet.gate_pulldown_missing", mcu("G1"), sized("R2", "G1", "GND", "10k"), nmos()) == []
    assert found("mosfet.gate_pulldown_missing", mcu("GATE"), sized("R1", "GATE", "G1", "100"),
                 sized("R2", "G1", "GND", "100k"), nmos()) == []
    assert found("mosfet.gate_pulldown_missing", mcu("GATE"), sized("R1", "GATE", "G1", "100"),
                 sized("R2", "GATE", "GND", "100k"), nmos()) == []
    assert found("mosfet.gate_pulldown_missing", mcu("GATE"), sized("R1", "GATE", "G1", "4.7k"), nmos()) == []


def test_gate_pulldown_quiet_for_other_drivers_and_parts():
    driver = part("U1", "UCC27517", "SOT-23", ("1", "G1", "OUT", "bidirectional"))
    assert found("mosfet.gate_pulldown_missing", driver, nmos()) == []
    assert found("mosfet.gate_pulldown_missing", mcu("G1", kind="output", value="74HC04"), nmos()) == []
    assert found("mosfet.gate_pulldown_missing", mcu("G1", kind=""), nmos()) == []
    assert found("mosfet.gate_pulldown_missing", mcu("G1"), nmos(value="AO3401", fields={"d": "P-Channel"})) == []
    assert found("mosfet.gate_pulldown_missing", mcu("G1"), nmos(value="Q_PMOS_GSD")) == []
    assert found("mosfet.gate_pulldown_missing", mcu("G1"), nmos(value="MMBT3904")) == []
    assert found("mosfet.gate_pulldown_missing", mcu("G1"), nmos(source="+5V")) == []
    conn = part("J1", "Conn_01x01", "Connector_PinHeader_2.54mm:PinHeader_1x01", ("1", "G1", "Pin_1", "passive"))
    assert found("mosfet.gate_pulldown_missing", conn, nmos()) == []


def test_gate_pulldown_quiet_for_level_shifter():
    shifter = part("Q1", "BSS138", "SOT-23", ("1", "3V3", "G"), ("2", "LV", "S"), ("3", "HV", "D"))
    assert found("mosfet.gate_pulldown_missing", mcu("3V3"), shifter) == []


# --- i2c.address_conflict ---------------------------------------------------------------------

def dev(ref, value, extra=(), sda="SDA", scl="SCL", sda_name="SDA", scl_name="SCL", footprint="Package_LGA:LGA-8"):
    return part(ref, value, footprint, ("1", sda, sda_name), ("2", scl, scl_name),
                ("3", "+3V3", "VDD", "power_in"), ("4", "GND", "GND", "power_in"), *extra)


def at(pin, net, number="9"):
    return (number, net, pin)


def conflicts(*parts):
    return rules(design(*parts), "i2c.address_conflict")


def test_i2c_classic_ds3231_and_mpu6050():
    hits = conflicts(dev("U1", "DS3231"), dev("U2", "MPU-6050", [at("AD0", "GND")]))
    assert [(f.severity, f.refs, f.nets) for f in hits] == [(Severity.ERROR, ("U1", "U2"), ("SDA", "SCL"))]
    assert "0x68" in hits[0].evidence and "AD0 on GND" in hits[0].evidence and "0x68" in hits[0].title


A3 = [at("A0", "GND"), at("A1", "GND", "10"), at("A2", "GND", "11")]
INA = [at("A0", "GND"), at("A1", "GND", "10")]


@pytest.mark.parametrize("a,b", [
    (("BME280", [at("SDO", "GND")]), ("BMP280", [at("SDO", "GND")])),
    (("BME280", [at("SDO", "+3V3")]), ("BMP280", [at("SDO", "VCC_IO")])),
    (("SHT31-DIS", [at("ADDR", "GND")]), ("SHT40-AD1B", [])),
    (("SHT31", [at("ADDR", "+3V3")]), ("SHT31", [at("ADDR", "VDD")])),
    (("SSD1306", [at("SA0", "GND")]), ("SH1106", [at("SA0", "GND")])),
    (("MPU9250", [at("AD0/SDO", "+3V3")]), ("ICM-20948", [at("AD0/SDO", "+3V3")])),
    (("DS3231", []), ("DS1307", [])),
    (("INA219", INA), ("INA226", INA)),
    (("INA219", [at("A0", "SDA"), at("A1", "GND", "10")]), ("INA219", [at("A0", "SDA"), at("A1", "GND", "10")])),
    (("ADS1115", [at("ADDR", "GND")]), ("ADS1115", [at("ADDR", "GND")])),
    (("ADS1115", [at("ADDR", "SCL")]), ("ADS1115", [at("ADDR", "SCL")])),
    (("24LC256", A3), ("AT24C02", A3)),
    (("MCP23017", [at("A0", "+3V3"), *A3[1:]]), ("MCP23017", [at("A0", "+3V3"), *A3[1:]])),
    (("VEML7700", []), ("VEML7700", [])), (("AHT20", []), ("AHT20", [])), (("SCD41", []), ("SCD40", [])),
    (("PCF8563", []), ("PCF8563", [])), (("BH1750", [at("ADDR", "GND")]), ("BH1750", [at("ADDR", "GND")])),
    (("BH1750", [at("ADDR", "+3V3")]), ("BH1750", [at("ADDR", "+3V3")])),
])
def test_i2c_conflict_fires(a, b):
    assert [f.refs for f in conflicts(dev("U1", a[0], a[1]), dev("U2", b[0], b[1]))] == [("U1", "U2")]


def test_i2c_conflict_fires_for_pca9685_and_alternate_pin_names():
    pins = [at(f"A{i}", "GND", str(10 + i)) for i in range(6)]
    assert len(conflicts(dev("U1", "PCA9685", pins), dev("U2", "PCA9685", pins))) == 1
    one = dev("U1", "BME280", [at("SDO", "GND")], sda_name="SDI", scl_name="SCK")
    two = dev("U2", "BME280", [at("SDO", "GND")], sda_name="SDA/SDI", scl_name="SCL/SCK")
    assert len(conflicts(one, two)) == 1


def test_i2c_three_parts_name_all_refs():
    hits = conflicts(dev("U1", "DS3231"), dev("U2", "DS1307"), dev("U3", "MPU6050", [at("AD0", "GND")]))
    assert [f.refs for f in hits] == [("U1", "U2", "U3")]


@pytest.mark.parametrize("a,b", [
    (("BME280", [at("SDO", "GND")]), ("BME280", [at("SDO", "+3V3")])),            # different SDO
    (("DS3231", []), ("MPU6050", [at("AD0", "+3V3")])),                           # 0x68 vs 0x69
    (("INA219", INA), ("INA219", [at("A0", "+3V3"), at("A1", "GND", "10")])),
    (("INA219", [at("A1", "GND", "10")]), ("INA219", [at("A1", "GND", "10")])),   # A0 floating
    (("INA219", [at("A0", "Net-(U1-A0)"), at("A1", "GND", "10")]),
     ("INA219", [at("A0", "Net-(U1-A0)"), at("A1", "GND", "10")])),               # A0 through an unknown part
    (("INA219", [at("A0", "SDA"), at("A1", "GND", "10")]), ("INA219", [at("A0", "SCL"), at("A1", "GND", "10")])),
    (("ADS1115", [at("ADDR", "GND")]), ("ADS1115", [at("ADDR", "+3V3")])),
    (("SHT31", []), ("SHT31", [at("ADDR", "+3V3")])),                             # ADDR open
    (("SHT31", [at("ADDR", "GND")]), ("SHT40-BD1B", [])),                          # B variant is 0x45
    (("BME280", [at("SDO", "SDA")]), ("BME280", [at("SDO", "SDA")])),              # signal net
    (("BH1750", [at("ADDR", "GND")]), ("BH1750", [at("ADDR", "+3V3")])),
    (("24LC256", A3), ("24LC256", [at("A0", "+3V3"), *A3[1:]])),
    (("24C04", [at("A1", "GND")]), ("24C04", [at("A1", "GND")])),                  # not in the table
    (("SSD1306 module", [at("SA0", "GND")]), ("SSD1306 module", [at("SA0", "GND")])),
])
def test_i2c_conflict_quiet(a, b):
    assert conflicts(dev("U1", a[0], a[1]), dev("U2", b[0], b[1])) == []


def test_i2c_conflict_quiet_on_different_buses_modules_and_dnp():
    assert conflicts(dev("U1", "DS3231"), dev("U2", "DS3231", sda="SDA2", scl="SCL2")) == []
    assert conflicts(dev("U1", "DS3231"), dev("U2", "DS3231", sda="SDA", scl="SCL2")) == []
    breakout = dev("U2", "DS3231", footprint="Module:DS3231_Module")
    assert conflicts(dev("U1", "DS3231"), breakout) == []
    off = dev("U2", "DS3231")
    off = off.__class__(**{**off.__dict__, "dnp": True})
    assert conflicts(dev("U1", "DS3231"), off) == []
    assert conflicts(dev("U1", "DS3231")) == []
