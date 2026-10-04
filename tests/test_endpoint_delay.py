from dataclasses import replace
import unittest

from velatrace.endpoint_delay import EndpointRequirement, analyze_endpoint_delay
from velatrace.reference_benchmark import reference_fixture
from velatrace.ses import RoutePlan, Track, Via, ViaSpec
from velatrace.sexpr import children, one, parse, render
from velatrace.stackup import read_stackup, thin_microstrip


REQ = EndpointRequirement('N1', 'J1', '1', 'J5', '1', 'GND')


def plan(*tracks, vias=()):
    return RoutePlan('reference', tuple(tracks) or (Track('N1', 'F.Cu', .25, ((15., -40.), (65., -40.))),), vias)


class EndpointDelayTests(unittest.TestCase):
    def setUp(self):
        self.board = reference_fixture(4)['reference.kicad_pcb']

    def test_uses_endpoint_path_not_total_copper_and_tracks_width_changes(self):
        route = plan(Track('N1', 'F.Cu', .25, ((15., -40.), (40., -40.))),
                     Track('N1', 'F.Cu', .5, ((40., -40.), (65., -40.))),
                     Track('N1', 'F.Cu', .25, ((40., -40.), (40., -55.))),
                     Track('OTHER', 'B.Cu', .25, ((0., 0.), (100., 0.))))
        result = analyze_endpoint_delay(self.board, route, REQ)
        self.assertEqual(result.status, 'estimated', result.details)
        self.assertEqual(result.path_length_mm, 50)
        dielectric = next(l for l in read_stackup(self.board).layers if l.kind == 'dielectric').sublayers[0]
        expected = sum(25*thin_microstrip(w, dielectric.thickness_nm/1e6, dielectric.epsilon_r).delay_ps_per_mm
                       for w in (.25, .5))
        self.assertAlmostEqual(result.estimated_delay_ps, expected)
        self.assertFalse(result.electrically_verified)
        self.assertIn('Advisory', result.limitations[0])
        reversed_duplicate = replace(route, tracks=route.tracks+(replace(route.tracks[0], points_mm=((40., -40.), (15., -40.))),))
        self.assertEqual(analyze_endpoint_delay(self.board, reversed_duplicate, REQ).estimated_delay_ps, expected)

    def test_wrong_y_direction_and_disconnected_or_ambiguous_paths_are_unknown(self):
        routes = [plan(Track('N1', 'F.Cu', .25, ((15., 40.), (65., 40.)))),
                  plan(Track('N1', 'F.Cu', .25, ((15., -40.), (30., -40.)))),
                  plan(Track('N1', 'F.Cu', .25, ((15., -40.), (65., -40.))),
                       Track('N1', 'F.Cu', .25, ((40., -40.), (40., -55.)))),
                  plan(Track('N1', 'F.Cu', .25, ((15., -40.), (65., -40.), (65., -45.), (15., -40.)))),
                  plan(vias=(Via('N1', (15., -40.), ViaSpec(.6, .3, ('F.Cu', 'B.Cu'))),)),
                  plan(Track('N1', 'B.Cu', .25, ((15., -40.), (65., -40.))))]
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(analyze_endpoint_delay(self.board, route, REQ).status, 'unknown')

    def test_missing_material_and_unsupported_or_ambiguous_pins_stay_unknown(self):
        roots = []
        missing = parse(self.board, kicad=True)
        dielectric = next(l for l in children(one(one(missing, 'setup'), 'stackup'), 'layer')
                          if l[1] == 'dielectric 1')
        dielectric.remove(one(dielectric, 'epsilon_r'))
        roots.append(missing)
        for mutation in ('rotated', 'back', 'wrong-net', 'duplicate'):
            root = parse(self.board, kicad=True)
            fp = children(root, 'footprint')[0]
            if mutation == 'rotated':
                one(fp, 'at').append('90')
            elif mutation == 'back':
                one(fp, 'layer')[1] = 'B.Cu'
            elif mutation == 'wrong-net':
                one(one(fp, 'pad'), 'net')[2] = 'OTHER'
            else:
                root.append(fp)
            roots.append(root)
        for root in roots:
            self.assertEqual(analyze_endpoint_delay(render(root), plan(), REQ).status, 'unknown')
        self.assertEqual(analyze_endpoint_delay(self.board, plan(), replace(REQ, end_pad='missing')).status, 'unknown')

    def test_inline_net_format_and_hashes_bind_changed_material_or_requirement(self):
        root = parse(self.board, kicad=True)
        root[:] = [r for r in root if not isinstance(r, list) or r[0] != 'net']
        for fp in children(root, 'footprint'):
            for pad in children(fp, 'pad'):
                net = one(pad, 'net')
                net[:] = ['net', net[2]]
        zone = one(root, 'zone')
        one(zone, 'net')[1] = one(zone, 'net_name')[1]
        result = analyze_endpoint_delay(render(root), plan(), REQ)
        self.assertEqual(result.status, 'estimated', result.details)
        baseline = analyze_endpoint_delay(self.board, plan(), REQ)
        root = parse(self.board, kicad=True)
        dielectric = next(l for l in children(one(one(root, 'setup'), 'stackup'), 'layer')
                          if l[1] == 'dielectric 1')
        one(dielectric, 'epsilon_r')[1] = '5'
        changed = analyze_endpoint_delay(render(root), plan(), REQ)
        self.assertNotEqual(baseline.board_digest, changed.board_digest)
        self.assertNotEqual(baseline.stackup_digest, changed.stackup_digest)
        self.assertNotEqual(baseline.estimated_delay_ps, changed.estimated_delay_ps)
        changed_req = analyze_endpoint_delay(self.board, plan(), replace(REQ, end_pad='2'))
        self.assertNotEqual(baseline.requirement_digest, changed_req.requirement_digest)


if __name__ == '__main__':
    unittest.main()
