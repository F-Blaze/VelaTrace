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
    parts = (usb(), cap("C1", "VBUS", "GND", value="10uF"), cap("C2", "VBUS", "GND", value="10uF"))
    hits = rules(design(*parts), "usb.vbus_capacitance")
    assert [(f.severity, f.refs, f.nets) for f in hits] == [
        (Severity.INFO, ("J1", "C1", "C2"), ("VBUS",))]
    assert "20" in hits[0].evidence
    # A small excess is within what ceramics lose at 5 V bias.
    assert rules(design(usb(), cap("C1", "VBUS", "GND", value="10uF"), cap("C2", "VBUS", "GND", value="1uF")),
                 "usb.vbus_capacitance") == []


def test_vbus_quiet_on_a_source_port_with_cc_pull_ups():
    source = part("J1", "USB_C_Receptacle", "Connector_USB:USB_C_Receptacle_GCT_USB4105", ("A4", "VBUS", "VBUS", "passive"),
                  ("A1", "GND", "GND", "passive"), ("A5", "/CC1", "CC1", "bidirectional"))
    rp = res("R5", "/CC1", "VBUS", "56k")
    assert rules(design(source, rp, cap("C1", "VBUS", "GND", value="22uF")), "usb.vbus_capacitance") == []
    rd = res("R5", "/CC1", "GND", "5.1k")
    assert [f.rule for f in rules(design(source, rd, cap("C1", "VBUS", "GND", value="22uF")), "usb.vbus_capacitance")] == [
        "usb.vbus_capacitance"]


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


def test_vbus_fires_even_with_a_load_switch_on_the_net_because_it_only_protects_what_is_behind_it():
    big = cap("C1", "VBUS", "GND", value="100uF")
    switch = part("U1", "TPS2051", "Package_SO:SOIC-8", ("1", "VBUS", "IN", "power_in"), ("2", "GND", "GND"))
    hits = rules(design(usb(), big, switch), "usb.vbus_capacitance")
    assert [(f.refs, f.nets) for f in hits] == [(("J1", "C1"), ("VBUS",))]
    efuse = part("U2", "TPS25940", "Package_SO:SOIC-8", ("1", "VBUS", "VIN", "power_in"))
    assert found("usb.vbus_capacitance", usb(), big, efuse) != []
    fet = part("U2", "Whatever", "Package_SO:SOIC-8", ("1", "VBUS", "VIN", "power_in"))
    assert found("usb.vbus_capacitance", usb(), big, fet) != []


def test_vbus_quiet_when_an_ic_output_drives_the_net():
    big = cap("C1", "VBUS", "GND", value="22uF")
    hub = part("U3", "Hub", "Package_SO:SOIC-8", ("1", "VBUS", "VBUSOUT", "power_out"))
    assert found("usb.vbus_capacitance", usb(), big, hub) == []


def test_vbus_quiet_for_host_connector_and_dnp_caps():
    big = cap("C1", "VBUS", "GND", value="22uF")
    assert found("usb.vbus_capacitance", usb("Connector_USB:USB_A_Receptacle", "USB_A"), big) == []
    assert found("usb.vbus_capacitance", usb("Connector_USB:USB_C_Receptacle", "USB_C_HOST_PORT"), big) == []
    assert found("usb.vbus_capacitance", usb("Connector_USB:USB_C_Receptacle", "USB_C_Downstream"), big) == []
    dnp = big.__class__(**{**big.__dict__, "dnp": True})
    assert found("usb.vbus_capacitance", usb(), dnp) == []
    other = part("J2", "Conn_01x02", "Connector_PinHeader:PinHeader_1x02", ("1", "VBUS", "VBUS"), ("2", "GND", "GND"))
    assert found("usb.vbus_capacitance", other, big) == []


def schottky(net_a="VBUS", net_k="+5V", value="SS14"):
    return part("D1", value, "Diode_SMD:D_SMA", ("1", net_k, "K"), ("2", net_a, "A"))


