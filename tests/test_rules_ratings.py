"""Rating and common-circuit rules: each fires every time the mistake is on the board and stays quiet otherwise."""
import pytest

from test_audit_rules import cap, design, part, res, rules
from velatrace.findings import Severity


def found(prefix, *parts):
    return [(f.rule, f.refs) for f in rules(design(*parts), prefix)]


# --- cap.voltage_rating -----------------------------------------------------------------------

@pytest.mark.parametrize("value,rail", [("10u_0402_X5R_6V3", "+12V"), ("22uF/6.3V", "+12V"), ("100n 16V", "+24V"),
                                        ("10uF 10V", "+12V"), ("1u 6.3V", "12V"), ("10u_6V3", "/pwr/+12V"),
                                        ("10u 4V", "+5V"), ("10u 4V", "VBUS"), ("10u 6V3", "+7V5")])
def test_cap_rating_fires_below_rail(value, rail):
    hits = rules(design(cap("C1", rail, "GND", value=value)), "cap.voltage_rating")
    assert [(f.severity, f.refs) for f in hits] == [(Severity.ERROR, ("C1",))]


def test_cap_rating_fires_with_voltage_field_and_either_pin_order():
    low = part("C1", "10uF", "Capacitor_SMD:C_0603", ("1", "GND"), ("2", "+12V"), fields={"Rated Voltage": "10V"})
    assert found("cap.voltage_rating", low) == [("cap.voltage_rating", ("C1",))]
    low = part("C1", "10uF", "Capacitor_SMD:C_0603", ("1", "+12V"), ("2", "GND"), fields={"Voltage": "6V3"})
    assert found("cap.voltage_rating", low) == [("cap.voltage_rating", ("C1",))]


@pytest.mark.parametrize("value,rail", [("10u 6V3", "+5V"), ("10u 6.3V", "VBUS"), ("10u 16V", "+12V"),
                                        ("10u 12V", "+12V"), ("10u", "+12V"), ("10u 6V3", "+VSYS"),
                                        ("10u 6V3", "Net-(U1-VDD)"), ("10u 25V", "+24V")])
def test_cap_rating_quiet(value, rail):
    assert found("cap.voltage_rating", cap("C1", rail, "GND", value=value)) == []


def test_cap_rating_quiet_between_two_rails_dnp_or_conflicting_voltages():
    assert found("cap.voltage_rating", cap("C1", "+12V", "+5V", value="10u 6V3")) == []
    dnp = part("C1", "10u 6V3", "Capacitor_SMD:C_0603", ("1", "+12V"), ("2", "GND"))
    assert found("cap.voltage_rating", dnp.__class__(**{**dnp.__dict__, "dnp": True})) == []
    mixed = part("C1", "10u 6V3", "Capacitor_SMD:C_0603", ("1", "+12V"), ("2", "GND"), fields={"Voltage": "25V"})
    assert found("cap.voltage_rating", mixed) == []


# --- resistor.power_rating --------------------------------------------------------------------

def led(ref="D1", a="LA", k="GND", value="LED"):
    return part(ref, value, "LED_SMD:LED_0603_1608Metric", ("1", k, "K"), ("2", a, "A"))


def sized(ref, a, b, value, size):
    return part(ref, value, f"Resistor_SMD:R_{size}_1005Metric", ("1", a), ("2", b))


def test_power_rating_fires_on_the_anode_side_with_arithmetic():
    parts = (sized("R1", "+12V", "LA", "220", "0402"), led())
    hits = rules(design(*parts), "resistor.power_rating")
    assert [(f.severity, f.refs, f.nets) for f in hits] == [(Severity.WARNING, ("R1", "D1"), ("+12V",))]
    assert "46 mA" in hits[0].evidence and "0.47 W" in hits[0].evidence


def test_power_rating_fires_on_the_cathode_side():
    parts = (led(a="+12V", k="LK"), sized("R1", "LK", "GND", "220", "0402"))
    assert found("resistor.power_rating", *parts) == [("resistor.power_rating", ("R1", "D1"))]


@pytest.mark.parametrize("size,ohms,volts", [("0201", "100", "+5V"), ("0603", "100", "+12V"), ("0805", "100", "+12V"),
                                            ("1206", "47", "+12V"), ("0402", "33R", "+5V")])
def test_power_rating_fires_for_each_package(size, ohms, volts):
    assert found("resistor.power_rating", sized("R1", volts, "LA", ohms, size), led()) == [
        ("resistor.power_rating", ("R1", "D1"))]


