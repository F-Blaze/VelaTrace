import unittest

from velatrace.portfolio import candidate_geometry
from velatrace.route_cleanup import consolidate_collinear
from velatrace.ses import RoutePlan, Track


class CleanupTests(unittest.TestCase):
    def test_union_preserves_copper_and_never_fills_gaps(self):
        for direction in ((1, 0), (0, 1), (1, 1), (2, -3)):
            def points(a, b):
                return tuple((t * direction[0], t * direction[1]) for t in (a, b))
            plan = RoutePlan('board', tuple(Track('A', 'F.Cu', .25, points(a, b))
                             for a, b in ((0, 4), (3, 1), (4, 6), (8, 9))), ())
            clean = consolidate_collinear(plan)
            self.assertEqual(len(clean.tracks), 2)
            self.assertEqual(candidate_geometry(plan), candidate_geometry(clean))
            self.assertEqual(consolidate_collinear(clean), clean)

    def test_different_nets_layers_widths_and_real_branch_survive(self):
        tracks = (Track('A', 'F.Cu', .25, ((0, 0), (4, 0))),
                  Track('B', 'F.Cu', .25, ((0, 0), (4, 0))),
                  Track('A', 'B.Cu', .25, ((0, 0), (4, 0))),
                  Track('A', 'F.Cu', .3, ((0, 0), (4, 0))),
                  Track('A', 'F.Cu', .25, ((2, 0), (2, 1))))
        plan = RoutePlan('board', tracks, ())
        self.assertEqual(len(consolidate_collinear(plan).tracks), 5)
        self.assertEqual(candidate_geometry(consolidate_collinear(plan)), candidate_geometry(plan))


if __name__ == '__main__':
    unittest.main()
