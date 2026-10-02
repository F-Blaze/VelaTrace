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
                texts = fixture_texts(count, dense=dense)
                self.assertEqual(texts, fixture_texts(count, dense=dense))
                board, dsn = parse(texts['crossed.kicad_pcb'], kicad=True), parse(texts['crossed.dsn'])
                board_layers = [row[1] for row in one(board, 'layers')[1:] if row[2] == 'signal']
                dsn_layers = [row[1] for row in children(one(dsn, 'structure'), 'layer')]
                self.assertEqual(board_layers, dsn_layers)
                self.assertEqual(len(board_layers), count)
                net_rows = children(one(dsn, 'network'), 'net')
                self.assertEqual(set(board_nets(board)) - {''}, {row[1] for row in net_rows})
                actual = {}
                places = {row[1]: row for row in children(one(one(dsn, 'placement'), 'component'), 'place')}
                for fp in children(board, 'footprint'):
                    ref = next(row[2] for row in children(fp, 'property') if row[1] == 'Reference')
                    at = one(fp, 'at')
                    self.assertAlmostEqual(float(at[1]), float(places[ref][2])*.001)
                    self.assertAlmostEqual(float(at[2]), -float(places[ref][3])*.001)
                    for pad in children(fp, 'pad'):
                        actual.setdefault(one(pad, 'net')[2], set()).add(ref+'-'+pad[1])
                self.assertEqual(actual, {row[1]: set(one(row, 'pins')[1:]) for row in net_rows})
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
            for seconds in (float('nan'), 0, 3601, True):
                with self.assertRaises(ValidationError):
                    run_benchmark(Path(name), jar=Path('none'), java=Path('none'),
                                  kicad_cli=Path('none'), seconds=seconds)
            with self.assertRaises(FileExistsError):
                run_benchmark(Path(name), jar=Path('none'), java=Path('none'), kicad_cli=Path('none'))


if __name__ == '__main__':
    unittest.main()
