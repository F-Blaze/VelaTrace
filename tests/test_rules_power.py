"""Power rules: each fires every time the mistake is on the board and stays quiet otherwise."""
import pytest

from test_audit_rules import cap, design, part, res, rules
from velatrace.findings import Severity

GND = ("2", "GND", "GND", "power_in")
QFN = "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"


def found(prefix, *parts):
    return [(f.rule, f.refs) for f in rules(design(*parts), prefix)]


def chip(*pins, value="MCU", footprint="Package_QFP:LQFP-48", ref="U1"):
    return part(ref, value, footprint, ("1", "+3V3", "VDD", "power_in"), GND, *pins)


def inductor(ref, a, b):
    return part(ref, "10uH", "Inductor_SMD:L_0805", ("1", a), ("2", b))


# --- power.vcap_missing -----------------------------------------------------------------------

@pytest.mark.parametrize("name", ["VCAP_1", "VCAP1", "VCAP", "VCAP_2", "VREG_VOUT", "VREGVOUT", "VREG_OUT", "DEC1", "DEC4",
                                  "REGOUT", "REGOUT1", "VDDCORE", "VCORE", "LDO_OUT", "LDOOUT", "vcap_1", "~{DEC2}",
                                  "BYPASS", "BYP"])
def test_vcap_fires_for_each_spelling_when_net_has_a_neighbour_but_no_cap(name):
    ic = chip(("3", "Net-(U1-X)", name, "passive"))
    probe = part("TP1", "TP", "TestPoint:TestPoint_Pad", ("1", "Net-(U1-X)"))
    hits = rules(design(ic, probe), "power.vcap_missing")
    assert [(f.rule, f.severity, f.refs, f.nets) for f in hits] == [
        ("power.vcap_missing", Severity.WARNING, ("U1",), ("Net-(U1-X)",))]


@pytest.mark.parametrize("name", ["VCAP_1", "VCAP2", "VREG_OUT", "REGOUT", "VDDCORE", "LDO_OUT"])
def test_vcap_fires_when_pin_is_left_unconnected_without_a_no_connect_flag(name):
    assert found("power.vcap_missing", chip(("3", "unconnected-(U1-VCAP)", name, "passive"))) == [
        ("power.vcap_missing", ("U1",))]
    assert found("power.vcap_missing", chip(("3", "", name, "passive"))) == [("power.vcap_missing", ("U1",))]


def test_vcap_one_finding_lists_every_bare_pin_and_ignores_the_decoupled_one():
    ic = chip(("3", "VCAP1", "VCAP_1", "passive"), ("4", "VCAP2", "VCAP_2", "passive"),
              ("5", "VCAPOK", "VCAP_3", "passive"), ("6", "NRES", "NRST"))
    parts = (ic, res("R1", "VCAP1", "VCAP2"), cap("C1", "VCAPOK", "GND"))
    hits = rules(design(*parts), "power.vcap_missing")
    assert len(hits) == 1
    assert set(hits[0].nets) == {"VCAP1", "VCAP2"} and "VCAP_1" in hits[0].title and "VCAP_2" in hits[0].title


def test_vcap_cap_to_a_supply_rail_is_not_a_cap_to_ground():
    ic = chip(("3", "VCAP", "VCAP_1", "passive"))
    assert found("power.vcap_missing", ic, cap("C1", "VCAP", "+3V3")) == [("power.vcap_missing", ("U1",))]


@pytest.mark.parametrize("name", ["VCAP_1", "DEC4", "REGOUT", "VDDCORE", "LDO_OUT", "VREG_OUT"])
def test_vcap_quiet_with_cap_to_ground(name):
    assert found("power.vcap_missing", chip(("3", "VCAP", name, "passive")), cap("C1", "VCAP", "GND")) == []


def test_vcap_quiet_for_other_ground_spellings_and_polarised_caps():
    ic = chip(("3", "VCAP", "VCAP_1", "passive"))
    assert found("power.vcap_missing", ic, cap("C1", "VCAP", "/IMU/Q_GND")) == []
    assert found("power.vcap_missing", ic, cap("C1", "VCAP", "AGND")) == []
    assert found("power.vcap_missing", ic, cap("CP1", "VCAP", "GND", value="4.7uF")) == []


