"""Pre-flight rules: each refusal is one actionable line; rule notes never refuse."""
from pathlib import Path
import tempfile
import unittest

from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.preflight import outline_box, preflight
from velatrace.sexpr import parse

FIXTURES = Path(__file__).parent / "fixtures" / "audit"
OUTLINE = '(gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts"))'
PROJECT = {"board": {"design_settings": {"rules": {"min_clearance": 0.0, "min_track_width": 0.2,
                                                  "min_copper_edge_clearance": 0.0, "min_hole_clearance": 0.0}}},
           "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2}]}}


def board(*rows):
    return parse("(kicad_pcb " + " ".join(rows) + ")", kicad=True)


def part(reference, *pads, at=None):
    return Component(reference, "", "", tuple(Pin(str(n), "N", position_mm=p) for n, p in enumerate(pads, 1)),
                     position_mm=at or (pads[0] if pads else None))


def snapshot(*parts, path=None):
    return DesignSnapshot(parts, "ipc-pcb", path)


class PreflightTests(unittest.TestCase):
    def test_clean_board_passes(self):
        problems, notes = preflight(board(OUTLINE), snapshot(part("R1", (10, 10), (12, 10))), PROJECT)
        self.assertEqual((problems, notes), ([], []))

    def test_missing_outline_refuses(self):
        problems, _ = preflight(board(), snapshot(part("R1", (10, 10))), PROJECT)
        self.assertEqual(len(problems), 1)
        self.assertIn("Edge.Cuts", problems[0])

    def test_footprint_outside_outline_refuses_by_pad_position(self):
        # The footprint origin is inside, one pad is not: an unplaced or overhanging part.
        parts = (part("R1", (10, 10)), part("U7", (39, 10), (45, 10), at=(39, 10)), part("H1"))
        problems, _ = preflight(board(OUTLINE), snapshot(*parts), PROJECT)
        self.assertEqual(problems, ["1 footprint(s) outside the board outline: U7. "
                                    "Place them inside Edge.Cuts, then retry."])

    def test_arc_outline_box_covers_the_bulge(self):
        # A semicircular right edge bulges to x=50; the end points alone stop at 40.
        rows = ('(gr_line (start 0 0) (end 40 0) (layer "Edge.Cuts"))',
                '(gr_arc (start 40 0) (mid 50 10) (end 40 20) (layer "Edge.Cuts"))')
        self.assertGreaterEqual(outline_box(board(*rows))[2], 50 - 1e-9)
        problems, _ = preflight(board(*rows), snapshot(part("R1", (48, 10))), PROJECT)
        self.assertEqual(problems, [])

    def test_outline_drawn_only_inside_a_footprint_is_not_missing(self):
        root = board('(footprint "Outline" (fp_rect (start 0 0) (end 10 10) (layer "Edge.Cuts")))')
        self.assertEqual(outline_box(root), "footprint")
        self.assertEqual(preflight(root, snapshot(part("R1", (99, 99))), PROJECT)[0], [])

    def test_duplicate_references_refuse(self):
        parts = (part("R1", (1, 1)), part("R1", (2, 2)), part("C1", (3, 3)))
        problems, _ = preflight(board(OUTLINE), snapshot(*parts), PROJECT)
        self.assertEqual(len(problems), 1)
        self.assertIn("Duplicate reference designators: R1.", problems[0])

    def test_already_routed_board_refuses(self):
        for row in ('(segment (start 1 1) (end 2 2) (width .2) (layer "F.Cu"))', '(via (at 1 1))'):
            problems, _ = preflight(board(OUTLINE, row), snapshot(part("R1", (10, 10))), PROJECT)
            self.assertEqual(len(problems), 1)
            self.assertIn("already has tracks or vias", problems[0])

    def test_board_setup_rules_that_skip_the_dsn_only_warn(self):
        project = {**PROJECT, "board": {"design_settings": {"rules": {
            "min_clearance": 0.25, "min_track_width": 0.2, "min_copper_edge_clearance": 0.5,
            "min_hole_clearance": 0.1}}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "b.kicad_pcb"
            path.with_suffix(".kicad_dru").write_text('(version 1)\n(rule "x" (constraint clearance (min 1mm)))')
            problems, notes = preflight(board(OUTLINE), snapshot(part("R1", (10, 10)), path=path), project)
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 3)
        self.assertIn("minimum clearance 0.25 mm", notes[0])
        self.assertIn("copper-to-edge clearance 0.5 mm", notes[1])
        self.assertIn("b.kicad_dru", notes[2])

    def test_real_fixture_board_has_an_outline(self):
        root = parse((FIXTURES / "necessity.kicad_pcb").read_text(encoding="utf-8"), kicad=True)
        self.assertIsInstance(outline_box(root), tuple)


if __name__ == "__main__":
    unittest.main()