def test_power_rating_blue_led_uses_the_higher_forward_voltage():
    blue = led(value="LED_Blue")
    assert found("resistor.power_rating", sized("R1", "+5V", "LA", "100", "0402"), blue) == []  # 0.048 W
    assert found("resistor.power_rating", sized("R1", "+5V", "LA", "100", "0402"), led()) != []  # 0.102 W
    assert found("resistor.power_rating", sized("R1", "+5V", "LA", "47", "0402"), blue) != []  # 0.103 W


@pytest.mark.parametrize("r,size,rail", [("1k", "0402", "+5V"), ("330", "0402", "+3V3"), ("220", "0603", "+5V"),
                                        ("150", "2512", "+12V"), ("220", "0402", "Net-(U1-X)"),
                                        ("0", "0402", "+5V"), ("220", "0402", "+1V8")])
def test_power_rating_quiet_when_within_rating_or_unknown(r, size, rail):
    assert found("resistor.power_rating", sized("R1", rail, "LA", r, size), led()) == []


def test_power_rating_quiet_for_tht_unknown_package_stated_power_and_shared_nets():
    tht = part("R1", "220", "Resistor_THT:R_Axial_DIN0207", ("1", "+12V"), ("2", "LA"))
    assert found("resistor.power_rating", tht, led()) == []
    odd = part("R1", "220", "Resistor_SMD:R_Custom", ("1", "+12V"), ("2", "LA"))
    assert found("resistor.power_rating", odd, led()) == []
    assert found("resistor.power_rating", sized("R1", "+12V", "LA", "220 0.5W", "0402"), led()) == []
    # LED anode net also reaches a transistor: not a plain chain
    gate = part("Q1", "BC847", "Package_TO_SOT_SMD:SOT-23", ("1", "LA", "C"), ("2", "GND", "E"), ("3", "X", "B"))
    assert found("resistor.power_rating", sized("R1", "+12V", "LA", "220", "0402"), led(), gate) == []


def test_power_rating_quiet_when_led_pins_are_unnamed_or_cathode_not_grounded():
    bare = part("D1", "LED", "LED_SMD:LED_0603_1608Metric", ("1", "GND"), ("2", "LA"))
    assert found("resistor.power_rating", sized("R1", "+12V", "LA", "220", "0402"), bare) == []
    assert found("resistor.power_rating", sized("R1", "+12V", "LA", "220", "0402"), led(k="SIG")) == []


# --- usb.vbus_capacitance ---------------------------------------------------------------------

def usb(footprint="Connector_USB:USB_C_Receptacle_GCT_USB4105", value="USB_C_Receptacle"):
    return part("J1", value, footprint, ("A4", "VBUS", "VBUS", "passive"), ("A1", "GND", "GND", "passive"))


def test_vbus_fires_above_ten_microfarad_with_summed_caps():
    parts = (usb(), cap("C1", "VBUS", "GND", value="10uF"), cap("C2", "VBUS", "GND", value="4.7uF"))
    hits = rules(design(*parts), "usb.vbus_capacitance")
    assert [(f.severity, f.refs, f.nets) for f in hits] == [
        (Severity.INFO, ("J1", "C1", "C2"), ("VBUS",))]
    assert "14.7" in hits[0].evidence


@pytest.mark.parametrize("fp,value", [("Connector_USB:USB_Micro-B_Molex", "USB_B_Micro"),
                                      ("Connector_USB:USB_C_Receptacle", "USB4105"),
                                      ("Connector_USB:USB_Mini-B", "USB_B_Mini")])
def test_vbus_fires_for_each_device_connector(fp, value):
    assert found("usb.vbus_capacitance", usb(fp, value), cap("C1", "VBUS", "GND", value="22uF")) == [
        ("usb.vbus_capacitance", ("J1", "C1"))]


def test_vbus_quiet_at_exactly_ten_microfarad_and_without_caps():
    assert found("usb.vbus_capacitance", usb(), cap("C1", "VBUS", "GND", value="10uF")) == []
    assert found("usb.vbus_capacitance", usb(), cap("C1", "VBUS", "GND", value="4.7uF"),
                 cap("C2", "VBUS", "GND", value="5.3uF")) == []
    assert found("usb.vbus_capacitance", usb()) == []


