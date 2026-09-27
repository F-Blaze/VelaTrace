"""Read real connectivity from KiCad's XML netlist, including hierarchical exports."""
from pathlib import Path
import xml.etree.ElementTree as ET

from .errors import ValidationError
from .models import Component, DesignSnapshot, Pin


def read_xml_netlist(path: Path) -> DesignSnapshot:
    if path.stat().st_size > 25_000_000:
        raise ValidationError("Netlist is too large.")
    payload = path.read_bytes()
    if len(payload) > 25_000_000 or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValidationError("Netlist is too large or includes unsupported XML declarations.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValidationError("Cannot parse KiCad XML netlist; export again with --format kicadxml.") from exc
    if root.tag != "export" or root.find("components") is None or root.find("nets") is None:
        raise ValidationError("A KiCad connectivity netlist is required, not a flat BOM.")
    pins: dict[str, list[Pin]] = {}
    membership: dict[tuple[str, str], str] = {}
    for net in root.findall("./nets/net"):
        name = net.get("name", "")
        if not name:
            raise ValidationError("Netlist contains a net without a name.")
        for node in net.findall("node"):
            key = (node.get("ref", ""), node.get("pin", ""))
            if not all(key):
                raise ValidationError("Netlist contains a node without a reference or pin number.")
            if key in membership:
                raise ValidationError("Netlist contains duplicate or conflicting pin membership.")
            membership[key] = name
            pins.setdefault(node.get("ref", ""), []).append(
                Pin(node.get("pin", ""), name, node.get("pinfunction", "")))
    components = []
    references: set[str] = set()
    for comp in root.findall("./components/comp"):
        ref = comp.get("ref", "")
        if not ref or ref in references:
            raise ValidationError("Netlist contains missing or duplicate component references.")
        references.add(ref)
        fields = {item.get("name", ""): item.text or "" for item in comp.findall("./fields/field")}
        components.append(Component(ref, comp.findtext("value", ""), comp.findtext("footprint", ""),
                                    tuple(pins.get(ref, ())), fields))
    if set(pins) - references:
        raise ValidationError("Netlist connectivity refers to unknown components.")
    return DesignSnapshot(tuple(components), "kicad-xml-netlist", path)
