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


def test_second_plane_carries_signals_and_a_cut_plane_is_noticed(tmp_path):
    def zone(net, layer):
        return f'(zone (net "{net}") (layer "{layer}") (polygon (pts (xy 0 0) (xy 20 0) (xy 20 12) (xy 0 12))))'
    pads = (_pad("C1", 4, 3, "GND") + _pad("S1", 3, 6, "SIG") + _pad("S2", 17, 6, "SIG")
            + _pad("P1", 10, 9, "VCC") + _pad("P2", 14, 9, "VCC"))
    doubled = route_board(_board(tmp_path, zone("GND", "In1.Cu") + zone("GND", "In2.Cu") + pads, inner=True))
    assert doubled.report()["mixed_planes"] == ["In2.Cu"] and doubled.report()["unrouted"] == 0
    assert doubled.guard == []   # GND keeps In1 whole whatever happens on In2
    # VCC has no other plane: In2 still carries signals, but its continuity is checked afterwards.
    split = route_board(_board(tmp_path, zone("GND", "In1.Cu") + zone("VCC", "In2.Cu") + pads, inner=True))
    report = split.report()
    assert report["mixed_planes"] == ["In2.Cu"] and split.guard == [("VCC", 2)]
    assert report["unrouted"] == 0 and report["plane_connections_mended"] == 0


def test_pieces_tells_a_cut_plane_from_a_whole_one():
    import numpy as np
    from velatrace.vela_engine import _pieces
    region = np.ones((5, 9), bool)
    region[:, 4] = False                     # a track's keep-off band cuts the plane in two
    labels, image = _pieces(region, [1 * 9 + 1, 3 * 9 + 2, 2 * 9 + 7, 2 * 9 + 4 + 9 * 0])
    assert labels[0] == labels[1] != labels[2] and labels[3] in (labels[0], labels[2])   # rim seed joins a neighbour
    assert (image == labels[0]).sum() == 20 and (image == labels[2]).sum() == 20
    region[2, 4] = True                      # a gap in the band: one piece again
    labels, _ = _pieces(region, [10, 25])
    assert labels[0] == labels[1]


def test_no_pour_rule_area_is_not_plane(tmp_path):
    zone = '(zone (net "GND") (layer "In1.Cu") (polygon (pts (xy 0 0) (xy 20 0) (xy 20 12) (xy 0 12))))'
    keepout = ('(zone (layers "In1.Cu") (keepout (tracks allowed) (vias allowed) (copperpour not_allowed)) '
               '(polygon (pts (xy 12 0) (xy 20 0) (xy 20 12) (xy 12 12))))')
    board = _board(tmp_path, zone + keepout + _pad("C1", 4, 3, "GND") + _pad("C2", 16, 9, "GND"), inner=True)
    assert [z.no_pour for z in board.zones] == [False, True]
    router = route_board(board, Options())
    _, vias = router.geometry(router.nets["GND"])
    # KiCad fills nothing in the rule area, so C2 must not drop its via there.
    assert router.report()["unrouted"] == 0 and vias and all(x < 12 for x, _ in vias)


def test_tracks_that_starve_a_thermal_relief_move_off_its_spokes(tmp_path):
    zone = ('(zone (net "GND") (layer "F.Cu") (connect_pads (clearance 0.2)) (min_thickness 0.25) '
            '(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) (polygon (pts (xy 0 0) (xy 20 0) (xy 20 12) (xy 0 12))))')
    # N1 and N2 sit in G1's side spokes, so only the upper and lower spokes can reach the pour.
    board = _board(tmp_path, zone + _pad("G1", 10, 6.9, "GND") + _pad("N1", 8.6, 6.9, "N1") + _pad("N2", 11.4, 6.9, "N2")
                   + _pad("S1", 3, 6, "SIG") + _pad("S2", 17, 6, "SIG"))
    assert board.zones[0].thermal_gap == 0.5 and board.zones[0].connect == "thermal"

    def nearest(relieve):   # SIG centre line to G1's upper spoke axis: 0.5 mm thermal gap plus half the fill's 0.25 mm
        router = route_board(board, Options(relieve=relieve))
        assert router.report()["unrouted"] == 0 and [len(g) for *_, g in router.corridors] == ([2] if relieve else [])
        return min(_gap(p, q, (10, 6.3), (10, 5.675)) for _, p, q, _ in router.geometry(router.nets["SIG"])[0])
    # The straight line clears the pads but cuts that spoke: one left, KiCad reports a starved thermal.
    assert nearest(False) < 0.1 and nearest(True) >= 0.1 + 0.2 - 1e-6


def test_rescue_turn_is_undone_when_worse_and_victims_get_dearer(tmp_path):
    import numpy as np
    board = _board(tmp_path, _pad("A1", 3, 6, "A") + _pad("A2", 17, 6, "A")
                   + _pad("B1", 10, 2, "B") + _pad("B2", 10, 10, "B"))
    router = route_board(board, Options())
    a, b = router.nets["A"], router.nets["B"]
    saved = router._state(a)

    def worse(price):   # a turn that only breaks things
        router._ripup(a)
        a.failed = 1
        return 0
    assert router._turn(a, worse, 8.0) is None
    assert router._state(a)[0] is saved[0] and a.failed == 0 and not router._in_conflict(a)
    args = (a, a.t, 0, router.H, 0, router.W, 1.0, False, router.via_cost, False)
    plain = np.asarray(router._costs(*args)[0])
    router.surcharge = {"B": 5.0}   # B was ripped before: crossing it costs more
    dear = np.asarray(router._costs(*args)[0])
    assert np.isclose((dear - plain).max(), 5.0) and (dear >= plain).all() and b.stamps is not None


def test_reconnect_routes_what_kicad_reports_open(tmp_path):
    from velatrace.kicad_cli import DrcResult
    from velatrace.vela_engine import kicad_check
    board = _board(tmp_path, _pad("A1", 3, 6, "A") + _pad("A2", 17, 6, "A")
                   + _pad("C1", 3, 2, "C") + _pad("C2", 4.6, 2, "C"))
    router = route_board(board, Options())
    for name in "AC":   # pretend both were left open
        router._restore(router.nets[name], ([], set(), 1, 1))
    missing = (("Pad 1 [A] of A1 on F.Cu", 3, 6), ("Pad 1 [A] of A2 on F.Cu", 17, 6)), \
              (("Pad 1 [C] of C1 on F.Cu", 3, 2), ("Zone 'C' [C] on F.Cu", 0.5, 0.5))

    class Cli:   # KiCad: first two open connections, then none
        results = [DrcResult(0, 2, 0, (), missing), DrcResult(0, 0, 0)]

        def drc(self, path):
            assert "(segment" in path.read_text(encoding="utf-8") or self.results[0].unconnected
            return self.results.pop(0)
    result, text = kicad_check(router, Cli(), "board.kicad_pcb")
    a, c = router.nets["A"], router.nets["C"]
    assert result.unconnected == 0 and not a.failed and not c.failed and a.paths
    assert c.links and c.links[0][1:3] == ((3.0, 2.0), (4.6, 2.0))   # off-grid straight link, pad to pad
    assert not router._in_conflict(a) and "(segment" in text
