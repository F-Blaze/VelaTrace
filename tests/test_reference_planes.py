from dataclasses import replace
import unittest

from velatrace.errors import ValidationError
from velatrace.reference_planes import ReferenceRequirement, check_reference_coverage, reference_regions
from velatrace.reference_routing import compile_reference_keepouts
from velatrace.ses import RoutePlan, Track, Via, ViaSpec
from velatrace.sexpr import children, one, parse, render


REQ = ReferenceRequirement('SIG', 'F.Cu', 'In1.Cu', 'GND')
BOARD = '''(kicad_pcb (version 20241229) (layers
 (0 "F.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal))
 (net 1 "SIG") (net 2 "GND")
 (zone (net 2) (net_name "GND") (layer "In1.Cu")
  (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))))'''
DSN = '''(pcb reference (unit mm) (structure
 (layer F.Cu (type signal)) (layer In1.Cu (type signal))
 (layer In2.Cu (type signal)) (layer B.Cu (type signal))
 (boundary (rect pcb 0 -10 10 0)) (rule (width 0.25) (clearance 0.2)))
 (network (net SIG (pins J1-1 J2-1))) (wiring))'''


def plan(a=(1, -5), b=(9, -5), width=.25):
    return RoutePlan('reference', (Track('SIG', 'F.Cu', width, (a, b)),), ())


