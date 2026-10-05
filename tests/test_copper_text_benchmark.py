import unittest

from velatrace.copper_text_benchmark import copper_text_fixture, run_copper_text_benchmark
from velatrace.errors import ValidationError
from velatrace.sexpr import parse, children, one


class CopperBenchmarkTests(unittest.TestCase):
    def test_authored_source_keeps_layer_count_and_models_export_omission(self):
        for count in (4, 6, 8):
            with self.subTest(layers=count):
                files = copper_text_fixture(count)
                board = parse(files['reference.kicad_pcb'], kicad=True)
                dsn = parse(files['reference.dsn'])
                self.assertEqual(len(children(one(dsn, 'structure'), 'layer')), count)
                text = one(board, 'gr_text')
                self.assertEqual(text[1], 'I')
                self.assertEqual(one(text, 'layer')[1], 'F.Cu')
                self.assertFalse(children(board, 'zone'))
                self.assertFalse(children(one(dsn, 'structure'), 'keepout'))
                self.assertEqual(files, copper_text_fixture(count))

    def test_invalid_controls_refused_before_creating_output(self):
        for settings in ({'repeats': 0}, {'repeats': True}, {'repeats': 10}, {'seconds': float('nan')},
                         {'seconds': 0}, {'layer_counts': ()}, {'layer_counts': (4, 4)},
                         {'layer_counts': (3,)}, {'surface_only': 1}):
            with self.subTest(settings=settings), self.assertRaises(ValidationError):
                run_copper_text_benchmark('unused', jar=None, java=None, kicad_cli=None, **settings)


if __name__ == '__main__':
    unittest.main()
