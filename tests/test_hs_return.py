"""High-speed return-path checks: missing reference plane and layer change without a return via."""
import json
import os
from pathlib import Path
import unittest

from velatrace.audit_rules import check_return_plane, check_return_vias, run_rules
from velatrace.copper import BoardCopper, FilledZone, Segment, Via, read_copper_file
from velatrace.findings import Severity
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.vela_electrical import LayerModel

FIXTURE = Path(__file__).parent / "fixtures" / "copper" / "hs_board.kicad_pcb"
BENCH = Path(os.environ.get("VELATRACE_BENCH", "C:/Users/Owner/Documents/VelaTrace-bench"))
FOUR = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
SQUARE = ((0, 0), (20, 0), (20, 20), (0, 20))


def seg(net, layer, start, end):
    import math
    return Segment(net, layer, start, end, 0.2, math.dist(start, end))


def model(name, kind):
    return LayerModel(name, kind, 0.2, None, 4.2, True)


def two_layer(kind, net="USB_D+"):
    return BoardCopper(("F.Cu", "B.Cu"), 1.6, (seg(net, "F.Cu", (0, 0), (10, 0)),), (), (), frozenset(),
                       {"F.Cu": model("F.Cu", kind), "B.Cu": model("B.Cu", kind)}, {})


def four_layer(*extra_vias, in2_net="GND"):
    """USB_D+ goes F.Cu -> B.Cu through a via at (5, 5); In1/In2 are planes filled with GND / in2_net."""
    kinds = {"F.Cu": "microstrip", "In1.Cu": "plane", "In2.Cu": "plane", "B.Cu": "microstrip"}
    return BoardCopper(
        FOUR, 1.6,
        (seg("USB_D+", "F.Cu", (0, 5), (5, 5)), seg("USB_D+", "B.Cu", (5, 5), (15, 5))),
        (Via("USB_D+", (5, 5), FOUR), *extra_vias),
        (FilledZone("GND", "In1.Cu", SQUARE), FilledZone(in2_net, "In2.Cu", SQUARE)),
        frozenset({"In1.Cu", "In2.Cu"}), {name: model(name, kind) for name, kind in kinds.items()}, {})


def snap(copper, *components):
    return DesignSnapshot(tuple(components), "fixture", copper=copper)


def cap(ref, a, b, at):
    return Component(ref, "100nF", "Capacitor_SMD:C_0402", (Pin("1", a, position_mm=at), Pin("2", b, position_mm=at)))


class NoPlaneTests(unittest.TestCase):
    def test_unreferenced_layer_is_reported_with_length(self):
        found = check_return_plane(snap(two_layer("unreferenced")))
        self.assertEqual([f.rule for f in found], ["hs.return.no_plane"])
        self.assertEqual(found[0].nets, ("USB_D+",))
        self.assertIn("F.Cu 10.0 mm", found[0].evidence)
        self.assertIn("refill", found[0].evidence)

    def test_referenced_layer_and_crystal_nets_pass(self):
        self.assertEqual(check_return_plane(snap(two_layer("microstrip"))), [])
        self.assertEqual(check_return_plane(snap(two_layer("unreferenced", "XTAL_IN"))), [])

    def test_no_copper_means_no_findings(self):
        self.assertEqual(check_return_plane(snap(None)), [])
        self.assertEqual(check_return_vias(snap(None)), [])


class ReturnViaTests(unittest.TestCase):
    def test_layer_change_without_ground_via(self):
        found = check_return_vias(snap(four_layer(Via("GND", (9, 5), FOUR))))   # 4 mm away: too far
        self.assertEqual([f.rule for f in found], ["hs.return.via"])
        self.assertIn("5.00, 5.00 mm", found[0].title)
        self.assertIn("GND", found[0].title)

    def test_close_ground_via_passes(self):
        self.assertEqual(check_return_vias(snap(four_layer(Via("GND", (6, 5), FOUR)))), [])

    def test_ground_via_must_reach_both_planes(self):
        blind = Via("GND", (6, 5), ("F.Cu", "In1.Cu"))
        self.assertEqual(len(check_return_vias(snap(four_layer(blind)))), 1)

    def test_different_plane_nets_need_a_capacitor(self):
        copper = four_layer(Via("GND", (6, 5), FOUR), in2_net="+3V3")
        # No pad positions (netlist parts): cannot judge, stays silent.
        self.assertEqual(check_return_vias(snap(copper)), [])
        far = check_return_vias(snap(copper, cap("C1", "GND", "+3V3", (18, 18))))
        self.assertEqual(len(far), 1)
        self.assertIn("stitching capacitor", far[0].fix)
        self.assertEqual(check_return_vias(snap(copper, cap("C1", "GND", "+3V3", (6, 6)))), [])

    def test_fixture_board_is_clean(self):
        # Referenced layers, and the /USB_D+ through via only has F.Cu track on it: no layer change.
        snapshot = snap(read_copper_file(FIXTURE))
        self.assertEqual(check_return_plane(snapshot) + check_return_vias(snapshot), [])

    def test_rules_run_as_info(self):
        findings = run_rules(snap(four_layer()), providers=())
        rules = {f.rule: f.severity for f in findings if f.rule.startswith("hs.return")}
        self.assertEqual(rules, {"hs.return.via": Severity.INFO})
        findings = run_rules(snap(two_layer("unreferenced")), providers=())
        self.assertIn(("hs.return.no_plane", Severity.INFO), {(f.rule, f.severity) for f in findings})


@unittest.skipUnless((BENCH / "seeded" / "KEY_hs_return.json").is_file(), "VelaTrace-bench not present")
class BenchBoardTests(unittest.TestCase):
    def test_seeded_boards(self):
        for entry in json.loads((BENCH / "seeded" / "KEY_hs_return.json").read_text(encoding="utf-8")):
            with self.subTest(entry["id"]):
                snapshot = snap(read_copper_file(BENCH / entry["pcb"]))
                rules = {f.rule for f in check_return_plane(snapshot) + check_return_vias(snapshot)}
                self.assertTrue(set(entry["expected_rules"]) <= rules, rules)
                self.assertFalse(set(entry.get("absent_rules", ())) & rules, rules)


if __name__ == "__main__":
    unittest.main()
