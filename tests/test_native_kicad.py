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


if __name__ == '__main__':
    unittest.main()
