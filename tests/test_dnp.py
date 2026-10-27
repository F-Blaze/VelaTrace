"""Synthetic DNP fixtures: suppress only the irrelevant single-pin finding."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from velatrace.audit_rules import check_single_pin_nets
from velatrace.ipc import KiCadReader
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.netlist import read_xml_netlist


def test_dnp_connector_does_not_hide_fitted_dangling_labels():
    optional = Component("J1", "Conn", "Connector:Header", (Pin("1", "OPTIONAL"),), dnp=True)
    fitted = replace(optional, reference="J2", pins=(Pin("1", "TYPO"),), dnp=False)
    found = check_single_pin_nets(DesignSnapshot((optional, fitted), "synthetic"))
    assert [(f.rule, f.refs, f.nets) for f in found] == [("net.single_pin", ("J2",), ("TYPO",))]


def test_dnp_does_not_suppress_label_scope_findings():
    optional = Component("J1", "Conn", "Connector:Header", (Pin("1", "SIGNAL"),), dnp=True)
    fitted = Component("U1", "MCU", "Package:IC", (Pin("1", "/sheet/SIGNAL"), Pin("2", "/sheet/SIGNAL")))
    found = check_single_pin_nets(DesignSnapshot((optional, fitted), "synthetic"))
    assert [(f.rule, f.refs) for f in found] == [("net.label_scope", ("J1",))]


@pytest.mark.parametrize("net,kind", [("SPARE", "input+no_connect"), ("unconnected-(U1-Pad1)", "")])
def test_no_connect_is_not_a_single_pin_finding(net, kind):
    part = Component("U1", "MCU", "Package:IC", (Pin("1", net, electrical_type=kind),))
    assert check_single_pin_nets(DesignSnapshot((part,), "synthetic")) == []


def test_xml_dnp_property(tmp_path):
    # Authored fixture matching KiCad's <property name="dnp"/> export marker;
    # BOM exclusion and a user field named DNP do not mean do-not-populate.
    path = tmp_path / "dnp.xml"
    path.write_text('<export><components>'
                    '<comp ref="J1"><property name="dnp"/></comp>'
                    '<comp ref="J2"><property name="exclude_from_bom"/></comp>'
                    '<comp ref="J3"><fields><field name="DNP">yes</field></fields></comp>'
                    '</components><nets/></export>', encoding="utf-8")
    assert [c.dnp for c in read_xml_netlist(path).components] == [True, False, False]


@pytest.mark.parametrize("dnp", [False, True])
@pytest.mark.parametrize("net", ["SPARE", "unconnected-(J1-Pad1)"])
def test_ipc_reads_dnp_from_pinned_sdk(dnp, net):
    from kipy.board_types import FootprintInstance, Net, Pad
    fp = FootprintInstance()
    fp.reference_field.text.value = "J1"
    fp.value_field.text.value = "Conn"
    fp.attributes.do_not_populate = dnp
    pad = Pad()
    pad.number = "1"
    pad_net = Net()
    pad_net.name = net
    pad.net = pad_net
    fp.definition.add_item(pad)
    board = SimpleNamespace(get_footprints=lambda: [fp], get_enabled_layers=lambda: [], name="")
    client = SimpleNamespace(get_version=lambda: SimpleNamespace(major=10, minor=0, patch=6),
                             get_board=lambda: board)
    snapshot = KiCadReader(client).read_board()
    assert snapshot.components[0].dnp is dnp
    found = check_single_pin_nets(snapshot)
    assert [f.rule for f in found] == ([] if dnp or net.startswith("unconnected-") else ["net.single_pin"])
