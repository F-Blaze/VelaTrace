import json
from pathlib import Path
import tempfile
import unittest

from velatrace.copper_text import extract_copper_text, stroke_obstacles, prepare_copper_text_dsn
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import CapabilityError, ValidationError
from velatrace.sexpr import children, parse

SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" viewBox="0 0 297 210">
<g style="fill:none;stroke:#000;stroke-width:0.6;stroke-opacity:1;stroke-linecap:round;stroke-linejoin:round">
<g transform="rotate(-90 10 20)"><text opacity="0" transform="scale(-1 1)">ignored label</text></g>
<path d="M 10 20 L 13 24 M 40 50 L 40 51"/>
</g></svg>'''
BOARD = '''(kicad_pcb (version 20241229) (generator "pcbnew") (paper "A4")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user))
 (gr_text "physical copper" (at 10 20 90) (layer "B.Cu")
  (effects (font (size 1 1) (thickness 0.2)) (justify mirror)))
 (gr_text "silk only" (at 1 2) (layer "F.SilkS") (effects (font (size 1 1))))
 (footprint "R" (layer "F.Cu") (at 3 4) (property "Reference" "R1" (layer "F.SilkS"))))'''


class CopperStrokeTests(unittest.TestCase):
    def test_encloses_stroke_width_and_serialization_without_bridging_move(self):
        boxes = stroke_obstacles(SVG, 'B.Cu')
        self.assertEqual(len(boxes), 2)
        first, second = boxes
        self.assertEqual(first.layer, 'B.Cu')
        self.assertAlmostEqual(first.xmin_mm, 9.6999)
        self.assertAlmostEqual(first.ymin_mm, 19.6999)
        self.assertAlmostEqual(first.xmax_mm, 13.3001)
        self.assertAlmostEqual(first.ymax_mm, 24.3001)
        self.assertAlmostEqual(second.xmin_mm, 39.6999)
        self.assertAlmostEqual(second.xmax_mm, 40.3001)

    def test_implicit_lines_and_reversed_duplicates(self):
        text = SVG.replace('M 10 20 L 13 24 M 40 50 L 40 51', 'M 10 20 13 24 M 13 24 10 20')
        self.assertEqual(len(stroke_obstacles(text, 'F.Cu')), 1)

    def test_fixed_kicad_doctype_stripped_without_loading_it(self):
        declaration = '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"\n "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
        self.assertEqual(stroke_obstacles(declaration+SVG, 'F.Cu'), stroke_obstacles(SVG, 'F.Cu'))
        with self.assertRaises(ValidationError):
            stroke_obstacles('<!DOCTYPE svg [<!ENTITY a SYSTEM "file:///private">]>'+SVG, 'F.Cu')

    def test_refuses_curves_units_shift_visible_text_and_transformed_geometry(self):
        changes = (
            ('L 13 24', 'C 11 21 12 23 13 24'),
            ('width="297mm"', 'width="297px"'),
            ('viewBox="0 0 297 210"', 'viewBox="1 0 297 210"'),
            ('opacity="0"', 'opacity="1"'),
            ('<path d=', '<path transform="translate(1 0)" d='),
            ('fill:none', 'fill:none;transform:translate(10px,0)'),
            ('fill:none', 'fill:none;TrAnSfOrM:translate(10px,0)'),
            ('fill:none', 'fill:none;d:path("M0,0 L9,9")'),
            ('fill:none', 'fill:none;scale:2'),
            ('<path d=', '<svg x="20" viewBox="0 0 1 1"></svg><path d='),
            ('fill:none', 'fill:#000'),
            ('stroke-linecap:round', 'stroke-linecap:square'),
            ('stroke-width:0.6', 'stroke-width:nan'),
            ('L 13 24', 'L 13'),
            ('<path d=', '<image href="https://example.com/secret"/><path d='),
        )
        for before, after in changes:
            with self.subTest(before=before), self.assertRaises((ValidationError, CapabilityError)):
                stroke_obstacles(SVG.replace(before, after), 'F.Cu')


class FakeCli:
    version = (10, 0, 4)

    def __init__(self, original, mutation=None):
        self.original = original
        self.mutation = mutation
        self.calls = []

    def _export_config_home(self):
        return None

    def _run(self, args, cwd, allowed, home):
        staged = Path(args[-1])
        self.calls.append((args, staged.read_text(encoding='utf-8')))
        if staged == self.original:
            raise AssertionError('Exporter received original board')
        Path(args[args.index('--output')+1]).write_text(SVG, encoding='utf-8')
        if self.mutation == 'board':
            self.original.write_text(BOARD+'\n', encoding='utf-8')
        elif self.mutation == 'project':
            self.original.with_suffix('.kicad_pro').write_text('{}', encoding='utf-8')
        elif self.mutation == 'new-rules':
            self.original.with_suffix('.kicad_dru').write_text('(version 1)', encoding='utf-8')
        elif self.mutation == 'tool':
            self.version = (10, 0, 5)


class CopperTextExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.board = Path(self.temp.name)/'board.kicad_pcb'
        self.board.write_text(BOARD, encoding='utf-8')
        self.project = self.board.with_suffix('.kicad_pro')
        self.project.write_text(json.dumps({'board': {'design_settings': {}}}), encoding='utf-8')

    def test_export_isolated_text_and_bind_source_context(self):
        cli = FakeCli(self.board)
        before = self.board.read_bytes(), self.project.read_bytes()
        result = extract_copper_text(self.board, cli)
        self.assertEqual(result.text_items, 1)
        self.assertEqual(len(result.obstacles), 2)
        self.assertEqual(result.tool_version, (10, 0, 4))
        self.assertEqual(len(result.board_digest), 64)
        self.assertEqual(len(result.context_digest), 64)
        self.assertEqual(before, (self.board.read_bytes(), self.project.read_bytes()))
        args, snapshot = cli.calls[0]
        self.assertEqual(args[args.index('--layers')+1], 'B.Cu')
        root = parse(snapshot, kicad=True)
        self.assertEqual(len(children(root, 'gr_text')), 1)
        self.assertFalse(children(root, 'footprint'))
        self.assertFalse(children(root, 'segment'))

    def test_changed_source_context_absence_or_tool_is_refused(self):
        original = self.project.read_bytes()
        for mutation in ('board', 'project', 'tool', 'new-rules'):
            with self.subTest(mutation=mutation):
                self.board.write_text(BOARD, encoding='utf-8')
                self.project.write_bytes(original)
                with self.assertRaises(ValidationError):
                    extract_copper_text(self.board, FakeCli(self.board, mutation))

    def test_unknown_footprint_copper_text_and_variables_are_refused(self):
        for text in (BOARD.replace('physical copper', '${TITLE}'),
                     BOARD.replace('(property "Reference" "R1" (layer "F.SilkS"))',
                                   '(property "Reference" "R1" (layer "F.Cu"))')):
            with self.subTest(text=text):
                self.board.write_text(text, encoding='utf-8')
                cli = FakeCli(self.board)
                with self.assertRaises(CapabilityError):
                    extract_copper_text(self.board, cli)
                self.assertFalse(cli.calls)

    def test_no_copper_text_needs_no_export(self):
        self.board.write_text(BOARD.replace('(layer "B.Cu")', '(layer "F.SilkS")'), encoding='utf-8')
        cli = FakeCli(self.board)
        result = extract_copper_text(self.board, cli)
        self.assertEqual(result.obstacles, ())
        self.assertFalse(cli.calls)

    def test_prepared_export_preserves_name_and_remains_bound_to_original(self):
        path = self.board.with_suffix('.dsn')
        path.write_text('(pcb board (unit mm) (structure (layer F.Cu (type signal)) (layer B.Cu (type signal))))',
                        encoding='utf-8')
        dsn = DsnInput(path, file_digest(path), ExportTicket.begin(self.board), frozenset(),
                       frozenset({'F.Cu', 'B.Cu'}), 'board')
        cli = FakeCli(self.board)
        output = Path(self.temp.name)/'prepared'
        result = prepare_copper_text_dsn(dsn, cli, output)
        self.assertEqual(result.dsn.path.name, path.name)
        self.assertEqual(result.dsn.base_design, dsn.base_design)
        self.assertNotEqual(result.dsn.digest, dsn.digest)
        result.assert_current(cli)
        with self.assertRaises(FileExistsError):
            prepare_copper_text_dsn(dsn, cli, output)
        path.write_text(path.read_text(encoding='utf-8')+' ', encoding='utf-8')
        with self.assertRaises(ValidationError):
            result.assert_current(cli)


if __name__ == '__main__':
    unittest.main()
