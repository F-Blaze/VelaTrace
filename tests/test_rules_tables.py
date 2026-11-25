"""Table rules: each fires every time the mistake is on the board and stays quiet otherwise."""
import pytest

from test_audit_rules import cap, design, part, res, rules
from velatrace.findings import Severity

SOT = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
HEADER = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"


def found(prefix, *parts):
    return [(f.rule, f.refs) for f in rules(design(*parts), prefix)]


# --- power.divider_voltage --------------------------------------------------------------------

def regulator(value, fb_name, out, top, bottom, fb="Net-(U1-FB)", extra=()):
    """Regulator U1 with a divider R1 (out -> fb) and R2 (fb -> GND), plus any extra parts on the feedback net."""
    ic = part("U1", value, SOT, ("1", fb, fb_name, "input"), ("2", "GND", "GND", "power_in"), ("3", "12V", "VIN", "power_in"))
    return (ic, res("R1", out, fb, top), res("R2", fb, "GND", bottom), *extra)


@pytest.mark.parametrize("value,pin,out,top,bottom", [
    ("LM317", "ADJ", "5V", "240", "390"),            # 1.25 x (1 + 390/240) = 3.3 V
    ("LM317T", "ADJ", "12V", "240", "390"),
    ("AMS1117-ADJ", "ADJ", "5V", "120", "180"),      # 1.25 x (1 + 180/120) = 3.1 V
    ("LM2596S-ADJ", "FB", "5V", "10k", "10k"),       # 1.23 x 2 = 2.46 V
    ("TPS5430", "VSENSE", "5V", "10k", "10k"),       # 1.221 x 2 = 2.44 V
    ("MP1584EN", "FB", "5V", "10k", "10k"),          # 0.8 x 2 = 1.6 V
    ("MP1584EN", "FB", "3V3", "10k", "1.91k"),       # 0.8 x (1 + 10/1.91) = 5.0 V
])
def test_divider_fires_when_resistors_contradict_the_net_voltage(value, pin, out, top, bottom):
    hits = rules(design(*regulator(value, pin, out, top, bottom)), "power.divider_voltage")
    assert [(f.rule, f.severity, f.refs, f.nets) for f in hits] == [
        ("power.divider_voltage", Severity.WARNING, ("U1", "R1", "R2"), (out, "Net-(U1-FB)"))]


@pytest.mark.parametrize("value,pin,out,top,bottom", [
    ("LM317", "ADJ", "3V3", "240", "390"),           # 3.28 V
    ("LM317", "ADJ", "+5V", "240", "750"),           # 5.16 V
    ("AMS1117-ADJ", "ADJ", "3V3", "120", "180"),     # 3.1 V is within 5 % of 3.3 V after the adjust-pin current
    ("LM2596S-ADJ", "FB", "5V", "31.6k", "10k"),     # 1.23 x 4.16 = 5.12 V
    ("TPS5430", "VSENSE", "+3V3", "17.4k", "10k"),   # 1.221 x 2.74 = 3.35 V
    ("MP1584EN", "FB", "5V", "10k", "1.91k"),
    ("MP1584EN", "FB", "1V8", "10k", "8k"),          # 0.8 x 2.25 = 1.8 V
])
def test_divider_quiet_when_the_output_matches_the_net_name(value, pin, out, top, bottom):
    assert found("power.divider_voltage", *regulator(value, pin, out, top, bottom)) == []


def test_divider_quiet_when_the_net_name_has_no_voltage():
    for out in ("VOUT", "VCC", "Net-(L1-Pad2)", "OUT_ADJ"):
        assert found("power.divider_voltage", *regulator("MP1584EN", "FB", out, "10k", "10k")) == []


def test_divider_quiet_when_the_net_name_states_two_voltages():
    assert found("power.divider_voltage", *regulator("MP1584EN", "FB", "5V_3V3", "10k", "10k")) == []


def test_divider_quiet_when_the_divider_has_extra_parts():
    pot = part("RV1", "10k", "Potentiometer_SMD:Pot", ("1", "Net-(U1-FB)"), ("2", "GND"))
    third = res("R3", "Net-(U1-FB)", "+3V3", "100k")
    diode = part("D1", "1N4148", "Diode_SMD:D_SOD-123", ("1", "Net-(U1-FB)"), ("2", "GND"))
    for extra in (pot, third, diode):
        assert found("power.divider_voltage", *regulator("MP1584EN", "FB", "5V", "10k", "10k", extra=(extra,))) == []