def test_vbus_counts_capacitors_behind_a_series_schottky_diode():
    caps = (cap("C1", "+5V", "GND", value="100uF"), cap("C2", "+5V", "GND", value="100uF"),
            cap("C3", "+5V", "GND", value="47uF"))
    hits = rules(design(usb(), schottky(), *caps), "usb.vbus_capacitance")
    assert [(f.severity, f.refs, f.nets) for f in hits] == [
        (Severity.INFO, ("J1", "D1", "C1", "C2", "C3"), ("VBUS",))]
    assert "247" in hits[0].evidence and "behind D1" in hits[0].evidence


def test_vbus_counts_direct_and_ferrite_or_fuse_hop_capacitors_together():
    ferrite = part("FB1", "BLM18", "Inductor_SMD:L_0603", ("1", "VBUS"), ("2", "VBUS_F"))
    hits = rules(design(usb(), ferrite, cap("C1", "VBUS", "GND", value="10uF"),
                         cap("C2", "VBUS_F", "GND", value="10uF")), "usb.vbus_capacitance")
    assert [f.refs for f in hits] == [("J1", "C1", "FB1", "C2")]
    fuse = part("F1", "500mA", "Fuse:Fuse_0603", ("1", "VBUS"), ("2", "VBUS_F"))
    assert found("usb.vbus_capacitance", usb(), fuse, cap("C2", "VBUS_F", "GND", value="22uF")) != []
    assert found("usb.vbus_capacitance", usb(), res("R1", "VBUS", "VBUS_F", "0.5"),
                 cap("C2", "VBUS_F", "GND", value="22uF")) != []


def test_vbus_quiet_for_small_diode_isolated_capacitance_and_non_series_parts():
    small = cap("C1", "+5V", "GND", value="8uF")
    assert found("usb.vbus_capacitance", usb(), schottky(), small) == []
    big = cap("C1", "+5V", "GND", value="100uF")
    assert found("usb.vbus_capacitance", usb(), schottky(net_a="+5V", net_k="VBUS"), big) == []  # reversed diode
    assert found("usb.vbus_capacitance", usb(), schottky(value="SMAJ5.0A"), big) == []  # TVS, not a rectifier
    assert found("usb.vbus_capacitance", usb(), res("R1", "VBUS", "+5V", "100"), big) == []  # not a <=1 ohm link
    switch = part("U1", "TPS22918", "Package_TO_SOT_SMD:SOT-23-5", ("1", "VBUS", "VIN", "power_in"),
                  ("2", "GND", "GND"), ("3", "+5V", "VOUT", "power_out"))
    assert found("usb.vbus_capacitance", usb(), switch, big) == []  # behind a switch, not a diode
    driven = part("U2", "Boost", "Package_SO:SOIC-8", ("1", "+5V", "VOUT", "power_out"))
    assert found("usb.vbus_capacitance", usb(), schottky(), driven, big) == []  # rail driven by an IC output


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


RAIL = part("U9", "LDO", "Package_SO:SOIC-8", ("1", "+3V3", "VOUT", "power_out"), ("2", "GND", "GND", "power_in"))


def fed(*parts):
    """Findings with a regulator driving +3V3, so only the pin under test can be cap-fed."""
    return found("pin.power_unconnected", RAIL, *parts)


def test_power_pin_fed_only_through_a_capacitor_fires():
    hits = rules(design(RAIL, chip(("3", "VBAT", "VBAT", "power_in")), cap("C9", "VBAT", "+3V3")), "pin.power_unconnected")
    assert [(f.severity, f.refs, f.nets) for f in hits] == [(Severity.ERROR, ("U1", "C9"), ("VBAT",))]
    assert "fed only through capacitor C9 (other end +3V3): no DC path" in hits[0].evidence
    # A cap to ground on a net that no name marks as a supply leaves the pin unfed too.
    assert fed(chip(("3", "Net-(U1-VDDIO)", "VDDIO", "power_in")),
                 cap("C9", "Net-(U1-VDDIO)", "GND")) == [("pin.power_unconnected", ("U1", "C9"))]


