from velatrace.copper import BoardCopper, CopperPad, Segment, parse_copper
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.rules_copper import check_exposed_pad_open, check_mains_spacing

TWO = ("F.Cu", "B.Cu")


def board(pads, segments=(), edges=(), values=None):
    copper = BoardCopper(TWO, 1.6, tuple(segments), (), (), frozenset(), {}, {}, pads=tuple(pads), edges=tuple(edges))
    refs = dict.fromkeys(p.ref for p in pads)
    comps = tuple(Component(r, (values or {}).get(r, ""), "", tuple(Pin(p.number, p.net) for p in pads if p.ref == r))
                  for r in refs)
    return DesignSnapshot(comps, "test", copper=copper)


def pad(ref, number, net, x, y, w=0.3, h=0.8, kind="smd", layers=("F.Cu",)):
    return CopperPad(ref, number, net, (x, y), (w, h), layers, kind)


def qfn(ep_net, ref="U1", extra=()):
    ring = [pad(ref, str(i + 1), "SIG", x, y) for i, (x, y) in enumerate(
        [(-2, -1), (-2, 0), (-2, 1), (2, -1), (2, 0), (2, 1), (0, -2), (0, 2)])]
    return ring + [pad(ref, "9", ep_net, 0, 0, 2.0, 2.0)] + list(extra)


def test_exposed_pad_without_net_is_reported_and_grounded_one_is_not():
    found = check_exposed_pad_open(board(qfn("")))
    assert [f.rule for f in found] == ["pcb.exposed_pad_open"] and found[0].refs == ("U1",)
    assert check_exposed_pad_open(board(qfn("unconnected-(U1-EP-Pad9)")))
    assert not check_exposed_pad_open(board(qfn("GND")))


def test_flash_and_sensirion_parts_may_leave_the_pad_open():
    assert not check_exposed_pad_open(board(qfn(""), values={"U1": "W25Q128JVP"}))
    assert not check_exposed_pad_open(board(qfn(""), values={"U1": "SGP40-D-R4"}))


def test_thermal_vias_without_net_inside_a_grounded_pad():
    vias = [pad("U1", "", "", x, y, 0.5, 0.5, "thru_hole", TWO) for x, y in ((-0.5, -0.5), (0.5, 0.5))]
    found = check_exposed_pad_open(board(qfn("GND", extra=vias)))
    assert len(found) == 1 and "thermal vias" in found[0].title
    joined = [pad("U1", "9", "GND", x, y, 0.5, 0.5, "thru_hole", TWO) for x, y in ((-0.5, -0.5), (0.5, 0.5))]
    assert not check_exposed_pad_open(board(qfn("GND", extra=joined)))


def test_mounting_hole_and_two_pad_parts_are_not_exposed_pads():
    hole = [pad("H1", "1", "", 0, 0, 6.4, 6.4, "thru_hole", TWO)] + [
        pad("H1", "1", "", x, y, 0.8, 0.8, "thru_hole", TWO) for x, y in ((-2, 0), (2, 0), (0, -2), (0, 2))]
    inductor = [pad("L1", "1", "", -2, 0, 2, 3), pad("L1", "2", "SW", 2, 0, 2, 3)]
    assert not check_exposed_pad_open(board(hole + inductor))


def mains(extra_pads=(), segments=(), edges=(), values=None):
    """J1 mains terminal (L, N), PS1 AC-DC module, J2 low-voltage header on GND/+5V."""
    pads = [pad("J1", "1", "/L", 0, 0, 2, 2, "thru_hole", TWO), pad("J1", "2", "/N", 0, 5, 2, 2, "thru_hole", TWO),
            pad("PS1", "1", "/L", 20, 0, 2, 2, "thru_hole", TWO), pad("PS1", "2", "/N", 20, 5, 2, 2, "thru_hole", TWO),
            pad("PS1", "3", "GND", 40, 0, 2, 2, "thru_hole", TWO), pad("PS1", "4", "+5V", 40, 5, 2, 2, "thru_hole", TWO),
            pad("J2", "1", "GND", 60, 0, 2, 2, "thru_hole", TWO), pad("J2", "2", "+5V", 60, 5, 2, 2, "thru_hole", TWO),
            *extra_pads]
    return board(pads, segments, edges, values or {"PS1": "HLK-PM01"})


