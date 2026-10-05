"""Optional real CLI parsing and DRC; never edits the source fixture."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from velatrace.errors import ValidationError
from velatrace.kicad_cli import KiCadCli

FIXTURES = Path(__file__).parent / 'fixtures' / 'audit'


@unittest.skipUnless(os.getenv('VELATRACE_TEST_KICAD_CLI'), 'Set VELATRACE_TEST_KICAD_CLI for real KiCad CLI DRC')
class NativeKiCadTests(unittest.TestCase):
    def test_real_cli_loads_board_and_counts_incomplete_routing(self):
        original = {suffix: (FIXTURES / ('necessity' + suffix)).read_bytes()
                    for suffix in ('.kicad_pcb', '.kicad_pro')}
        with tempfile.TemporaryDirectory(prefix='velatrace-native-kicad-') as directory:
            root = Path(directory)
            board = root / 'necessity.kicad_pcb'
            for suffix in original:
                shutil.copyfile(FIXTURES / ('necessity' + suffix), root / ('necessity' + suffix))
            with patch.dict(os.environ, {'KICAD_CONFIG_HOME': str(root / 'config')}):
                cli = KiCadCli(os.environ['VELATRACE_TEST_KICAD_CLI'])
                self.assertGreaterEqual(cli.check_startup()[0], 9)
                report = cli.drc(board)
                self.assertGreater(report.unconnected, 0)
                self.assertGreater(report.violations, 0)
                self.assertEqual(report.schematic_parity, 0)
                # KiCad 10's actual JSON identifies disabled checks; never accept it.
                if cli.version[0] >= 10:
                    project = board.with_suffix('.kicad_pro')
                    data = json.loads(project.read_text(encoding='utf-8'))
                    data['board']['design_settings']['rule_severities']['clearance'] = 'ignore'
                    project.write_text(json.dumps(data), encoding='utf-8')
                    with self.assertRaisesRegex(ValidationError, 'disabled DRC checks'):
                        cli.drc(board)
                # An unusable saved schematic must not silently become zero parity errors.
                board.with_suffix('.kicad_sch').write_text('(not-a-valid-schematic)', encoding='utf-8')
                with self.assertRaisesRegex(ValidationError, 'KiCad CLI failed'):
                    cli.drc(board)
        for suffix, content in original.items():
            self.assertEqual((FIXTURES / ('necessity' + suffix)).read_bytes(), content)


DUMP = '''import json, sys, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
mm = lambda p: [p.x / 1e6, p.y / 1e6]
print(json.dumps([[[f.GetReference(), mm(f.GetPosition()),
                    [[p.GetNumber(), p.GetNetname(), mm(p.GetPosition())] for p in f.Pads()]]
                   for f in b.GetFootprints()],
                  [b.GetLayerName(layer) for layer in b.GetEnabledLayers().CuStack()]]))'''


@unittest.skipUnless(os.getenv('VELATRACE_TEST_KICAD_PYTHON') and os.getenv('VELATRACE_TEST_KICAD_CLI'),
                     'Set VELATRACE_TEST_KICAD_PYTHON and VELATRACE_TEST_KICAD_CLI for the real one-click export')
class NativeOneClickExportTests(unittest.TestCase):
    def test_bundled_python_exports_a_dsn_that_validates_and_routes(self):
        import subprocess
        from velatrace.candidate import project_context, trusted_via_catalog
        from velatrace.dsn import export_live
        from velatrace.models import Component, DesignSnapshot, Pin
        from velatrace.preflight import preflight
        from velatrace.sexpr import parse
        original = (FIXTURES / 'necessity.kicad_pcb').read_bytes()
        with tempfile.TemporaryDirectory(prefix='velatrace-native-export-') as directory:
            root = Path(directory)
            project = root / 'project'
            project.mkdir()
            for suffix in ('.kicad_pcb', '.kicad_pro'):
                shutil.copyfile(FIXTURES / ('necessity' + suffix), project / ('necessity' + suffix))
            board = project / 'necessity.kicad_pcb'
            python = os.environ['VELATRACE_TEST_KICAD_PYTHON']
            with patch.dict(os.environ, {'KICAD_CONFIG_HOME': str(root / 'config')}):
                # pcbnew stands in for the IPC reader: an independent read of the same board.
                dumped = subprocess.run([python, '-I', '-c', DUMP, str(board)], capture_output=True,
                                        timeout=120, check=True, env={**os.environ})
                parts, layers = json.loads(dumped.stdout)
                snapshot = DesignSnapshot(tuple(
                    Component(ref, '', '', tuple(Pin(n, net, position_mm=tuple(p)) for n, net, p in pads),
                              position_mm=tuple(at)) for ref, at, pads in parts), 'ipc-pcb', board, tuple(layers))
                text = board.read_text(encoding='utf-8')
                problems, _ = preflight(parse(text, kicad=True), snapshot, project_context(board)[0])
                self.assertEqual(problems, [])
                cli = KiCadCli(os.environ['VELATRACE_TEST_KICAD_CLI'])
                cli.python = Path(python)
                out = root / 'export'
                out.mkdir()
                dsn = export_live(cli, text, snapshot, out)  # Same validation as a manual DSN.
            self.assertEqual(dsn.path.parent, out.resolve())
            self.assertEqual(set(dsn.placements), {ref for ref, _, _ in parts})
            if os.getenv('VELATRACE_TEST_JAR') and os.getenv('VELATRACE_TEST_JAVA'):
                from velatrace.freerouting import Freerouting
                from velatrace.ses import parse_ses
                router = Freerouting(Path(os.environ['VELATRACE_TEST_JAR']), os.environ['VELATRACE_TEST_JAVA'],
                                     work_directory=root / 'router')
                plan = parse_ses(router.route(dsn, ()), expected_design=dsn.base_design, nets=set(dsn.nets),
                                 layers=set(dsn.layers), via_catalog=trusted_via_catalog(dsn),
                                 expected_placements=dsn.placements,
                                 expected_placement_resolution_mm=dsn.placement_resolution_mm)
                self.assertGreater(plan.trace_count, 0)
        self.assertEqual((FIXTURES / 'necessity.kicad_pcb').read_bytes(), original)

    def test_footprint_copper_graphics_become_keepouts(self):
        # VT-01: a bare copper bar inside a footprint that has pads (synthetic). A graphic
        # touching one of the footprint's own pads (net tie, antenna) is left to DRC.
        import subprocess
        from velatrace.kicad_cli import EXPORT_SCRIPT
        line = ('\n    (fp_line (start {} {}) (end {} {}) (stroke (width 0.3) (type solid)) (layer "{}") '
                '(uuid "aaaaaaaa-0000-4000-8000-00000000000{}"))')
        text = (FIXTURES / 'necessity.kicad_pcb').read_text(encoding='utf-8')
        at = text.index('(attr smd)', text.index('"C2"')) + len('(attr smd)')
        python = os.environ['VELATRACE_TEST_KICAD_PYTHON']
        for name, extra, expected in (('plain', '', 0),
                                      ('bar', line.format(-2, -4.3, -2, 7.3, 'F.Cu', 0)
                                       + line.format(-2, -4.3, -2, 7.3, 'B.Cu', 1), 2),
                                      ('tie', line.format(-1, 0, 1, 0, 'F.Cu', 2), 0)):
            with self.subTest(name), tempfile.TemporaryDirectory(prefix='velatrace-native-keepout-') as directory:
                board = Path(directory) / 'necessity.kicad_pcb'
                board.write_text(text[:at] + extra + text[at:], encoding='utf-8')
                with patch.dict(os.environ, {'KICAD_CONFIG_HOME': str(Path(directory) / 'config')}):
                    done = subprocess.run([python, '-I', '-c', EXPORT_SCRIPT, str(board), str(board.with_suffix('.dsn'))],
                                          capture_output=True, timeout=120, env={**os.environ})
                self.assertEqual(done.returncode, 0, done.stderr[-300:])
                self.assertEqual(board.with_suffix('.dsn').read_text(encoding='utf-8').count('(keepout'), expected)

    def test_project_rule_files_cannot_hide_a_short_from_candidate_drc(self):
        # VT-01 with real kicad-cli: a GND track laid across a VCC pad (synthetic). The
        # project's rule file silences it; the pass without the rule file must not.
        from types import SimpleNamespace
        from velatrace.candidate import (CopperItem, SafeCandidateValidator, candidate_text, hidden_issues,
                                         project_context, route_issues)
        short = (CopperItem('99999999-0000-4000-8000-000000000001', 'segment', 'GND', 'F.Cu', (11.0, 10.0),
                            (16.0, 10.0), 0.25),)
        cli = KiCadCli(os.environ['VELATRACE_TEST_KICAD_CLI'])
        tented = (FIXTURES / 'necessity.kicad_pcb').read_text(encoding='utf-8').replace(' "F.Paste" "F.Mask")', ' "F.Paste")')
        for name, rules, blinded in (
                ('ignore', '(version 1)\n(rule "house" (severity ignore) (constraint clearance (min 0.2mm)))\n', False),
                ('split', '(version 1)\n(rule "house" (severity\n# x\nignore) (constraint clearance (min 0.2mm)))\n', True),
                ('zero', '(version 1)\n(rule "house" (constraint clearance (min 0mm)))\n', True)):
            with self.subTest(name), tempfile.TemporaryDirectory(prefix='velatrace-native-rules-') as directory, \
                    patch.dict(os.environ, {'KICAD_CONFIG_HOME': str(Path(directory) / 'config')}):
                board = Path(directory) / 'necessity.kicad_pcb'
                board.write_text(tented, encoding='utf-8')
                shutil.copyfile(FIXTURES / 'necessity.kicad_pro', board.with_suffix('.kicad_pro'))
                board.with_suffix('.kicad_dru').write_text(rules, encoding='utf-8')
                validator = SafeCandidateValidator(SimpleNamespace(directory=Path(directory)), cli)
                files = validator._context_files(project_context(board)[1])
                sets = validator._rule_sets((), files)
                self.assertEqual(sets, (0, validator.STOCK))
                def run(rule_set):
                    used = validator._with_rule(files, board.name, rule_set)
                    return (validator._drc(board.name, tented, used, True),
                            validator._drc(board.name, candidate_text(tented, short), used, False))
                # Under the project's own rules the short may vanish; without the rule file it cannot.
                self.assertEqual(route_issues(*run(0))[0] == 0, blinded)
                self.assertIn('shorting_items', {kind for kind, _, _ in hidden_issues(*run(validator.STOCK))})


if __name__ == '__main__':
    unittest.main()
