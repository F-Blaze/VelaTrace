import unittest

from velatrace.errors import ValidationError
from velatrace.reference_planes import ReferenceRequirement, geometry_library, reference_regions
from velatrace.reference_routing import _merge_convex_neighbors, compile_reference_keepouts
from velatrace.sexpr import children, one, parse


REQ = ReferenceRequirement('SIG', 'F.Cu', 'In1.Cu', 'GND')
DSN = '''(pcb reference (unit mm) (structure
 (layer F.Cu (type signal)) (layer In1.Cu (type signal))
 (boundary (rect pcb 0 -10 10 0)) (rule (width 0.25) (clearance 0.2)))
 (network (net SIG (pins J1-1 J2-1))) (wiring))'''


def board(contour):
    points = ' '.join(f'(xy {x} {y})' for x, y in contour)
    return f'''(kicad_pcb (version 20241229) (layers
 (0 "F.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal))
 (net 1 "SIG") (net 2 "GND")
 (zone (net 2) (net_name "GND") (layer "In1.Cu")
  (filled_polygon (layer "In1.Cu") (pts {points}))))'''


def keepout_union(projection):
    shape = geometry_library()
    structure = one(parse(projection.dsn_text), 'structure')
    polygons = []
    for keepout in children(structure, 'keepout'):
        row = one(keepout, 'polygon')
        coords = [(float(row[i]), -float(row[i+1])) for i in range(3, len(row), 2)]
        polygons.append(shape.Polygon(coords))
    return shape.union_all(polygons)


class ReferenceProjectionSpeedTests(unittest.TestCase):
    def test_convex_merging_reduces_a_large_exact_triangulation(self):
        shape = geometry_library()
        triangles = []
        for x in range(12):
            for y in range(12):
                triangles.extend(shape.constrained_delaunay_triangles(
                    shape.box(x, y, x + 1, y + 1)).geoms)

        merged = _merge_convex_neighbors(triangles)

        self.assertLessEqual(len(merged), 4)
        self.assertTrue(shape.union_all(merged).equals(shape.box(0, 0, 12, 12)))
        self.assertTrue(all(p.equals(p.convex_hull) for p in merged))

    def test_projection_preserves_hole_and_tiny_slit_without_crossing_board(self):
        shape = geometry_library()
        # A zero-width out-and-back bridge encodes the 2 x 2 hole.
        holed = board([(0, 0), (10, 0), (10, 10), (0, 10), (0, 5), (4, 5),
                       (4, 4), (6, 4), (6, 6), (4, 6), (4, 5), (0, 5)])
        projection = compile_reference_keepouts(DSN, holed, (REQ,), merge_convex=True)
        expected = shape.box(0, 0, 10, 10).difference(reference_regions(holed, REQ))
        voids = keepout_union(projection)
        self.assertTrue(voids.equals(expected))
        self.assertTrue(shape.box(0, 0, 10, 10).covers(voids))
        self.assertTrue(voids.covers(shape.Point(5, 5)))
        emitted = [shape.Polygon([(float(p[i]), -float(p[i+1])) for i in range(3, len(p), 2)])
                   for k in children(one(parse(projection.dsn_text), 'structure'), 'keepout')
                   for p in [one(k, 'polygon')]]
        self.assertTrue(all(p.equals(p.convex_hull) for p in emitted))

        # A 0.001-wide notch in the copper leaves a representable narrow void.
        notched = board([(0, 0), (10, 0), (10, 10), (5.001, 10), (5.001, 4),
                         (5, 4), (5, 10), (0, 10)])
        projection = compile_reference_keepouts(DSN, notched, (REQ,), merge_convex=True)
        expected = shape.box(0, 0, 10, 10).difference(reference_regions(notched, REQ))
        voids = keepout_union(projection)
        self.assertTrue(voids.equals(expected))
        self.assertTrue(voids.covers(shape.Point(5.0005, 7)))
        self.assertFalse(voids.covers(shape.Point(4.999, 7)))
        self.assertTrue(shape.box(0, 0, 10, 10).covers(voids))

    def test_unmerged_mode_retains_reference_triangulation(self):
        contour = [(0, 0), (10, 0), (10, 10), (5.001, 10), (5.001, 4),
                   (5, 4), (5, 10), (0, 10)]
        filled = board(contour)
        merged = compile_reference_keepouts(DSN, filled, (REQ,), merge_convex=True)
        original = compile_reference_keepouts(DSN, filled, (REQ,), merge_convex=False)
        self.assertLess(merged.keepout_count, original.keepout_count)
        self.assertTrue(keepout_union(merged).equals(keepout_union(original)))
        self.assertEqual(compile_reference_keepouts(DSN, filled, (REQ,)).keepout_count, original.keepout_count)
        with self.assertRaises(ValidationError):
            compile_reference_keepouts(DSN, filled, (REQ,), merge_convex=1)


if __name__ == '__main__':
    unittest.main()