def test_vcap_quiet_when_cap_is_behind_inductor_or_ferrite_or_resistor():
    # nRF52 DC/DC: DCC -> inductor -> DEC4 with the capacitor
    ic = chip(("3", "DCCnet", "DCC", "passive"), ("4", "DEC4net", "DEC4", "passive"))
    parts = (ic, inductor("L1", "DCCnet", "DEC4net"), cap("C1", "DEC4net", "GND", value="1uF"))
    assert found("power.vcap_missing", *parts) == []
    ferrite = part("FB1", "BLM18", "Inductor_SMD:L_0603", ("1", "VCAP"), ("2", "VCAPF"))
    assert found("power.vcap_missing", chip(("3", "VCAP", "VCAP_1", "passive")), ferrite, cap("C1", "VCAPF", "GND")) == []
    assert found("power.vcap_missing", chip(("3", "VCAP", "VCAP_1", "passive")), res("R1", "VCAP", "VCAPF", "0"),
                 cap("C1", "VCAPF", "GND")) == []


@pytest.mark.parametrize("name", ["VCAP", "DEC1", "VDDCORE", "VCORE"])
def test_vcap_quiet_when_tied_to_a_decoupled_supply_rail(name):
    ic = chip(("3", "+3V3", name, "power_in"))
    assert found("power.vcap_missing", ic, cap("C1", "+3V3", "GND")) == []
    via_zero = chip(("3", "VCORE_IN", name, "power_in"))
    assert found("power.vcap_missing", via_zero, res("R1", "VCORE_IN", "+3V3", "0"), cap("C1", "+3V3", "GND")) == []


@pytest.mark.parametrize("name", ["VCAP_1", "DEC2", "REGOUT", "VDDCORE"])
def test_vcap_quiet_with_no_connect_flag(name):
    assert found("power.vcap_missing", chip(("3", "unconnected-(U1-X)", name, "passive+no_connect"))) == []


@pytest.mark.parametrize("name", ["DCC", "DEC2", "BYP", "BYPASS"])
def test_vcap_optional_pins_may_stay_open_but_not_float_on_a_bare_net(name):
    assert found("power.vcap_missing", chip(("3", "unconnected-(U1-X)", name, "passive"))) == []
    probe = part("TP1", "TP", "TestPoint:TestPoint_Pad", ("1", "N1"))
    assert found("power.vcap_missing", chip(("3", "N1", name, "passive")), probe) == [("power.vcap_missing", ("U1",))]


@pytest.mark.parametrize("name", ["VCAPX", "VDD", "DECODE", "VCAP_FB_SENSE", "VREF", "CORE", "BYPASS_EN", "OUT"])
def test_vcap_ignores_other_pin_names(name):
    assert found("power.vcap_missing", chip(("3", "N1", name, "passive")),
                 part("TP1", "TP", "TestPoint:TestPoint_Pad", ("1", "N1"))) == []


def test_vcap_ignores_dnp_ic_and_connectors():
    pin = ("3", "N1", "VCAP_1", "passive")
    probe = part("TP1", "TP", "TestPoint:TestPoint_Pad", ("1", "N1"))
    dead = chip(pin)
    dead = type(dead)(**{**dead.__dict__, "dnp": True})
    assert found("power.vcap_missing", dead, probe) == []
    header = part("J1", "Hdr", "Connector_PinHeader_2.54mm:PinHeader_1x04", ("1", "N1", "VCAP_1"))
    assert found("power.vcap_missing", header) == []


# --- power.reg_out_cap_missing ----------------------------------------------------------------

def ldo(ref="U2", value="AMS1117-3.3", vin="VIN", vout="+3V3", names=("VIN", "VOUT"), footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
        out_type="power_out", extra=()):
    return part(ref, value, footprint, ("1", "GND", "GND", "power_in"), ("3", vin, names[0], "power_in"),
                ("2", vout, names[1], out_type), *extra)


def load(net="+3V3"):
    return part("U9", "MCU", "Package_QFP:LQFP-48", ("1", net, "VDD", "power_in"), ("2", "GND", "GND", "power_in"))