def test_vbus_quiet_with_load_switch_or_other_driver():
    big = cap("C1", "VBUS", "GND", value="22uF")
    switch = part("U1", "TPS2051", "Package_SO:SOIC-8", ("1", "VBUS", "IN", "power_in"), ("2", "GND", "GND"))
    assert found("usb.vbus_capacitance", usb(), big, switch) == []
    fet = part("U2", "Whatever", "Package_SO:SOIC-8", ("1", "VBUS", "VIN", "power_in"))
    assert found("usb.vbus_capacitance", usb(), big, fet) != []  # unknown part is not an inrush limiter
    efuse = part("U2", "TPS25940", "Package_SO:SOIC-8", ("1", "VBUS", "VIN", "power_in"))
    assert found("usb.vbus_capacitance", usb(), big, efuse) == []
    hub = part("U3", "Hub", "Package_SO:SOIC-8", ("1", "VBUS", "VBUSOUT", "power_out"))
    assert found("usb.vbus_capacitance", usb(), big, hub) == []


def test_vbus_quiet_for_host_connector_dnp_caps_and_caps_behind_a_series_part():
    big = cap("C1", "VBUS", "GND", value="22uF")
    assert found("usb.vbus_capacitance", usb("Connector_USB:USB_A_Receptacle", "USB_A"), big) == []
    behind = cap("C1", "VBUS_F", "GND", value="22uF")
    ferrite = part("FB1", "BLM18", "Inductor_SMD:L_0603", ("1", "VBUS"), ("2", "VBUS_F"))
    assert found("usb.vbus_capacitance", usb(), behind, ferrite) == []
    dnp = big.__class__(**{**big.__dict__, "dnp": True})
    assert found("usb.vbus_capacitance", usb(), dnp) == []
    other = part("J2", "Conn_01x02", "Connector_PinHeader:PinHeader_1x02", ("1", "VBUS", "VBUS"), ("2", "GND", "GND"))
    assert found("usb.vbus_capacitance", other, big) == []


# --- inductive.flyback_missing ----------------------------------------------------------------

def relay(coil=("COIL+", "COIL-"), nets=("+5V", "SW"), ref="K1", value="G5LE-1", footprint="Relay_THT:Relay_SPDT"):
    return part(ref, value, footprint, ("1", nets[0], coil[0]), ("2", nets[1], coil[1]),
                ("3", "COMN", "COM"), ("4", "NON", "NO"), ("5", "NCN", "NC"))


def nmos(ref="Q1", d="SW"):
    return part(ref, "2N7002", "Package_TO_SOT_SMD:SOT-23", ("1", "GATE", "G"), ("2", "GND", "S"), ("3", d, "D"))


def diode(a="SW", k="+5V", ref="D1"):
    return part(ref, "1N4148", "Diode_SMD:D_SOD-123", ("1", k, "K"), ("2", a, "A"))


@pytest.mark.parametrize("coil", [("COIL+", "COIL-"), ("A1", "A2"), ("+", "-"), ("1", "2"), ("COIL1", "COIL2")])
def test_flyback_fires_for_each_coil_spelling(coil):
    assert found("inductive.flyback_missing", relay(coil), nmos()) == [("inductive.flyback_missing", ("K1", "Q1"))]


@pytest.mark.parametrize("ref,value", [("RL1", "SRD-05VDC"), ("K2", "G5LE"), ("X1", "Relay_DPDT")])
def test_flyback_fires_for_each_relay_spelling(ref, value):
    assert found("inductive.flyback_missing", relay(ref=ref, value=value), nmos()) == [
        ("inductive.flyback_missing", (ref, "Q1"))]


def test_flyback_fires_with_a_bjt_collector_and_reports_error():
    bjt = part("Q1", "BC547", "Package_TO_SOT_SMD:SOT-23", ("1", "B", "B"), ("2", "GND", "E"), ("3", "SW", "C"))
    hits = rules(design(relay(), bjt), "inductive.flyback_missing")
    assert [(f.severity, f.nets) for f in hits] == [(Severity.ERROR, ("SW", "+5V"))]


@pytest.mark.parametrize("clamp", [diode(), diode("+5V", "SW"), diode("SW", "+5V", "D5"),
                                   part("D2", "SMAJ12A", "Diode_SMD:D_SMA", ("1", "SW", "K"), ("2", "+5V", "A")),
                                   part("D3", "1N4148", "Diode_SMD:D_SOD-123", ("1", "SW", "A"), ("2", "+12V", "K"))])
def test_flyback_quiet_with_a_diode(clamp):
    assert found("inductive.flyback_missing", relay(), nmos(), clamp) == []


def test_flyback_quiet_with_a_snubber_capacitor():
    assert found("inductive.flyback_missing", relay(), nmos(), cap("C1", "SW", "+5V", value="100nF")) == []


