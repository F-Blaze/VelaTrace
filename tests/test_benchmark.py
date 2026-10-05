"""Authored corpus consistency; external engine runs are explicit integration checks."""
import json
from pathlib import Path
import tempfile
import unittest

from velatrace.benchmark import _FixtureSafety, fixture_texts, run_benchmark
from velatrace.candidate import board_nets, canonical, trusted_via_catalog
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import ValidationError
from velatrace.sexpr import children, one, parse


class BenchmarkTests(unittest.TestCase):
    def test_all_fixtures_have_identical_board_dsn_connectivity_and_physical_layers(self):
        for count in (4, 6, 8):
            for dense in (False, True):
                for obstacles in (False, True):
                    texts = fixture_texts(count, dense=dense, obstacles=obstacles)
                    self.assertEqual(texts, fixture_texts(count, dense=dense, obstacles=obstacles))
                    board, dsn = parse(texts['crossed.kicad_pcb'], kicad=True), parse(texts['crossed.dsn'])
                    board_layers = [row[1] for row in one(board, 'layers')[1:] if row[2] == 'signal']
                    dsn_layers = [row[1] for row in children(one(dsn, 'structure'), 'layer')]
                    self.assertEqual(board_layers, dsn_layers)
                    self.assertEqual(len(board_layers), count)
                    net_rows = children(one(dsn, 'network'), 'net')
                    self.assertEqual(set(board_nets(board)) - {''}, {row[1] for row in net_rows})
                    actual = {}
                    places = {}
                    for component in children(one(dsn, 'placement'), 'component'):
                        places.update({row[1]: row for row in children(component, 'place')})
                    footprint_refs = set()
                    dsn_library = one(dsn, 'library')
                    images = {row[1]: row for row in children(dsn_library, 'image')}
                    padstacks = {row[1]: row for row in children(dsn_library, 'padstack')}
                    for fp in children(board, 'footprint'):
                        ref = next(row[2] for row in children(fp, 'property') if row[1] == 'Reference')
                        footprint_refs.add(ref)
                        at = one(fp, 'at')
                        self.assertAlmostEqual(float(at[1]), float(places[ref][2])*.001)
                        self.assertAlmostEqual(float(at[2]), -float(places[ref][3])*.001)
                        self.assertEqual(places[ref][4], 'front')
                        self.assertEqual(float(places[ref][5]), 0)
                        for pad in children(fp, 'pad'):
                            pad_net = one(pad, 'net')[2]
                            if pad_net:
                                actual.setdefault(pad_net, set()).add(ref+'-'+pad[1])
                            else:
                                self.assertTrue(obstacles)
                                self.assertEqual(one(pad, 'layers')[1:], ['F.Cu', 'F.Paste', 'F.Mask'])
                            image_name = 'BenchmarkObstacle' if ref.startswith('O') else 'BenchmarkPad'
                            image_pin = one(images[image_name], 'pin')
                            self.assertEqual(pad[1], image_pin[2])
                            self.assertEqual(float(image_pin[3]), 0)
                            self.assertEqual(float(image_pin[4]), 0)
                            padstack = padstacks[image_pin[1]]
                            circle = one(one(padstack, 'shape'), 'circle')
                            size = one(pad, 'size')
                            self.assertEqual(circle[1], 'F.Cu')
                            self.assertAlmostEqual(float(size[1]), float(circle[2]) / 1000)
                            self.assertAlmostEqual(float(size[2]), float(circle[2]) / 1000)
                    self.assertEqual(footprint_refs, set(places))
                    self.assertEqual(actual, {row[1]: set(one(row, 'pins')[1:]) for row in net_rows})
                    obstacle_refs = {ref for ref in footprint_refs if ref.startswith('O')}
                    self.assertEqual(len(obstacle_refs), 2 if obstacles else 0)
                    obstacle_pins = {place[1] + '-1' for component in children(one(dsn, 'placement'), 'component')
                                     if component[1] == 'BenchmarkObstacle' for place in children(component, 'place')}
                    self.assertTrue(obstacle_pins.isdisjoint(
                        pin for row in net_rows for pin in one(row, 'pins')[1:]))
                    self.assertEqual(json.loads(texts['crossed.kicad_pro'])['board']['design_settings']['drc_exclusions'], [])

    def test_fixture_adapter_refuses_stale_files_and_cannot_apply(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            for filename, content in fixture_texts(4).items():
                (folder/filename).write_text(content, encoding='utf-8')
            path, board = folder/'crossed.dsn', folder/'crossed.kicad_pcb'
            dsn = DsnInput(path, file_digest(path), ExportTicket.begin(board), frozenset(),
                           frozenset(('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu')))
            safety = _FixtureSafety(folder)
            self.assertFalse(hasattr(safety, 'apply'))
            original = safety.assert_matches(dsn)
            safety.assert_matches(dsn, expected_board=canonical(parse(original, kicad=True)))
            self.assertEqual(len(trusted_via_catalog(dsn)), 1)
            board.write_text(original+'\n', encoding='utf-8')
            with self.assertRaises(ValidationError):
                safety.assert_matches(dsn)

    def test_existing_output_and_invalid_budgets_refused_before_tools(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(ValidationError):
                fixture_texts(4, obstacles=1)
            with self.assertRaises(ValidationError):
                run_benchmark(Path(name)/'invalid-obstacles', jar=Path('none'), java=Path('none'),
                              kicad_cli=Path('none'), obstacles=1)
            for seconds in (float('nan'), 0, 3601, True):
                with self.assertRaises(ValidationError):
                    run_benchmark(Path(name), jar=Path('none'), java=Path('none'),
                                  kicad_cli=Path('none'), seconds=seconds)
            with self.assertRaises(FileExistsError):
                run_benchmark(Path(name), jar=Path('none'), java=Path('none'), kicad_cli=Path('none'))

    def test_solver_selection_refuses_unavailable_backends_before_creating_output(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name)/'not-created'
            for solvers in ((), ('krt',), ('unknown',), ('baseline', 'baseline'), ['baseline']):
                with self.subTest(solvers=solvers), self.assertRaises(ValidationError):
                    run_benchmark(output, jar=Path('none'), java=Path('none'),
                                  kicad_cli=Path('none'), solvers=solvers)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
