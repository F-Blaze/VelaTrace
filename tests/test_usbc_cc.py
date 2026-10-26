"""Focused regression tests for USB-C CC pull-down audit checks (Issue #38)."""

from velatrace.audit_rules import run_rules
from velatrace.findings import Severity
from velatrace.models import Component, DesignSnapshot, Pin


def component(reference, value, footprint, *pins):
    """Build a component with (number, net[, name[, electrical_type]]) pins."""
    made = []
    for number, net, *details in pins:
        name = details[0] if details else ""
        kind = details[1] if len(details) > 1 else ""
        made.append(Pin(number, net, name, kind, None))
    return Component(reference, value, footprint, tuple(made), {})


def design(*components):
    return DesignSnapshot(tuple(components), "fixture")


def resistor(reference, a, b, value="5.1k"):
    return component(reference, value, "Resistor_SMD:R_0402", ("1", a), ("2", b))


def usb_c(*extra, footprint="Connector_USB:USB_C_Receptacle_USB2.0",
          value="USB_C_Receptacle", named=True):
    cc1 = ("A5", "USB_CC1", "CC1") if named else ("A5", "USB_CC1")
    cc2 = ("B5", "USB_CC2", "CC2") if named else ("B5", "USB_CC2")
    connector = component("J1", value, footprint, cc1, cc2,
                          ("A4", "VBUS", "VBUS"), ("A1", "GND", "GND"))
    return design(connector, *extra)


def cc_findings(snapshot):
    return [item for item in run_rules(snapshot, providers=())
            if item.rule == "connector.usbc_cc_missing"]


def test_both_cc_pulldowns_missing_warns():
    found = cc_findings(usb_c())
    assert len(found) == 1
    finding = found[0]
    assert finding.severity == Severity.WARNING
    assert finding.refs == ("J1",)
    assert finding.nets == ("USB_CC1", "USB_CC2")
    assert "5.1 kΩ" in finding.fix


def test_two_5k1_resistors_to_ground_are_silent():
    assert cc_findings(usb_c(resistor("R1", "USB_CC1", "GND"),
                              resistor("R2", "USB_CC2", "GND", "5k1"))) == []


def test_one_missing_cc_reports_that_net_only():
    found = cc_findings(usb_c(resistor("R1", "USB_CC1", "GND")))
    assert len(found) == 1
    assert found[0].nets == ("USB_CC2",)
    assert "CC2" in found[0].title


def test_controller_on_cc_suppresses_warning():
    controller = component("U2", "FUSB302", "Package_SO:SOIC-8",
                           ("1", "USB_CC1", "CC1"))
    assert cc_findings(usb_c(controller)) == []


def test_power_out_on_vbus_suppresses_source_port():
    source = component("U2", "POWER", "Package_SO:SOIC-8",
                       ("1", "VBUS", "VOUT", "power_out"))
    assert cc_findings(usb_c(source)) == []


def test_missing_pin_names_silent():
    assert cc_findings(usb_c(named=False)) == []


def test_non_usb_c_connector_silent():
    assert cc_findings(usb_c(value="USB_B", footprint="Connector_USB:USB_B_Micro")) == []


def test_zero_ohm_link_does_not_count_as_pulldown():
    found = cc_findings(usb_c(resistor("R1", "USB_CC1", "GND", "0R"),
                               resistor("R2", "USB_CC2", "GND")))
    assert len(found) == 1
    assert found[0].nets == ("USB_CC1",)


def test_resistor_array_on_cc_stays_silent():
    array = component("RN1", "5.1k", "Resistor_SMD:R_Array",
                      ("1", "USB_CC1"), ("2", "GND"))
    assert cc_findings(usb_c(array)) == []


def test_no_named_ground_stays_silent():
    connector = component("J1", "USB_C", "Connector_USB:USB_C_Receptacle_USB2.0",
                          ("A5", "USB_CC1", "CC1"), ("B5", "USB_CC2", "CC2"),
                          ("A4", "VBUS", "VBUS"))
    assert cc_findings(design(connector)) == []