def track(net, y, x0=5, x1=15, width=1.0, layer="F.Cu"):
    return Segment(net, layer, (x0, y), (x1, y), width, x1 - x0)


def test_line_and_neutral_tracks_closer_than_2_5_mm():
    found = check_mains_spacing(mains(segments=[track("/L", 0), track("/N", 2.5)]))   # 1.5 mm gap
    assert len(found) == 1 and "line and neutral" in found[0].title and "1.50 mm" in found[0].evidence
    assert not check_mains_spacing(mains(segments=[track("/L", 0), track("/N", 4)]))   # 3 mm gap


def test_tracks_on_other_layers_or_across_a_slot_are_fine():
    assert not check_mains_spacing(mains(segments=[track("/L", 0), track("/N", 2.5, layer="B.Cu")]))
    slot = [((-5, 1.25), (25, 1.25))]
    assert not check_mains_spacing(mains(segments=[track("/L", 0), track("/N", 2.5)], edges=slot))


def test_mains_track_near_low_voltage_ground():
    found = check_mains_spacing(mains(segments=[track("/L", 0), track("GND", 2.0, 30, 50), track("/L", 0, 20, 45)]))
    assert [f.title for f in found] == ["Copper between mains and low-voltage is closer than 2.5 mm"]


def test_non_isolated_supply_is_left_alone():
    # A dropper resistor from line into the low-voltage ground: no isolation barrier to measure.
    dropper = [pad("R1", "1", "/L", 30, 10), pad("R1", "2", "GND", 32, 10)]
    assert not check_mains_spacing(mains(dropper, segments=[track("/L", 0), track("GND", 2.0, 0, 20)]))


def test_audio_left_channel_is_not_mains():
    pads = [pad("U1", "1", "/L", 0, 0), pad("U1", "2", "GND", 0, 2), pad("U1", "3", "/R", 0, 4),
            pad("C1", "1", "/L", 5, 0), pad("C1", "2", "GND", 5, 1)]
    assert not check_mains_spacing(board(pads, [track("/L", 0), track("GND", 1)]))


def test_parse_copper_reads_pads_and_footprint_slots():
    text = """(kicad_pcb (version 20240108) (generator "pcbnew")
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (net 0 "") (net 1 "GND")
      (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts"))
      (footprint "X:Y" (layer "F.Cu") (at 10 20 90)
        (property "Reference" "U1")
        (fp_line (start 0 -1) (end 0 1) (layer "Edge.Cuts"))
        (pad "1" smd rect (at 1 0 90) (size 0.5 1.0) (layers "F.Cu" "F.Mask") (net 1 "GND"))
        (pad "2" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu"))))"""
    copper = parse_copper(text)
    one, two = copper.pads
    assert (one.ref, one.number, one.net, one.layers, one.kind) == ("U1", "1", "GND", ("F.Cu",), "smd")
    assert abs(one.position[0] - 10) < 1e-9 and abs(one.position[1] - 19) < 1e-9   # turned 90 degrees
    assert two.net == "" and two.layers == ("F.Cu", "B.Cu")
    assert len(copper.edges) == 2


def test_charger_exposed_pad_without_vias():
    from velatrace.copper import Via
    from velatrace.rules_copper import check_thermal_vias_missing
    found = check_thermal_vias_missing(board(qfn("GND"), values={"U1": "TP4056"}))
    assert [f.rule for f in found] == ["pcb.thermal_vias_missing"]
    assert not check_thermal_vias_missing(board(qfn("GND"), values={"U1": "CP2102N"}))   # low power: no claim
    via_pad = [pad("U1", "9", "GND", 0.5, 0.5, 0.5, 0.5, "thru_hole", TWO)]
    assert not check_thermal_vias_missing(board(qfn("GND", extra=via_pad), values={"U1": "TP4056"}))
    snap = board(qfn("GND"), values={"U1": "DRV8833"})
    snap = snap.__class__(snap.components, snap.source, copper=snap.copper.__class__(
        **{**snap.copper.__dict__, "vias": (Via("GND", (0.3, -0.3), TWO),)}))
    assert not check_thermal_vias_missing(snap)