def test_power_pin_with_a_dc_path_or_a_regulator_pin_is_quiet():
    vbat = chip(("3", "VBAT", "VBAT", "power_in"))
    c9 = cap("C9", "VBAT", "+3V3")
    assert fed(vbat, c9, res("R9", "VBAT", "+3V3", "1k")) == []
    assert fed(vbat, c9, part("U2", "LDO", "Package_SO:SOIC-8",
                                                          ("1", "VBAT", "VOUT", "power_out"))) == []
    assert fed(vbat, c9, part("BT1", "Battery", "Battery:BatteryHolder",
                                                          ("1", "VBAT"), ("2", "GND"))) == []
    # A supply-named rail with only decoupling caps to ground is fed from outside the drawing.
    assert found("pin.power_unconnected", chip(("3", "+3V3", "VDD", "power_in")), cap("C1", "+3V3", "GND")) == []
    for name in ("VCAP_1", "VCAP", "VDDCORE", "VCORE", "REGOUT", "DEC1", "VDDCR", "LDO_OUT"):
        assert fed(chip(("3", "Net-(U1-X)", name, "power_in")),
                     cap("C9", "Net-(U1-X)", "GND")) == []
    assert fed(chip(("3", "VBAT", "VBAT", "power_out")), c9) == []
    dnp = c9.__class__(**{**c9.__dict__, "dnp": True})
    assert fed(vbat, dnp) == []
    module = part("U4", "ESP32-WROOM-MODULE", "RF_Module:ESP32-WROOM-32", ("1", "VBAT", "VBAT", "power_in"),
                  ("2", "GND", "GND", "power_in"))
    assert fed(module, c9) == []
    assert fed(chip(("3", "VBAT", "VBAT", "power_in+no_connect")), c9) == []


def test_power_unconnected_quiet_for_a_module_powered_on_another_pin():
    zero = part("U4", "RP2040-Zero", "Module:RP2040-Zero", ("1", "unconnected-(U4-5V-Pad1)", "5V", "power_in"),
                ("2", "+3V3", "3V3", "power_in"), ("3", "GND", "GND", "power_in"))
    assert rules(design(zero), "pin.power_unconnected") == []
    chip = part("U4", "STM32F103C8T6", "Package_QFP:LQFP-48", ("1", "unconnected-(U4-VDD-Pad1)", "VDD", "power_in"),
                ("2", "+3V3", "VDD", "power_in"), ("3", "GND", "VSS", "power_in"))
    assert [f.rule for f in rules(design(chip), "pin.power_unconnected")] == ["pin.power_unconnected"]


# --- LEDs returning through a low-side switch ------------------------------------------------------

def rgb(ref="D3", names=("A", "R", "G", "B"), nets=("+3V3", "LR", "LG", "LB")):
    return part(ref, "RGB", "LED_SMD:LED_RGB_PLCC", *[(str(i + 1), net, name) for i, (net, name) in
                                                      enumerate(zip(nets, names))])


def bss138(net="LR", gnd="GND"):
    return part("Q1", "BSS138", "Package_TO_SOT_SMD:SOT-23", ("1", "GATE", "G"), ("2", gnd, "S"), ("3", net, "D"))


def sized(ref, a, b, value, size="0603"):
    return part(ref, value, f"Resistor_SMD:R_{size}_1608Metric", ("1", a), ("2", b))


def test_power_rating_fires_for_an_rgb_channel_behind_a_low_side_mosfet():
    hits = rules(design(rgb(), sized("R22", "LR", "LD", "5.1"), bss138("LD")), "resistor.power_rating")
    assert [(f.refs, f.nets) for f in hits] == [(("R22", "D3", "Q1"), ("+3V3",))]
    assert "294 mA" in hits[0].evidence and "(3.3 V - 1.8 V)" in hits[0].evidence
    assert found("resistor.power_rating", rgb(), sized("R22", "LR", "LD", "330"), bss138("LD")) == []
    assert found("resistor.power_rating", rgb(names=("A", "K1", "K2", "K3")), sized("R22", "LR", "LD", "5.1"),
                 bss138("LD")) == []