@pytest.mark.parametrize("value,names", [
    ("AMS1117-3.3", ("VIN", "VOUT")), ("LM7805", ("VI", "VO")), ("L7805", ("VI", "VO")), ("78L05", ("IN", "OUT")),
    ("LM317", ("VI", "VO")), ("AP2112K-3.3", ("VIN", "VOUT")), ("MCP1700T-3302E", ("VIN", "VOUT")),
    ("XC6206P332MR", ("VIN", "VOUT")), ("TLV75533PDBV", ("IN", "OUT")), ("RT9013-33GB", ("VIN", "VOUT")),
    ("NCP1117-5v", ("IN", "OUT")), ("LDL1117S33R", ("VIN", "VOUT")), ("HT7333", ("VIN", "VOUT")),
    ("ME6211C33", ("VIN", "VOUT")), ("LP2985-33", ("VIN", "VOUT")), ("MCP1825S", ("VIN", "VOUT")),
    ("AP7333-33", ("VIN", "VOUT")), ("AP2210", ("VIN", "VOUT")), ("LM1117-3.3", ("VIN", "VOUT")),
    ("7812", ("VI", "VO")), ("79L05", ("VI", "VO"))])
def test_reg_out_fires_for_known_families(value, names):
    hits = rules(design(ldo(value=value, names=names), load(), cap("C1", "VIN", "GND")), "power.reg_out_cap_missing")
    assert [(f.rule, f.severity, f.refs, f.nets) for f in hits] == [
        ("power.reg_out_cap_missing", Severity.WARNING, ("U2",), ("+3V3",))]


@pytest.mark.parametrize("names", [("VIN", "VOUT"), ("IN", "OUT"), ("VI", "VO"), ("~{IN}", "OUT"), ("Vin", "Vout")])
def test_reg_out_fires_for_an_unknown_part_with_a_typed_in_out_pair(names):
    reg = ldo(value="XYZ-LDO-3V3", names=names)
    assert found("power.reg_out_cap_missing", reg, load()) == [("power.reg_out_cap_missing", ("U2",))]


def test_reg_out_fires_even_when_input_has_a_cap_and_for_each_regulator():
    parts = (ldo("U2"), ldo("U3", vout="+1V8", value="AP2112K-1.8"), load(), load("+1V8"), cap("C9", "+3V3", "+1V8"))
    assert sorted(found("power.reg_out_cap_missing", *parts)) == [
        ("power.reg_out_cap_missing", ("U2",)), ("power.reg_out_cap_missing", ("U3",))]


def test_reg_out_cap_to_wrong_net_does_not_count():
    assert found("power.reg_out_cap_missing", ldo(), load(), cap("C1", "VIN", "GND")) == [
        ("power.reg_out_cap_missing", ("U2",))]


@pytest.mark.parametrize("value", ["AMS1117-3.3", "LM7805", "XYZ-LDO"])
def test_reg_out_quiet_with_output_cap(value):
    assert found("power.reg_out_cap_missing", ldo(value=value), load(), cap("C1", "+3V3", "GND", value="22uF")) == []


def test_reg_out_quiet_when_the_cap_is_elsewhere_on_the_rail_or_behind_a_ferrite():
    # the capacitor sits on the downstream side of a ferrite / 0R / inductor
    ferrite = part("FB1", "BLM18", "Inductor_SMD:L_0603", ("1", "+3V3"), ("2", "+3V3_F"))
    assert found("power.reg_out_cap_missing", ldo(), load("+3V3_F"), ferrite, cap("C1", "+3V3_F", "GND")) == []
    assert found("power.reg_out_cap_missing", ldo(), load(), res("R1", "+3V3", "+3V3_F", "0"),
                 cap("C1", "+3V3_F", "GND")) == []
    # a rail shared with another regulator that has the capacitor
    assert found("power.reg_out_cap_missing", ldo(), ldo("U3", value="AMS1117-3.3"), load(), cap("C1", "+3V3", "AGND")) == []


def test_reg_out_quiet_for_switching_converters_and_modules():
    buck = ldo(value="TPS62160", extra=(("4", "SWN", "SW", "power_out"), ("5", "BSTN", "BST")))
    assert found("power.reg_out_cap_missing", buck, load()) == []
    lx = ldo(value="XYZ", extra=(("4", "SWN", "LX"),))
    assert found("power.reg_out_cap_missing", lx, load()) == []
    module = ldo(ref="U2", value="R-78E5.0-0.5", footprint="Converter_DCDC:Converter_DCDC_RECOM_R-78E-0.5")
    assert found("power.reg_out_cap_missing", module, load()) == []
    pump = ldo(value="XYZ", extra=(("4", "C1P", "C1+"), ("5", "C1N", "C1-")))
    assert found("power.reg_out_cap_missing", pump, load()) == []


