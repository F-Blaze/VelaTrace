"""XML encoding must not bypass the netlist declaration boundary."""
import pytest

from velatrace.errors import ValidationError
from velatrace.netlist import read_xml_netlist


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_entity_declarations_rejected_in_every_supported_encoding(tmp_path, encoding):
    path = tmp_path / "declared.xml"
    declared_encoding = "utf-16" if encoding.startswith("utf-16") else encoding
    text = (f'<?xml version="1.0" encoding="{declared_encoding}"?>'
            '<!DOCTYPE export [<!ENTITY value "injected">]>'
            '<export><components><comp ref="R1"><value>&value;</value></comp>'
            '</components><nets><net name="A"><node ref="R1" pin="1"/>'
            '</net></nets></export>')
    path.write_bytes(text.encode(encoding))
    with pytest.raises(ValidationError, match="declarations"):
        read_xml_netlist(path)


def test_utf16_netlist_without_declarations_still_reads(tmp_path):
    path = tmp_path / "netlist.xml"
    path.write_bytes(('<?xml version="1.0" encoding="utf-16"?>'
                      '<export><components><comp ref="R1"><value>10k</value></comp>'
                      '</components><nets><net name="A"><node ref="R1" pin="1"/>'
                      '</net></nets></export>').encode("utf-16"))
    snapshot = read_xml_netlist(path)
    assert snapshot.components[0].value == "10k"
    assert snapshot.connectivity() == {"A": (("R1", "1"),)}


def test_schematic_pin_types_are_kept_for_rule_checks(tmp_path):
    path = tmp_path / "typed.xml"
    path.write_text('<export><components><comp ref="U1"><value>LDO</value></comp></components>'
                    '<nets><net name="+5V"><node ref="U1" pin="1" pinfunction="VIN" pintype="power_in"/>'
                    '</net></nets></export>', encoding="utf-8")
    pin = read_xml_netlist(path).components[0].pins[0]
    assert (pin.name, pin.electrical_type, pin.position_mm) == ("VIN", "power_in", None)


def _netlist(tmp_path, nets, comps=("U1",)):
    path = tmp_path / "n.xml"
    path.write_text("<export><components>" + "".join(f'<comp ref="{ref}"/>' for ref in comps)
                    + "</components><nets>" + nets + "</nets></export>", encoding="utf-8")
    return read_xml_netlist(path)


def test_kicad10_pin_number_suffix_is_stripped_from_pin_names(tmp_path):
    snap = _netlist(tmp_path, '<net name="A"><node ref="U1" pin="37" pinfunction="GPIO25_37" pintype="bidirectional"/>'
                              '<node ref="U1" pin="+12V" pinfunction="+12V_+12V" pintype="power_out"/></net>')
    assert [pin.name for pin in snap.components[0].pins] == ["GPIO25", "+12V"]


def test_pin_names_kept_when_file_does_not_use_number_suffix(tmp_path):
    snap = _netlist(tmp_path, '<net name="A"><node ref="U1" pin="1" pinfunction="IO_1"/>'
                              '<node ref="U1" pin="2" pinfunction="VDD"/></net>')
    assert [pin.name for pin in snap.components[0].pins] == ["IO_1", "VDD"]


def test_no_connect_pin_type_is_exposed(tmp_path):
    snap = _netlist(tmp_path, '<net name="A"><node ref="U1" pin="1" pintype="input+no_connect"/>'
                              '<node ref="U1" pin="2" pintype="input"/></net>')
    assert [pin.no_connect for pin in snap.components[0].pins] == [True, False]


@pytest.mark.parametrize("order", [0, 1])
def test_repeated_pad_on_real_and_unconnected_net_prefers_real_net(tmp_path, order):
    real = '<net name="/BOOT"><node ref="SW1" pin="1"/><node ref="U1" pin="3"/></net>'
    dangling = '<net name="unconnected-(SW1-Pad1)"><node ref="SW1" pin="1" pintype="passive+no_connect"/></net>'
    snap = _netlist(tmp_path, real + dangling if order == 0 else dangling + real, ("U1", "SW1"))
    assert snap.connectivity()["/BOOT"] == (("U1", "3"), ("SW1", "1"))
    assert "unconnected-(SW1-Pad1)" not in snap.connectivity()


def test_pad_on_two_real_nets_is_still_rejected(tmp_path):
    with pytest.raises(ValidationError):
        _netlist(tmp_path, '<net name="A"><node ref="U1" pin="1"/></net><net name="B"><node ref="U1" pin="1"/></net>')