def test_flyback_quiet_for_driver_ics_modules_and_solid_state_relays():
    uln = part("U1", "ULN2003A", "Package_DIP:DIP-16", ("1", "IN", "I1"), ("16", "SW", "O1"))
    assert found("inductive.flyback_missing", relay(), uln) == []
    ic = part("U1", "MCU", "Package_QFP:LQFP-32", ("1", "SW", "PA1"))
    assert found("inductive.flyback_missing", relay(), ic) == []
    assert found("inductive.flyback_missing", relay(value="Relay module 5V"), nmos()) == []
    for value in ("SSR-25DA", "AQY212", "G3MB-202P", "PhotoMOS", "TLP3547"):
        assert found("inductive.flyback_missing", relay(value=value), nmos()) == []


def test_flyback_quiet_without_named_coil_pins_or_a_transistor():
    unnamed = part("K1", "G5LE", "Relay_THT:Relay_SPDT", ("1", "+5V", "~"), ("2", "SW", "~"), ("3", "C", "~"))
    assert found("inductive.flyback_missing", unnamed, nmos()) == []
    plain = part("K1", "G5LE", "Relay_THT:Relay_SPDT", ("1", "+5V", "1"), ("2", "SW", "2"))
    assert found("inductive.flyback_missing", plain, nmos()) == []  # numbers only count beside COM/NO/NC
    assert found("inductive.flyback_missing", relay()) == []
    gate = part("Q1", "2N7002", "Package_TO_SOT_SMD:SOT-23", ("1", "SW", "G"), ("2", "GND", "S"), ("3", "X", "D"))
    assert found("inductive.flyback_missing", relay(), gate) == []


# --- pin.power_unconnected --------------------------------------------------------------------

def chip(*pins, value="MCU", ref="U1"):
    return part(ref, value, "Package_QFP:LQFP-48", ("1", "+3V3", "VDD", "power_in"), ("2", "GND", "VSS", "power_in"),
                *pins)


@pytest.mark.parametrize("net", ["", "unconnected-(U1-VDDA-Pad3)"])
def test_power_pin_fires_when_open(net):
    hits = rules(design(chip(("3", net, "VDDA", "power_in"))), "pin.power_unconnected")
    assert [(f.severity, f.refs) for f in hits] == [(Severity.ERROR, ("U1",))]
    assert "VDDA" in hits[0].title


def test_power_pin_groups_per_ic_and_fires_for_power_in_with_other_flags():
    ic = chip(("3", "", "VDDA", "power_in"), ("4", "unconnected-(U1-VSSA)", "VSSA", "power_in"),
              ("5", "", "VDD", "power_in+passive"))
    hits = rules(design(ic), "pin.power_unconnected")
    assert len(hits) == 1 and "VDDA" in hits[0].title and "VSSA" in hits[0].title


def test_power_pin_quiet_for_flags_optional_names_non_ics_and_pcb_sources():
    assert found("pin.power_unconnected", chip(("3", "", "VDDA", "no_connect"))) == []
    assert found("pin.power_unconnected", chip(("3", "", "VDDA", "power_in+no_connect"))) == []
    for name in ("VBAT", "VBACKUP", "VRTC", "VPP", "VBAT1", "NC"):
        assert found("pin.power_unconnected", chip(("3", "", name, "power_in"))) == []
    assert found("pin.power_unconnected", chip(("3", "unconnected-(U1-VDDA)", "VDDA", "power_out"))) == []
    module = part("U2", "ESP32-WROOM-MODULE", "RF_Module:ESP32-WROOM-32", ("1", "", "3V3", "power_in"),
                  ("2", "GND", "GND", "power_in"))
    assert found("pin.power_unconnected", module) == []
    conn = part("J1", "Conn", "Connector_PinHeader:PinHeader_1x02", ("1", "", "VCC", "power_in"),
                ("2", "GND", "GND", "power_in"))
    assert found("pin.power_unconnected", conn) == []
    untyped = part("U1", "MCU", "Package_QFP:LQFP-48", ("1", "+3V3", "VDD"), ("2", "", "VDDA"))
    assert found("pin.power_unconnected", untyped) == []


def test_power_pin_quiet_for_dnp_and_connected_pins():
    ic = chip(("3", "", "VDDA", "power_in"))
    assert found("pin.power_unconnected", ic.__class__(**{**ic.__dict__, "dnp": True})) == []
    assert found("pin.power_unconnected", chip(("3", "+3V3A", "VDDA", "power_in"))) == []