def test_divider_quiet_for_an_unlisted_family():
    assert found("power.divider_voltage", *regulator("XYZ1234", "FB", "5V", "10k", "10k")) == []
    assert found("power.divider_voltage", *regulator("LM1117-ADJ", "ADJ", "5V", "10k", "10k")) == []


def test_divider_quiet_when_a_resistor_is_not_fitted_or_unreadable():
    ic, top, bottom = regulator("MP1584EN", "FB", "5V", "10k", "10k")
    dnp = part("R2", "10k", "Resistor_SMD:R_0402", ("1", "Net-(U1-FB)"), ("2", "GND"))
    object.__setattr__(dnp, "dnp", True)
    assert found("power.divider_voltage", ic, top, dnp) == []
    assert found("power.divider_voltage", ic, top, res("R2", "Net-(U1-FB)", "GND", "TBD")) == []


def test_divider_feed_forward_capacitor_across_the_top_resistor_does_not_hide_a_wrong_divider():
    extra = (cap("C9", "5V", "Net-(U1-FB)", "22pF"),)
    assert found("power.divider_voltage", *regulator("MP1584EN", "FB", "5V", "10k", "10k", extra=extra)) == [
        ("power.divider_voltage", ("U1", "R1", "R2"))]


# --- power.ldo_dropout ------------------------------------------------------------------------

def ldo(value, vin, ref="U1", out="Net-(U1-VOUT)", in_name="VIN", out_name="VOUT"):
    return part(ref, value, SOT, ("1", "GND", "GND", "power_in"), ("2", out, out_name, "power_out"), ("3", vin, in_name, "power_in"))


@pytest.mark.parametrize("value,vin", [
    ("AMS1117-3.3", "3V3"),                # no headroom at all
    ("AMS1117-3.3", "4V"),                 # 0.7 V < 1.0 V
    ("AMS1117-5.0", "5V"),
    ("AMS1117-1.8", "2V5"),
    ("MCP1700T-3302E/TT", "3V4"),          # 0.1 V < 0.178 V
    ("MCP1700T-3002E/TT", "3V"),
    ("AP2112K-3.3", "+3.4V"),              # 0.1 V < 0.25 V
    ("HT7333", "3V35"),
])
def test_ldo_fires_when_headroom_is_below_dropout(value, vin):
    hits = rules(design(ldo(value, vin)), "power.ldo_dropout")
    assert [(f.rule, f.severity, f.refs) for f in hits] == [("power.ldo_dropout", Severity.WARNING, ("U1",))]
    assert "headroom" in hits[0].evidence


@pytest.mark.parametrize("value,vin", [
    ("AMS1117-3.3", "24V"),                # above 15 V
    ("AMS1117-5.0", "+16V"),
    ("MCP1700T-3302E/TT", "9V"),           # above 6 V
    ("XC6206P332MR", "12V"),
    ("AP2112K-3.3", "7V4"),
    ("HT7333", "24V"),                     # above 12 V
])
def test_ldo_fires_when_the_input_is_above_the_maximum(value, vin):
    hits = rules(design(ldo(value, vin)), "power.ldo_dropout")
    assert [(f.rule, f.refs) for f in hits] == [("power.ldo_dropout", ("U1",))]
    assert "maximum" in hits[0].title


def test_ldo_takes_the_input_voltage_from_an_upstream_fixed_regulator():
    first = ldo("AMS1117-3.3", "5V", ref="U1", out="Net-(U1-VOUT)")      # 5 V -> 3.3 V is fine
    second = ldo("AP2112K-3.3", "Net-(U1-VOUT)", ref="U2", out="3V3")     # 3.3 V -> 3.3 V is not
    assert found("power.ldo_dropout", first, second) == [("power.ldo_dropout", ("U2",))]


@pytest.mark.parametrize("value,vin", [
    ("AMS1117-3.3", "5V"),
    ("AMS1117-3.3", "4V3"),                # exactly 1.0 V of headroom
    ("AMS1117-3.3", "15V"),                # exactly the maximum
    ("AMS1117-5.0", "12V"),
    ("MCP1700T-3302E/TT", "3V7"),          # LiPo into a 178 mV part
    ("MCP1700T-3302E/TT", "5V"),
    ("XC6206P332MR", "5V"),
    ("AP2112K-3.3", "5V"),
    ("HT7333", "5V"),
    ("HT7333", "12V"),
])
def test_ldo_quiet_with_enough_headroom_and_a_legal_input(value, vin):
    assert found("power.ldo_dropout", ldo(value, vin)) == []


