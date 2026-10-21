"""Footprint fit: EasyEDA pad parser, pad-set comparison, IPC pad offsets and the seeded bench board."""
from dataclasses import replace
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from velatrace.easyeda import MAX_PADS, RealPart, parse_component
from velatrace.findings import Severity
from velatrace.footprint_fit import compare, fit_candidates, kicad_pads
from velatrace.ipc import KiCadReader
from velatrace.models import Component, DesignSnapshot, Pin

FIXTURES = Path(__file__).parent / "fixtures" / "fit"
BENCH = Path(os.environ.get("VELATRACE_BENCH", Path.home() / "Documents" / "VelaTrace-bench"))


def load(code):
    return json.loads((FIXTURES / f"{code}.json").read_text(encoding="utf-8"))


def real(code):
    return parse_component(load(code))


def rules(pads, part, ref="U1"):
    return [item.rule for item in compare(ref, pads, part)]


def row(count, pitch=2.54, tht=True):
    return [(str(i + 1), pitch * i, 0.0, tht) for i in range(count)]


# KiCad PinHeader_2x03_P2.54mm_Vertical: odd pins left, even pins right, pin 1 at the origin.
HEADER_2X3 = [(str(i + 1), 2.54 * (i % 2), 2.54 * (i // 2), True) for i in range(6)]


def soic(pins=8, pitch=1.27, tht=False, row=2.475):
    """KiCad SOIC-8_3.9x4.9mm_P1.27mm (pins=14: SOIC-14_3.9x8.7mm): pins down the left, then up the right."""
    side = pins // 2
    half = pitch * (side - 1) / 2
    return ([(str(i + 1), -row, -half + pitch * i, tht) for i in range(side)]
            + [(str(i + side + 1), row, half - pitch * i, tht) for i in range(side)])


def soic8(pitch=1.27, tht=False):
    return soic(8, pitch, tht)


def two_pads(half):
    """A 2-pad footprint (diode, buzzer) with its pad centres half apart from the origin."""
    return [("1", -half, 0.0, False), ("2", half, 0.0, False)]


# KiCad library pads of the megadesk parts (footprint frame), as read from the bench board.
TO252_2 = [("1", -5.04, -2.28, False), ("3", -5.04, 2.28, False), ("2", 1.26, 0.0, False)] + [
    ("2", x, y, False) for x in (-0.415, 2.935) for y in (-1.525, 1.525)]     # split tab, one number
SOT89_3 = [("1", -1.95, -1.5, False), ("2", -1.863, 0.0, False), ("3", -1.95, 1.5, False)]  # pad 2: custom tab
FFC_10 = [(str(i + 1), -4.5 + i, 0.0, False) for i in range(10)]           # F1004-H-10-20G-R pins
FFC_MOUNT = [("0", -7.0, 3.1, False), ("0", 7.0, 3.1, False)]


def spacing(pads):
    """Smallest distance between two pads, rounded to 0.001 mm."""
    return round(min(math.dist((a.x_mm, a.y_mm), (b.x_mm, b.y_mm))
                     for i, a in enumerate(pads) for b in pads[i + 1:]), 3)


# ---- parser ----

def test_parser_reads_pads_in_mm_from_the_origin():
    header = real("C65114")
    assert (header.lcsc, header.title, header.source) == ("C65114", "Header-Male-2.54_2x3", "easyeda")
    assert header.package == "HDR-TH_6P-P2.54-V-M-R2-C3-S2.54-1"
    assert len(header.pads) == 6 and spacing(header.pads) == 2.54 and all(p.through_hole for p in header.pads)
    pin1 = next(p for p in header.pads if p.number == "1")
    assert (pin1.x_mm, pin1.y_mm) == (-2.54, 1.27)
    soic = real("C7593")
    assert len(soic.pads) == 8 and spacing(soic.pads) == 1.27 and not any(p.through_hole for p in soic.pads)
    assert len(real("C124378").pads) == 4 and spacing(real("C124378").pads) == 2.54
    qfn = real("C2040")
    assert len(qfn.pads) == 57 and spacing(qfn.pads) == 0.4 and not any(p.through_hole for p in qfn.pads)
    areas = {p.number: p.area_mm2 for p in real("C16106").pads}    # SOT-89: the tab is 10 x a pin
    assert areas["1"] == areas["3"] == pytest.approx(1.06, abs=0.01) and areas["2"] == pytest.approx(10.2, abs=0.1)


def test_malformed_pad_records_are_ignored():
    data = load("C7593")
    good = data["result"]["packageDetail"]["dataStr"]["shape"]
    junk = ["PAD~RECT~nan~0~1~1~1~~9~0", "PAD~RECT~inf~0~1~1~1~~9~0", "PAD~RECT~1e300~0~1~1~1~~9~0",
            "PAD~RECT~x~0~1~1~1~~9~0", "PAD~RECT~0~0~1~1~1~~~0", "PAD~RECT~0~0", "PAD", "PAD~", 7, None,
            ["PAD"], "TRACK~1~2", "PAD~RECT~0~0~1~1~1~~9~-nan"]
    data["result"]["packageDetail"]["dataStr"]["shape"] = good + junk
    assert parse_component(data) == real("C7593")


def test_unusable_responses_give_none():
    assert parse_component({}) is None and parse_component({"result": 5}) is None
    assert parse_component({"result": {"packageDetail": {"dataStr": {"head": {}, "shape": []}}}}) is None
    for broken in ({"x": "a", "y": 0}, {"x": float("nan"), "y": 0}, None):
        data = load("C7593")
        data["result"]["packageDetail"]["dataStr"]["head"] = broken
        assert parse_component(data) is None
    data = load("C7593")
    data["result"]["packageDetail"]["dataStr"]["shape"] = ["TRACK~1"]
    assert parse_component(data) is None
    data["result"]["packageDetail"]["dataStr"]["shape"] = ["PAD~RECT~0~0~1~1~1~~1~0"] * (MAX_PADS + 1)
    assert parse_component(data) is None


# ---- comparison ----

def test_mic_case_two_rows_of_three_on_a_row_of_six():
    found = compare("MK1", row(6), real("C65114"))
    assert [item.rule for item in found] == ["fit.layout"]
    assert found[0].refs == ("MK1",) and "1 × 6" in found[0].title and "2 × 3" in found[0].title


def test_right_header_footprint_has_no_findings():
    assert compare("J1", HEADER_2X3, real("C65114")) == []


def test_soic_matches_in_any_orientation():
    assert rules(soic8(), real("C7593")) == []     # the part is the library footprint turned 90 degrees
    part = real("C7593")
    mirrored = replace(part, pads=tuple(replace(p, x_mm=-p.x_mm, y_mm=-p.y_mm) for p in part.pads))
    assert rules(soic8(), mirrored) == []


def test_every_fixture_fits_itself_turned_but_not_mirrored():
    for code in ("C65114", "C7593", "C2040", "C124378", "C614879", "C152207"):
        part = real(code)
        turned = [(p.number, -p.y_mm, p.x_mm, p.through_hole) for p in part.pads]
        mirrored = [(p.number, -p.x_mm, p.y_mm, p.through_hole) for p in part.pads]
        assert rules(turned, part) == [], code
        # A mirror-image pinout is wrong, except for a single row where a mirror equals a turn.
        assert rules(mirrored, part) == ([] if code == "C124378" else ["fit.pin_order"]), code


def test_swapped_pin_numbers_are_a_pin_order_warning():
    pads = [("8" if n == "1" else "1" if n == "8" else n, x, y, t) for n, x, y, t in soic8()]
    (finding,) = compare("U1", pads, real("C7593"))
    assert (finding.rule, finding.severity) == ("fit.pin_order", Severity.WARNING)
    assert "pad 1" in finding.evidence and "pin 8" in finding.evidence


def test_mount_type_count_and_pitch():
    assert rules(soic8(tht=True), real("C7593")) == ["fit.mount_type"]
    assert rules(soic8()[:7], real("C7593")) == ["fit.pad_count"]
    (finding,) = compare("U1", soic8(pitch=1.0), real("C7593"))
    assert finding.rule == "fit.pitch" and "1.00 mm" in finding.evidence and "1.27 mm" in finding.evidence


def test_only_the_most_fundamental_mismatch_is_reported():
    # Wrong count, wrong mounting, wrong layout and wrong numbers at once: count wins.
    assert rules(row(5, tht=False), real("C65114")) == ["fit.pad_count"]
    assert rules(row(6, tht=False), real("C65114")) == ["fit.mount_type"]


def test_shared_numbers_count_once_and_unnumbered_pads_are_left_out():
    qfn = real("C2040")
    thermal = [(p.number, p.x_mm, p.y_mm, False) for p in qfn.pads]
    ep = next(p for p in thermal if p[0] == "57")
    vias = [("57", ep[1] + dx, ep[2] + dy, False) for dx in (-1, 1) for dy in (-1, 1)]
    assert rules(thermal + vias, qfn) == []
    assert rules(thermal + [("", 9.0, 9.0, True)], qfn) == []


def test_package_only_part_checks_count_and_mount_type_and_says_so():
    part = RealPart("C9", "Header 6P", "", pin_count=6, through_hole=True)
    (finding,) = compare("J1", row(4), part)
    assert finding.rule == "fit.pad_count" and "could not" not in finding.evidence
    assert "not available" in finding.evidence
    assert rules(row(6, tht=False), part) == ["fit.mount_type"]
    assert rules(row(6), part) == [] and rules(HEADER_2X3, part) == []   # layout is not claimed


# ---- library differences that are not wrong parts (megadesk, LCSC parts captured 2026-10-21) ----

def test_row_spacing_may_differ_but_pin_pitch_may_not():
    # Same packages, other land patterns: rows 4.95 mm apart in KiCad, 5.60 / 5.41 mm at LCSC.
    assert rules(soic(14), real("C614879")) == [] and rules(soic8(), real("C152207")) == []
    # Two-pad parts: the pad spacing is the land pattern (SOD-123F 2.8 vs 3.3 mm, buzzer 8.7 vs 8.0 mm).
    assert rules(two_pads(1.4), real("C353288")) == [] and rules(two_pads(4.35), real("C391035")) == []
    # A different package still differs: narrow SOIC on a wide-SOIC land pattern (rows 9.3 mm apart),
    # SOD-123F on SOD-323 pads (2.1 mm), the buzzer on pads 11 mm apart, and a 1.2 mm pitch.
    assert rules(soic(8, row=4.65), real("C152207")) == ["fit.pitch"]
    assert rules(two_pads(1.05), real("C353288")) == ["fit.pitch"]
    assert rules(two_pads(5.5), real("C391035")) == ["fit.pitch"]
    (finding,) = compare("U3", soic(14, pitch=1.2), real("C614879"))
    assert finding.rule == "fit.pitch" and "0.15 mm along a row" in finding.evidence


def test_tab_pad_is_left_out_of_layout_and_positions():
    assert rules(SOT89_3, real("C16106")) == []     # KiCad anchors the tab by the pins, LCSC mid-tab
    assert rules(TO252_2, real("C116325")) == []
    # SOT-23 pads (1 and 2 on one side, 3 opposite) are still not a SOT-89.
    sot23 = [("1", -0.9375, -0.95, False), ("2", -0.9375, 0.95, False), ("3", 0.9375, 0.0, False)]
    assert rules(sot23, real("C16106")) == ["fit.layout"]
    # The tab still counts: a SOT-89 footprint without it has too few pads.
    assert rules(SOT89_3[::2], real("C16106")) == ["fit.pad_count"]


def test_mounting_pads_are_left_out_on_both_sides():
    part = real("C668117")             # 10 pins, mounting tabs numbered 11 and 12
    assert rules(FFC_10 + FFC_MOUNT, part) == []
    assert rules(FFC_10 + [("MP", x, y, t) for _, x, y, t in FFC_MOUNT], part) == []
    assert rules(FFC_10 + [("", x, y, t) for _, x, y, t in FFC_MOUNT], part) == []
    # Without mounting pads in the footprint the tabs have nowhere to go.
    assert rules(FFC_10, part) == ["fit.pad_count"]
    # Small extra pins are never taken for mounting pads: 9 pins + mounting pads vs the 10-pin part.
    assert rules(FFC_10[:9] + FFC_MOUNT, part) == ["fit.pad_count"]
    # An exposed pad is matched by number, not dropped: a QFN footprint without it has too few pads.
    qfn = real("C2040")
    assert rules([(p.number, p.x_mm, p.y_mm, False) for p in qfn.pads if p.number != "57"], qfn) == ["fit.pad_count"]


def test_cached_rows_with_and_without_pad_area():
    from velatrace.part_lookup import _from_row, _row
    part = real("C16106")
    assert _from_row("C16106", _row(part, "2026-10-21T00:00:00Z")) == part
    old = {"title": "T", "package": "P", "pads": [["1", 0.0, 0.0, False], ["2", 1.0, 0.0, False]]}
    assert [p.area_mm2 for p in _from_row("C1", old).pads] == [0.0, 0.0]


# ---- parts to look up ----

def test_fit_candidates_skip_passives_and_parts_without_pads():
    def comp(ref, footprint="Package_SO:SOIC-8", value="X", offset=(0.0, 0.0), number="1"):
        return Component(ref, value, footprint, (Pin(number, "N", offset_mm=offset),))
    snapshot = DesignSnapshot((comp("U1"), comp("R1", "Resistor_SMD:R_0402"), comp("C1", "Capacitor_SMD:C_0402"),
                               comp("L1", "Inductor_SMD:L_0805"), comp("FB1", "Inductor_SMD:L_0603"),
                               comp("U2", ""), comp("U3", number=""), comp("U4", offset=None),
                               comp("J1", "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical")), "fixture")
    assert [item.reference for item in fit_candidates(snapshot)] == ["U1", "J1"]
    assert kicad_pads(snapshot.components[0]) == [("1", 0.0, 0.0, None)]


# ---- IPC offsets ----

def _reader_snapshot(angle, layer, pads):
    from kipy.board_types import FootprintInstance, Pad
    from kipy.geometry import Angle, Vector2
    fp = FootprintInstance()
    fp.position = Vector2.from_xy(10_000_000, 20_000_000)
    fp.orientation = Angle.from_degrees(angle)
    fp.layer = layer
    fp.reference_field.text.value = "J3"
    for number, x, y, drill in pads:
        pad = Pad()
        pad.number = number
        pad.position = Vector2.from_xy(round(x * 1e6), round(y * 1e6))
        pad.padstack.drill.diameter = Vector2.from_xy(round(drill * 1e6), round(drill * 1e6))
        fp.definition.add_item(pad)
    board = SimpleNamespace(get_footprints=lambda: [fp], get_enabled_layers=lambda: [], name="")
    client = SimpleNamespace(get_version=lambda: SimpleNamespace(major=10, minor=0, patch=6),
                             get_board=lambda: board)
    return KiCadReader(client).read_board()


def test_ipc_pad_offsets_undo_footprint_rotation_and_side():
    from kipy.proto.board.board_types_pb2 import BoardLayer
    # Footprint at (10, 20) turned 90: a pad 2.54 below the origin in its own frame sits 2.54 to the right.
    pads = [("1", 10, 20, 1.0), ("2", 12.54, 20, 0.0), ("", 10, 22.54, 0.0)]
    pins = _reader_snapshot(90, BoardLayer.BL_F_Cu, pads).components[0].pins
    assert [pin.position_mm for pin in pins][1] == pytest.approx((12.54, 20))
    assert pins[0].offset_mm == pytest.approx((0, 0)) and pins[1].offset_mm == pytest.approx((0, 2.54))
    assert [pin.through_hole for pin in pins] == [True, False, False]
    # Back side: the library frame is mirrored left-right.
    back = _reader_snapshot(0, BoardLayer.BL_B_Cu, [("1", 12.54, 20, 0.0)]).components[0].pins[0]
    assert back.offset_mm == pytest.approx((-2.54, 0))
    # An unnumbered pad is passed on too: compare() counts it as a mounting pad.
    assert kicad_pads(Component("J3", "", "L:F", tuple(pins))) == [
        ("1", pytest.approx(0, abs=1e-9), pytest.approx(0, abs=1e-9), True),
        ("2", pytest.approx(0, abs=1e-9), pytest.approx(2.54), False),
        ("", pytest.approx(-2.54), pytest.approx(0, abs=1e-9), False)]


def test_ipc_missing_pad_fields_leave_fit_data_unknown():
    from kipy.board_types import FootprintInstance
    from kipy.geometry import Vector2
    from velatrace.ipc import _pin
    fp = FootprintInstance()
    bare = SimpleNamespace(number="1", net=SimpleNamespace(name="N"), position=Vector2.from_xy(0, 0))
    pin = _pin(fp, bare, False)
    assert pin.through_hole is None and pin.number == "1" and pin.position_mm == (0, 0)


# ---- seeded bench board ----

def bench_pads(board_file, reference):
    """Pads of one footprint in its own frame, from the saved board (absolute pads un-rotated)."""
    from velatrace.candidate import read_board
    from velatrace.sexpr import children
    from velatrace.vela_board import load_board
    _, root = read_board(board_file)
    for fp in children(root, "footprint"):
        if any(len(p) > 2 and p[1] == "Reference" and p[2] == reference for p in children(fp, "property")):
            at = children(fp, "at")[0]
            fx, fy, turn = float(at[1]), float(at[2]), math.radians(float(at[3]) if len(at) > 3 else 0.0)
            break
    result = []
    for pad in load_board(board_file).pads:
        if pad.ref == reference and pad.number:
            dx, dy = pad.x - fx, pad.y - fy   # KiCad turns y-down points x' = x cos + y sin
            result.append((pad.number, dx * math.cos(turn) - dy * math.sin(turn),
                           dx * math.sin(turn) + dy * math.cos(turn), pad.drill > 0))
    return result


@pytest.mark.skipif(not (BENCH / "seeded" / "KEY_fit.json").is_file(), reason="VelaTrace-bench not present")
def test_seeded_bench_boards():
    for case in json.loads((BENCH / "seeded" / "KEY_fit.json").read_text(encoding="utf-8")):
        (ref,) = case["refs"]
        found = rules(bench_pads(BENCH / case["board"], ref), real("C65114"), ref)
        assert found == case["expected_rules"], case["id"]


def test_package_only_part_that_states_its_rows_gets_a_layout_check():
    stated = RealPart("", "EXAMPLE-2X3-HDR", "", source="digikey", pin_count=6, through_hole=True, rows=2)
    found = compare("J1", row(6), stated)
    assert [item.rule for item in found] == ["fit.layout"]
    assert "part 2 × 3" in found[0].title and "pad positions were not compared" in found[0].evidence
    assert rules(HEADER_2X3, stated) == []
    assert rules(row(6), replace(stated, rows=1)) == []
    assert rules(row(6), replace(stated, rows=0)) == []        # no row count: nothing to compare
    assert rules(row(6), replace(stated, rows=4)) == []        # 6 pins cannot be 4 rows: ignore the hint
