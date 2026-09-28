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