def test_ldo_quiet_when_the_input_voltage_is_not_stated():
    for vin in ("VBAT", "VIN", "Net-(J1-Pad1)", "VBUS", "BATT+"):
        assert found("power.ldo_dropout", ldo("AMS1117-3.3", vin)) == []
    connector = part("J1", "Conn", HEADER, ("1", "Net-(J1-Pad1)"), ("2", "GND"))
    assert found("power.ldo_dropout", ldo("AMS1117-3.3", "Net-(J1-Pad1)"), connector) == []


def test_ldo_quiet_for_parts_that_are_not_in_the_table_or_have_no_known_output():
    assert found("power.ldo_dropout", ldo("LD1117S33TR", "30V")) != []   # in the table: 15 V maximum input
    for value in ("AMS1117", "AMS1117-ADJ", "HT7333-1", "HT7333-2", "XC6206", "MCP1700", "AP2112K-ADJ", "LM1117-3.3"):
        assert found("power.ldo_dropout", ldo(value, "3V3")) == []
        assert found("power.ldo_dropout", ldo(value, "30V")) == []


def test_ldo_quiet_when_a_pin_is_missing_or_input_and_output_share_a_net():
    ic = part("U1", "AMS1117-3.3", SOT, ("1", "GND", "GND"), ("3", "3V3", "VIN"))
    assert found("power.ldo_dropout", ic) == []
    assert found("power.ldo_dropout", ldo("AMS1117-3.3", "3V3", out="3V3")) == []


def test_ldo_ap2112_dropout_is_only_judged_for_the_3v3_type():
    assert found("power.ldo_dropout", ldo("AP2112K-1.8", "2V")) == []
    assert found("power.ldo_dropout", ldo("AP2112K-1.8", "7V")) == [("power.ldo_dropout", ("U1",))]


# --- mcu.strap_pins ---------------------------------------------------------------------------

def esp(value="ESP32-WROOM-32", *pins):
    return part("U1", value, "RF_Module:ESP32-WROOM-32", ("1", "GND", "GND", "power_in"), ("2", "+3V3", "3V3", "power_in"), *pins)


def header(ref, net):
    return part(ref, "Conn", HEADER, ("1", net), ("2", "GND"))


@pytest.mark.parametrize("name", ["SD0", "SD1", "SD2", "SD3", "CMD", "CLK", "SHD/SD2", "SWP/SD3", "SCS/CMD", "SCK/CLK", "SDO/SD0", "SDI/SD1"])
def test_esp32_module_flash_pin_shared_with_a_connector_fires(name):
    parts = (esp("ESP32-WROOM-32", ("17", "USER_IO", name, "bidirectional")), header("J1", "USER_IO"))
    assert found("mcu.strap_pins", *parts) == [("mcu.strap_pins", ("U1",))]


@pytest.mark.parametrize("name", ["SD_CLK", "SD_CMD", "SD_DATA_0", "SD_DATA_3", "GPIO6", "GPIO11"])
def test_esp32_chip_flash_pin_wired_to_a_non_flash_part_fires(name):
    chip = esp("ESP32-D0WD-V3", ("30", "SPI_X", name, "bidirectional"))
    led = part("D1", "LED", "LED_SMD:LED_0603", ("1", "SPI_X"), ("2", "GND"))
    assert found("mcu.strap_pins", chip, led) == [("mcu.strap_pins", ("U1",))]


def test_esp32_flash_pins_quiet_when_wired_to_the_flash_or_nothing():
    flash = part("U2", "W25Q32JVSS", "Package_SO:SOIC-8", ("1", "CS", "CS#"), ("6", "CLK_NET", "CLK"), ("2", "GND", "GND"))
    chip = esp("ESP32-D0WD-V3", ("30", "CLK_NET", "SD_CLK", "bidirectional"), ("31", "CS", "SD_CMD", "bidirectional"),
               ("32", "Net-(U1-SD_DATA_0)", "SD_DATA_0", "bidirectional"))
    assert found("mcu.strap_pins", chip, flash, res("R1", "CLK_NET", "+3V3", "10k")) == []
    assert found("mcu.strap_pins", esp("ESP32-WROOM-32", ("17", "Net-(U1-SD2)", "SHD/SD2"))) == []


