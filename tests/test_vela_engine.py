"""Native Vela-routing engine: loader geometry, clearance by brute force, plane drops."""
import math

import pytest

pytest.importorskip("numpy")

from velatrace import vela_electrical  # noqa: E402
from velatrace.vela_board import load_board  # noqa: E402
from velatrace.vela_engine import Options, route_board  # noqa: E402


def _board(tmp_path, body, inner=False):
    layers = '(0 "F.Cu" signal) ' + ('(4 "In1.Cu" signal) (6 "In2.Cu" signal) ' if inner else '') + '(2 "B.Cu" signal)'
    path = tmp_path / "board.kicad_pcb"
    path.write_text(
        f'(kicad_pcb (version 20260206) (generator "test") (general (thickness 1.6)) '
        f'(layers {layers} (25 "Edge.Cuts" user)) '
        f'(gr_rect (start 0 0) (end 20 12) (layer "Edge.Cuts")) {body})', encoding="utf-8")
    return load_board(path)


def _pad(ref, x, y, net, rotation=0, through=False):
    kind, layers, drill = ("thru_hole circle", '"*.Cu"', "(drill 0.6)") if through else ("smd rect", '"F.Cu"', "")
    return (f'(footprint "T" (layer "F.Cu") (at {x} {y} {rotation}) (property "Reference" "{ref}") '
            f'(pad "1" {kind} (at 0 0 {rotation}) (size 1.2 1.2) {drill} (layers {layers}) (net "{net}")))')


def _gap(a, b, c, d):
    """Smallest distance between segments ab and cd."""
    def point(p, q, r):
        dx, dy = r[0] - q[0], r[1] - q[1]
        t = max(0, min(1, ((p[0] - q[0]) * dx + (p[1] - q[1]) * dy) / (dx * dx + dy * dy or 1)))
        return math.dist(p, (q[0] + t * dx, q[1] + t * dy))

    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    if side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0:
        return 0.0
    return min(point(a, c, d), point(b, c, d), point(c, a, b), point(d, a, b))


def test_loader_rotates_pads_like_kicad(tmp_path):
    board = _board(tmp_path, '(footprint "T" (layer "F.Cu") (at 5 5 90) (property "Reference" "R1") '
                             '(pad "1" smd rect (at -1 0 90) (size 1 0.5) (layers "F.Cu") (net "A")) '
                             '(pad "2" thru_hole oval (at 1 0 90) (size 1 2) (drill 0.6) (layers "*.Cu") (net "B")))')
    first, second = board.pads
    # KiCad: positive rotation is counter-clockwise on a Y-down screen, so local -X goes down.
    assert (round(first.x, 6), round(first.y, 6)) == (5, 6) and (round(second.x, 6), round(second.y, 6)) == (5, 4)
    assert first.layers == (0,) and second.layers == (0, 1) and second.drill == 0.6
    assert len(board.edges) == 4 and board.netclass("A").width == 0.2


def test_crossing_nets_route_with_vias_and_keep_clearance(tmp_path):
    board = _board(tmp_path, _pad("A1", 3, 6, "A") + _pad("A2", 17, 6, "A")
                   + _pad("B1", 10, 2, "B") + _pad("B2", 10, 10, "B"))
    router = route_board(board, Options())
    report = router.report()
    assert report["unrouted"] == 0 and report["connections"] == 2 and report["vias"] == 2
    assert not any(router._in_conflict(net) for net in router.order)
    a, b = (router.geometry(router.nets[name]) for name in "AB")
    for layer_a, p, q, _ in a[0]:
        for layer_b, r, s, _ in b[0]:
            assert layer_a != layer_b or _gap(p, q, r, s) >= 0.2 + 0.2 - 1e-6
    for layer, p, q, _ in a[0]:   # the other net's vias are copper on every layer
        for via in b[1]:
            assert _gap(p, q, via, via) >= 0.1 + 0.2 + 0.3 - 1e-6


def test_inner_zone_becomes_reference_plane(tmp_path):
    zone = '(zone (net "GND") (layer "In1.Cu") (polygon (pts (xy 0 0) (xy 20 0) (xy 20 12) (xy 0 12))))'
    board = _board(tmp_path, zone + _pad("C1", 4, 3, "GND") + _pad("C2", 16, 9, "GND") + _pad("J1", 4, 9, "GND", through=True)
                   + _pad("S1", 3, 6, "SIG") + _pad("S2", 17, 6, "SIG"), inner=True)
    report = route_board(board, Options()).report()
    assert report["reference_planes"] == ["In1.Cu"] and report["unrouted"] == 0
    ground, signal = report["nets"]["GND"], report["nets"]["SIG"]
    # Two surface pads drop a via each; the through-hole pad is already in the plane.
    assert ground["connections"] == 2 and ground["vias"] == 2 and ground["to_plane"] == ["In1.Cu"]
    assert "In1.Cu" not in signal["length_mm"] and signal["vias"] == 0
    electrical = report["electrical"]
    assert electrical["stackup_assumed"] and electrical["signal_on_plane_mm"] == 0
    assert electrical["layers"][0]["kind"] == "microstrip" and signal["delay_ps"] > 14 * 5.5


def test_electrical_self_check():
    vela_electrical.demo()


def test_wide_class_necks_down_to_leave_a_tight_pad(tmp_path):
    # Pads 0.3 mm wide on a 0.5 mm pitch: a 0.5 mm track cannot leave the middle one.
    row = "".join(f'(pad "{n}" smd rect (at {0.5 * n} 0) (size 0.3 1.2) (layers "F.Cu") (net "N{n}"))' for n in range(5))
    body = (f'(footprint "U" (layer "F.Cu") (at 6 3) (property "Reference" "U1") {row})'
            + "".join(_pad(f"T{n}", 3 + 3.5 * n, 9, f"N{n}") for n in range(5)))
    board = _board(tmp_path, body)
    (tmp_path / "board.kicad_pro").write_text(
        '{"board": {"design_settings": {"rules": {"min_track_width": 0.15}}}, "net_settings": {"classes": '
        '[{"name": "Default", "track_width": 0.5, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3}]}}',
        encoding="utf-8")
    board = load_board(tmp_path / "board.kicad_pcb")
    router = route_board(board, Options())
    report = router.report()
    assert report["unrouted"] == 0 and report["electrical"]["necked_mm"] > 0
    widths = {width for net in router.nets.values() for *_, width in router.geometry(net)[0]}
    assert widths == {0.15, 0.5}


def test_second_plane_carries_signals_only_when_its_net_keeps_a_solid_plane(tmp_path):
    def zone(net, layer):
        return f'(zone (net "{net}") (layer "{layer}") (polygon (pts (xy 0 0) (xy 20 0) (xy 20 12) (xy 0 12))))'
    pads = (_pad("C1", 4, 3, "GND") + _pad("S1", 3, 6, "SIG") + _pad("S2", 17, 6, "SIG")
            + _pad("P1", 10, 9, "VCC") + _pad("P2", 14, 9, "VCC"))
    doubled = route_board(_board(tmp_path, zone("GND", "In1.Cu") + zone("GND", "In2.Cu") + pads, inner=True)).report()
    assert doubled["mixed_planes"] == ["In2.Cu"] and doubled["unrouted"] == 0
    # A signal on In2 could cut VCC pads off their only plane, so In2 stays solid here.
    split = route_board(_board(tmp_path, zone("GND", "In1.Cu") + zone("VCC", "In2.Cu") + pads, inner=True)).report()
    assert split["mixed_planes"] == [] and split["reference_planes"] == ["In1.Cu", "In2.Cu"] and split["unrouted"] == 0