@pytest.mark.parametrize("value", ["TPS2051B", "AP2171", "TPS22918", "MIC94090", "LM66100", "ADR4550", "REF3033",
                                   "25MHz", "Load switch", "eFuse"])
def test_reg_out_quiet_for_switches_references_and_oscillators(value):
    assert found("power.reg_out_cap_missing", ldo(value=value), load()) == []


def test_reg_out_unknown_part_needs_a_typed_power_output_and_a_small_package():
    assert found("power.reg_out_cap_missing", ldo(value="XYZ", out_type="passive"), load()) == []
    assert found("power.reg_out_cap_missing", ldo(value="XYZ", out_type="bidirectional"), load()) == []
    big = ldo(value="XYZ", extra=tuple((str(n), f"N{n}", f"P{n}") for n in range(10, 20)))
    assert found("power.reg_out_cap_missing", big, load()) == []


def test_reg_out_quiet_for_op_amps_h_bridges_and_dual_in_out_names():
    opamp = part("U2", "LM358", "Package_SO:SOIC-8", ("1", "N1", "OUT", "output"), ("2", "N2", "IN-", "input"),
                 ("3", "N3", "IN+", "input"), ("4", "GND", "V-", "power_in"))
    assert found("power.reg_out_cap_missing", opamp, load("N1")) == []
    bridge = part("U2", "MX", "Package_SO:SOIC-8", ("1", "N1", "OUT1", "power_out"), ("2", "N2", "IN1", "input"))
    assert found("power.reg_out_cap_missing", bridge, load("N1")) == []


def test_reg_out_quiet_for_unwired_grounded_no_connect_and_dnp_outputs():
    assert found("power.reg_out_cap_missing", ldo(vout="unconnected-(U2-VOUT)")) == []
    assert found("power.reg_out_cap_missing", ldo(vout="GND")) == []
    assert found("power.reg_out_cap_missing", ldo(out_type="power_out+no_connect"), load()) == []
    dead = ldo()
    assert found("power.reg_out_cap_missing", type(dead)(**{**dead.__dict__, "dnp": True}), load()) == []


# --- power.bootstrap_cap_missing --------------------------------------------------------------

def buck(boot="BST", sw="SW", boot_net="BSTN", sw_net="SWN", extra=(), ref="U3", footprint="Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.81mm"):
    return part(ref, "TPS54331", footprint, ("1", "VIN", "VIN", "power_in"), ("2", "GND", "GND", "power_in"),
                ("3", boot_net, boot, "passive"), ("4", sw_net, sw, "power_out"), *extra)


def coil(net="SWN"):
    return inductor("L1", net, "+3V3")


@pytest.mark.parametrize("boot,sw", [("BST", "SW"), ("BOOT", "PH"), ("CB", "LX"), ("BS", "SW"), ("BOOTSTRAP", "SW"),
                                     ("BST", "LX"), ("BOOT", "SW"), ("~{BST}", "SW"), ("BST1", "SW1"), ("BST", "PHASE")])
def test_bootstrap_fires_for_each_spelling_without_a_cap(boot, sw):
    hits = rules(design(buck(boot, sw), coil(), res("R9", "BSTN", "VIN")), "power.bootstrap_cap_missing")
    assert [(f.rule, f.severity, f.refs, f.nets) for f in hits] == [
        ("power.bootstrap_cap_missing", Severity.WARNING, ("U3",), ("BSTN",))]


def test_bootstrap_fires_for_cap_to_ground_or_wrong_node_and_for_unconnected_pin():
    assert found("power.bootstrap_cap_missing", buck(), coil(), cap("C1", "BSTN", "GND")) == [
        ("power.bootstrap_cap_missing", ("U3",))]
    assert found("power.bootstrap_cap_missing", buck(), coil(), cap("C1", "BSTN", "VIN")) == [
        ("power.bootstrap_cap_missing", ("U3",))]
    assert found("power.bootstrap_cap_missing", buck(boot_net="unconnected-(U3-BST)"), coil()) == [
        ("power.bootstrap_cap_missing", ("U3",))]