def test_esp32_flash_pins_quiet_when_an_unrecognised_memory_part_is_on_them():
    spi = part("U2", "FRAM-X", "Package_SO:SOIC-8", ("1", "CS", "CS#"), ("3", "WPN_NET", "WP#"), ("7", "HOLD_NET", "HOLD#"))
    chip = esp("ESP32-D0WD-V3", ("30", "WPN_NET", "SD_DATA_2", "bidirectional"))
    assert found("mcu.strap_pins", chip, spi) == []


def test_esp32_gpio0_tied_to_ground_fires_and_a_button_does_not():
    assert found("mcu.strap_pins", esp("ESP32-WROOM-32", ("25", "GND", "IO0", "bidirectional"))) == [("mcu.strap_pins", ("U1",))]
    button = part("SW1", "BOOT", "Button_Switch_SMD:SW_SPST", ("1", "BOOT_N"), ("2", "GND"))
    assert found("mcu.strap_pins", esp("ESP32-WROOM-32", ("25", "BOOT_N", "IO0", "bidirectional")), button,
                 res("R1", "BOOT_N", "+3V3", "10k")) == []


@pytest.mark.parametrize("pull", ["tie", "pullup"])
def test_esp32_wroom_gpio12_pulled_high_fires(pull):
    parts = [esp("ESP32-WROOM-32", ("14", "+3V3" if pull == "tie" else "IO12_NET", "IO12", "bidirectional"))]
    if pull == "pullup":
        parts.append(res("R1", "IO12_NET", "+3V3", "10k"))
    assert found("mcu.strap_pins", *parts) == [("mcu.strap_pins", ("U1",))]


def test_esp32_gpio12_quiet_when_low_open_or_a_strong_divider_or_not_a_wroom_32():
    pin = ("14", "IO12_NET", "IO12", "bidirectional")
    assert found("mcu.strap_pins", esp("ESP32-WROOM-32", pin), res("R1", "IO12_NET", "GND", "10k")) == []
    assert found("mcu.strap_pins", esp("ESP32-WROOM-32", pin), res("R1", "IO12_NET", "+3V3", "10k"),
                 res("R2", "IO12_NET", "GND", "10k")) == []
    assert found("mcu.strap_pins", esp("ESP32-WROOM-32", pin), res("R1", "IO12_NET", "+3V3", "1M")) == []
    assert found("mcu.strap_pins", esp("ESP32-WROOM-32", ("14", "Net-(U1-IO12)", "IO12"))) == []
    assert found("mcu.strap_pins", esp("ESP32-D0WD-V3", ("14", "IO12_NET", "MTDI"), ), res("R1", "IO12_NET", "+3V3", "10k")) == []


def test_other_esp_families_are_not_judged():
    for value in ("ESP32-S3-WROOM-1", "ESP32-C3-MINI-1", "ESP8266", "ESP32-S2-MINI-1"):
        assert found("mcu.strap_pins", esp(value, ("25", "GND", "IO0", "bidirectional"), ("17", "X", "SD0"), ), header("J1", "X")) == []


def rp2040(*pins):
    return part("U3", "RP2040", "Package_DFN_QFN:QFN-56-1EP_7x7mm", ("1", "+3V3", "IOVDD", "power_in"), ("2", "GND", "GND", "power_in"), *pins)


def test_rp2040_qspi_ss_tied_to_ground_fires():
    assert found("mcu.strap_pins", rp2040(("56", "GND", "QSPI_SS", "bidirectional"))) == [("mcu.strap_pins", ("U3",))]
    assert found("mcu.strap_pins", rp2040(("56", "GND", "~{QSPI_SS}", "bidirectional"))) == [("mcu.strap_pins", ("U3",))]


def test_rp2040_qspi_ss_quiet_with_flash_or_bootsel_button():
    flash = part("U4", "W25Q16JVUXIQ", "Package_SON:USON-8", ("1", "FLASH_CS", "CS#"), ("4", "GND", "GND"))
    assert found("mcu.strap_pins", rp2040(("56", "FLASH_CS", "QSPI_SS", "bidirectional")), flash) == []
    button = part("SW1", "BOOTSEL", "Button_Switch_SMD:SW_SPST", ("1", "FLASH_CS"), ("2", "GND"))
    assert found("mcu.strap_pins", rp2040(("56", "FLASH_CS", "QSPI_SS", "bidirectional")), flash, button,
                 res("R9", "FLASH_CS", "BTN", "1k")) == []
    assert found("mcu.strap_pins", rp2040(("56", "Net-(U3-QSPI_SS)", "QSPI_SS"))) == []