class ReferenceCoverageTests(unittest.TestCase):
    def test_full_width_coverage_and_coordinate_direction(self):
        report = check_reference_coverage(plan(), BOARD, (REQ,))
        self.assertEqual(report.status, 'covered')
        self.assertFalse(report.electrically_verified)
        self.assertEqual(check_reference_coverage(plan((1, 5), (9, 5)), BOARD, (REQ,)).status, 'gap')
        self.assertEqual(check_reference_coverage(plan((1, -.1), (9, -.1)), BOARD, (REQ,)).status, 'gap')
        self.assertEqual(check_reference_coverage(plan((1, -.3), (9, -.3)), BOARD,
                                                 (replace(REQ, edge_margin_mm=.2),)).status, 'gap')

    def test_sub_sampling_width_notch_cannot_be_skipped(self):
        notch = BOARD.replace('(xy 10 10) (xy 0 10)',
                              '(xy 10 10) (xy 5.001 10) (xy 5.001 4) (xy 5 4) (xy 5 10) (xy 0 10)')
        report = check_reference_coverage(plan(), notch, (REQ,))
        self.assertEqual(report.status, 'gap')
        self.assertEqual(report.checks[0].affected_edges, ((0, 0),))
        projection = compile_reference_keepouts(DSN, notch, (REQ,))
        self.assertGreater(projection.keepout_count, 0)
        projected = parse(projection.dsn_text)
        self.assertEqual(one(projected, 'network'), one(parse(DSN), 'network'))
        self.assertTrue(all(one(k, 'polygon')[1] == 'F.Cu'
                            for k in children(one(projected, 'structure'), 'keepout')))

    def test_missing_conflicting_invalid_and_isolated_fill_are_unknown(self):
        changes = [BOARD.replace('filled_polygon', 'polygon'),
                   BOARD.replace('(net_name "GND")', '(net_name "OTHER")'),
                   BOARD.replace('(filled_polygon', '(filled_polygon (island)'),
                   BOARD.replace('(xy 10 0) (xy 10 10)', '(xy 10 10) (xy 10 0)'),
                   BOARD.replace('(xy 10 10) (xy 0 10)', '(xy 0 0) (xy 0 0)')]
        for text in changes:
            with self.subTest(text=text):
                self.assertEqual(check_reference_coverage(plan(), text, (REQ,)).status, 'unknown')
        for req in (replace(REQ, reference_net='POWER'), replace(REQ, reference_layer='In2.Cu')):
            self.assertEqual(check_reference_coverage(plan(), BOARD, (req,)).status, 'unknown')

    def test_inline_nets_and_via_transitions(self):
        text = BOARD.replace('(net 1 "SIG") (net 2 "GND")', '').replace('(net 2) (net_name "GND")', '(net "GND")')
        self.assertEqual(check_reference_coverage(plan(), text, (REQ,)).status, 'covered')
        routed = replace(plan(), vias=(Via('SIG', (1, -5), ViaSpec(.6, .3, ('F.Cu', 'B.Cu'))),))
        self.assertEqual(check_reference_coverage(routed, text, (REQ,)).status, 'unknown')

    def test_exact_reversed_hole_bridges_preserve_voids(self):
        # A square hole reached by an out-and-back zero-width bridge.
        bridged = BOARD.replace('(xy 10 10) (xy 0 10)',
            '(xy 10 10) (xy 0 10) (xy 0 5) (xy 4 5) (xy 4 4) (xy 6 4) '
            '(xy 6 6) (xy 4 6) (xy 4 5) (xy 0 5)')
        self.assertAlmostEqual(reference_regions(bridged, REQ).area, 96)
        reversed_root = parse(bridged, kicad=True)
        points = one(one(one(reversed_root, 'zone'), 'filled_polygon'), 'pts')
        points[1:] = reversed(points[1:])
        self.assertTrue(reference_regions(render(reversed_root), REQ).equals(reference_regions(bridged, REQ)))
        self.assertEqual(check_reference_coverage(plan(), bridged, (REQ,)).status, 'gap')
        self.assertEqual(check_reference_coverage(plan((1, -2), (9, -2)), bridged, (REQ,)).status, 'covered')
        projection = compile_reference_keepouts(DSN, bridged, (REQ,))
        from velatrace.reference_planes import geometry_library
        shape = geometry_library()
        polygons = [one(k, 'polygon') for k in children(one(parse(projection.dsn_text), 'structure'), 'keepout')]
        voids = shape.union_all([shape.Polygon([(float(p[i]), -float(p[i+1]))
                                                for i in range(3, len(p), 2)]) for p in polygons])
        self.assertAlmostEqual(voids.area, 4)
        self.assertTrue(voids.covers(shape.Point(5, 5)))
        # Moving only one end of the return bridge must not silently repair it.
        malformed = bridged.replace('(xy 4 5) (xy 0 5)', '(xy 4 5) (xy 0 5.1)')
        self.assertEqual(check_reference_coverage(plan(), malformed, (REQ,)).status, 'unknown')

    def test_bad_requirements_and_boundary_refused(self):
        for margin in (True, -1, float('nan')):
            with self.assertRaises(ValidationError):
                replace(REQ, edge_margin_mm=margin)
        with self.assertRaises(ValidationError):
            check_reference_coverage(plan(), BOARD, ())
        with self.assertRaises(ValidationError):
            compile_reference_keepouts(DSN.replace('(rect pcb 0 -10 10 0)', '(circle pcb 10 5 -5)'), BOARD, (REQ,))
        with self.assertRaises(ValidationError):
            reference_regions(BOARD.replace('(zone (net 2)', '(zone (net 3)'), REQ)

    def test_authored_reference_fixtures_match_dsn_and_preserve_layer_count(self):
        from velatrace.reference_benchmark import reference_fixture
        from velatrace.stackup import read_stackup
        for count in (4, 6, 8):
            with self.subTest(layers=count):
                files = reference_fixture(count)
                pcb = parse(files['reference.kicad_pcb'], kicad=True)
                dsn = parse(files['reference.dsn'])
                layers = read_stackup(files['reference.kicad_pcb']).copper_layers
                self.assertEqual(len(layers), count)
                self.assertEqual(layers, tuple(r[1] for r in children(one(dsn, 'structure'), 'layer')))
                placed = {p[1]: (float(p[2])/1000, -float(p[3])/1000)
                          for c in children(one(dsn, 'placement'), 'component') for p in children(c, 'place')}
                self.assertEqual(set(placed), {'J1', 'J2', 'J5'})
                for fp in children(pcb, 'footprint'):
                    ref = next(p[2] for p in children(fp, 'property') if p[1] == 'Reference')
                    self.assertEqual(tuple(map(float, one(fp, 'at')[1:])), placed[ref])
                    if ref == 'J2':
                        self.assertEqual(one(fp, 'pad')[2], 'thru_hole')
                        self.assertEqual(one(one(fp, 'pad'), 'net')[2], 'GND')
                self.assertEqual(one(one(pcb, 'zone'), 'layer')[1], 'In1.Cu')
        for count in (2, 5, True):
            with self.assertRaises(ValidationError):
                reference_fixture(count)


if __name__ == '__main__':
    unittest.main()