def test_bootstrap_fires_once_per_ic_with_several_boot_pins():
    two = buck(extra=(("5", "BSTN2", "BST2"), ("6", "SWN2", "SW2")))
    hits = rules(design(two, coil(), inductor("L2", "SWN2", "+3V3"), cap("C1", "BSTN", "SWN"), res("R9", "BSTN2", "VIN")),
                 "power.bootstrap_cap_missing")
    assert [(f.refs, f.nets) for f in hits] == [(("U3",), ("BSTN2",))]


@pytest.mark.parametrize("boot,sw", [("BST", "SW"), ("BOOT", "PH"), ("CB", "LX"), ("BS", "SW")])
def test_bootstrap_quiet_with_cap_to_switch_node(boot, sw):
    assert found("power.bootstrap_cap_missing", buck(boot, sw), coil(), cap("C1", "BSTN", "SWN", value="100nF")) == []


def test_bootstrap_quiet_with_cap_through_a_boot_resistor_or_either_orientation():
    assert found("power.bootstrap_cap_missing", buck(), coil(), res("R1", "BSTN", "BSTR", "10R"),
                 cap("C1", "SWN", "BSTR")) == []
    assert found("power.bootstrap_cap_missing", buck(), coil(), cap("C1", "SWN", "BSTN")) == []


def test_bootstrap_quiet_for_mcu_strap_pins_and_ics_without_a_switch_pin():
    mcu = part("U1", "STM32F103", "Package_QFP:LQFP-48", ("1", "+3V3", "VDD", "power_in"), ("2", "GND", "VSS", "power_in"),
               ("3", "BOOT0net", "BOOT0", "input"), ("4", "BOOT1net", "BOOT1"), ("5", "N5", "PH0"), ("6", "N6", "PH1"),
               ("7", "N7", "BOOTSEL"), ("8", "N8", "BOOT_MODE"))
    probes = [part(f"TP{n}", "TP", "TestPoint:TestPoint_Pad", ("1", net)) for n, net in
              enumerate(("BOOT0net", "BOOT1net", "N5", "N6", "N7", "N8"))]
    assert found("power.bootstrap_cap_missing", mcu, *probes) == []
    assert found("power.bootstrap_cap_missing", buck(sw="FB", sw_net="FBN"), res("R1", "FBN", "GND")) == []


def test_bootstrap_quiet_with_no_connect_flag_and_ignores_dnp_ic():
    assert found("power.bootstrap_cap_missing", part("U3", "X", "Package_SO:SOIC-8", ("1", "SWN", "SW"),
                                                     ("2", "u1", "BST", "passive+no_connect")), coil()) == []
    dead = buck()
    assert found("power.bootstrap_cap_missing", type(dead)(**{**dead.__dict__, "dnp": True}), coil()) == []


def test_bootstrap_quiet_when_the_switch_pin_is_not_wired():
    assert found("power.bootstrap_cap_missing", buck(sw_net="unconnected-(U3-SW)")) == []


# --- power.exposed_pad_floating ---------------------------------------------------------------

def qfn(pad, ref="U4", value="IC", footprint=QFN, extra=()):
    others = [(str(n), f"S{n}", f"IO{n}") for n in range(1, 6)]
    return part(ref, value, footprint, ("1", "+3V3", "VDD", "power_in"), ("2", "GND", "GND", "power_in"),
                *others[2:], pad, *extra)


def test_ep_pad_names_that_fire_when_open():
    for name in ["EP", "PAD", "EPAD", "THERMAL", "GND_EP", "EP_GND", "THERMAL_PAD", "EXPOSED_PAD", "GNDPAD", "~{EP}",
                 "ep", "EP1"]:
        hits = rules(design(qfn(("33", "unconnected-(U4-EP)", name, "power_in"))), "power.exposed_pad_floating")
        assert [(f.rule, f.severity, f.refs) for f in hits] == [
            ("power.exposed_pad_floating", Severity.WARNING, ("U4",))], name


def test_ep_open_variants_no_net_single_node_label():
    assert found("power.exposed_pad_floating", qfn(("33", "", "EP"))) == [("power.exposed_pad_floating", ("U4",))]
    assert found("power.exposed_pad_floating", qfn(("33", "EP_ONLY", "EPAD"))) == [("power.exposed_pad_floating", ("U4",))]


def test_ep_ground_named_pad_on_non_ground_net_fires():
    stray = part("TP1", "TP", "TestPoint:TestPoint_Pad", ("1", "N33"))
    hits = rules(design(qfn(("33", "N33", "GND_EP")), stray), "power.exposed_pad_floating")
    assert [(f.rule, f.refs, f.nets) for f in hits] == [("power.exposed_pad_floating", ("U4",), ("N33",))]
    assert found("power.exposed_pad_floating", qfn(("33", "+3V3", "THERMAL_GND"))) == [("power.exposed_pad_floating", ("U4",))]


def test_ep_plain_gnd_last_pad_of_an_exposed_pad_footprint_fires_when_open_or_off_ground():
    many = [("%d" % n, f"S{n}", f"IO{n}") for n in range(3, 8)]
    ic = part("U4", "IC", QFN, ("1", "+3V3", "VDD"), ("2", "GND", "GND"), *many, ("8", "unconnected-(U4-GND-Pad8)", "GND"))
    assert found("power.exposed_pad_floating", ic) == [("power.exposed_pad_floating", ("U4",))]
    off = part("U4", "IC", QFN, ("1", "+3V3", "VDD"), ("2", "GND", "GND"), *many, ("8", "VX", "GND"))
    probe = part("TP1", "TP", "TestPoint:TestPoint_Pad", ("1", "VX"))
    assert found("power.exposed_pad_floating", off, probe) == [("power.exposed_pad_floating", ("U4",))]


def test_ep_one_finding_covers_several_open_pads():
    ic = qfn(("33", "unconnected-(U4-EP)", "EP1"), extra=(("34", "unconnected-(U4-EP2)", "EP2"),))
    hits = rules(design(ic), "power.exposed_pad_floating")
    assert len(hits) == 1 and "EP1" in hits[0].title and "EP2" in hits[0].title


@pytest.mark.parametrize("name", ["EP", "PAD", "GND_EP", "THERMAL", "EPAD"])
def test_ep_quiet_when_on_ground(name):
    assert found("power.exposed_pad_floating", qfn(("33", "GND", name, "power_in"))) == []
    assert found("power.exposed_pad_floating", qfn(("33", "/pwr/AGND", name)), cap("C1", "/pwr/AGND", "GND")) == []


def test_ep_quiet_when_neutral_name_sits_on_another_node():
    # EP/PAD only says "pad"; the designer may tie it to VOUT or a heat-sink net
    assert found("power.exposed_pad_floating", qfn(("33", "+3V3", "EP")), cap("C1", "+3V3", "GND")) == []
    assert found("power.exposed_pad_floating", qfn(("33", "VOUT", "PAD")), res("R1", "VOUT", "+3V3")) == []


@pytest.mark.parametrize("name", ["VOUT", "SW", "VIN", "NC", "SW_PAD_OUT", "TAB"])
def test_ep_quiet_when_named_for_another_node(name):
    assert found("power.exposed_pad_floating", qfn(("33", "unconnected-(U4-X)", name, "power_out"))) == []


def test_ep_quiet_with_no_connect_flag_dnp_and_non_exposed_footprints():
    assert found("power.exposed_pad_floating", qfn(("33", "unconnected-(U4-EP)", "EP", "power_in+no_connect"))) == []
    dead = qfn(("33", "unconnected-(U4-EP)", "EP"))
    assert found("power.exposed_pad_floating", type(dead)(**{**dead.__dict__, "dnp": True})) == []
    plain = qfn(("33", "unconnected-(U4-GND)", "GND"), footprint="Package_QFP:LQFP-32_7x7mm_P0.8mm")
    assert found("power.exposed_pad_floating", plain) == []
    connector = part("J1", "Conn", "Connector_PinHeader_2.54mm:PinHeader_1x02", ("1", "X", "EP"), ("2", "Y", "PAD"))
    assert found("power.exposed_pad_floating", connector) == []


def test_ep_quiet_for_a_plain_gnd_that_is_not_the_extra_pad():
    ic = part("U4", "IC", QFN, ("1", "+3V3", "VDD"), ("2", "unconnected-(U4-GND)", "GND"), ("3", "S3", "IO3"), ("4", "S4", "IO4"),
              ("5", "S5", "IO5"), ("6", "S6", "IO6"))
    assert found("power.exposed_pad_floating", ic) == []


def test_vcap_quiet_when_the_return_net_is_only_known_by_a_gnd_pin():
    ic = chip(("3", "VCAP", "VCAP_1", "passive"), ("4", "COMMON", "GND", "power_in"))
    assert found("power.vcap_missing", ic, cap("C1", "VCAP", "COMMON")) == []
