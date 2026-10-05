import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from velatrace.errors import CapabilityError, ValidationError
from velatrace.kicad_cli import KiCadCli


BOARD = '''(kicad_pcb (version 20250101) (generator "pcbnew")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "GND")
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    (polygon (pts (xy 1 1) (xy 5 1) (xy 5 5) (xy 1 5))))
  (zone (net 0) (net_name "") (layer "F.Cu")
    (keepout (tracks not_allowed) (vias not_allowed))
    (uuid "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    (polygon (pts (xy 8 8) (xy 9 8) (xy 9 9) (xy 8 9)))))'''

REPORT = {"violations": [], "unconnected_items": [], "schematic_parity": []}


class ZoneRefillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.board = self.root / "board.kicad_pcb"
        self.board.write_text(BOARD, encoding="utf-8")
        self.project = self.root / "board.kicad_pro"
        self.project.write_text(json.dumps({"board": {"design_settings": {
            "drc_exclusions": [], "rule_severities": {}}}}), encoding="utf-8")
        self.rules = self.root / "board.kicad_dru"
        self.rules.write_text("(version 1)\n", encoding="utf-8")

    def cli(self, version):
        cli = object.__new__(KiCadCli)
        cli.version = version
        cli._exportable = set()
        cli._export_lock = threading.Lock()
        cli._config_homes = {}
        cli._config_lock = threading.Lock()
        return cli

    @staticmethod
    def write_report(arguments):
        output = Path(arguments[arguments.index("--output") + 1])
        output.write_text(json.dumps(REPORT), encoding="utf-8")

    def test_zone_drc_refills_only_on_kicad10_and_ignores_keepouts(self):
        cli = self.cli((10, 0, 4))
        calls = []

        def run(arguments, cwd, allowed_exit_codes, config_home):
            calls.append(arguments)
            self.write_report(arguments)
            return 0

        with patch.object(cli, "_run", side_effect=run), \
                patch.object(cli, "_drc_config_home", return_value=self.root):
            cli.drc(self.board)
        self.assertIn("--refill-zones", calls[0])
        self.assertNotIn("--save-board", calls[0])

        keepout_only = self.root / "keepout.kicad_pcb"
        keepout_only.write_text(BOARD.replace(
            '(net 1) (net_name "GND") (layer "F.Cu")\n    (uuid "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")\n    (polygon (pts (xy 1 1) (xy 5 1) (xy 5 5) (xy 1 5)))',
            '(keepout (tracks not_allowed)) (net 1) (net_name "GND") (layer "F.Cu")\n    (uuid "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")\n    (polygon (pts (xy 1 1) (xy 5 1) (xy 5 5) (xy 1 5)))'), encoding="utf-8")
        with patch.object(cli, "_run", side_effect=run), \
                patch.object(cli, "_drc_config_home", return_value=self.root):
            cli.drc(keepout_only)
        self.assertNotIn("--refill-zones", calls[-1])

    def test_kicad9_refuses_zone_bearing_drc_before_running_cli(self):
        cli = self.cli((9, 0, 6))
        with patch.object(cli, "_run", side_effect=AssertionError("CLI must not run")), \
                patch.object(cli, "_drc_config_home", side_effect=AssertionError("no setup needed")):
            with self.assertRaisesRegex(CapabilityError, "cannot refill zones"):
                cli.drc(self.board)

    def test_refill_returns_provenance_and_never_saves_the_original(self):
        cli = self.cli((10, 0, 4))
        original = self.board.read_bytes()
        project = self.project.read_bytes()
        rules = self.rules.read_bytes()
        staged_text = BOARD.replace('(version 20250101)', '(version 20250102)')
        observed = {}

        def run(arguments, cwd, allowed_exit_codes, config_home):
            staged = Path(arguments[-1])
            backup = staged.with_name(staged.name + ".before-refill.bak")
            observed["flags"] = set(arguments)
            observed["cwd"] = Path(cwd)
            observed["backup"] = backup.read_bytes()
            observed["staged_context"] = (staged.with_suffix(".kicad_pro").read_bytes(),
                                           staged.with_suffix(".kicad_dru").read_bytes())
            staged.write_text(staged_text, encoding="utf-8")
            self.write_report(arguments)
            return 0

        with patch.object(cli, "_run", side_effect=run), \
                patch.object(cli, "_drc_config_home", return_value=self.root):
            result = cli.refill_for_analysis(self.board)

        self.assertIn("--refill-zones", observed["flags"])
        self.assertIn("--save-board", observed["flags"])
        self.assertEqual(observed["backup"], original)
        self.assertEqual(observed["staged_context"], (project, rules))
        self.assertNotEqual(observed["cwd"], self.root)
        self.assertEqual(result.text, staged_text)
        self.assertEqual(result.source_digest, hashlib.sha256(original).hexdigest())
        self.assertEqual(result.tool_version, (10, 0, 4))
        self.assertEqual((result.drc.violations, result.drc.unconnected, result.drc.schematic_parity), (0, 0, 0))
        self.assertEqual(self.board.read_bytes(), original)
        self.assertEqual(self.project.read_bytes(), project)
        self.assertEqual(self.rules.read_bytes(), rules)

    def test_refill_refuses_kicad9_and_inconsistent_drc_status(self):
        old = self.cli((9, 0, 6))
        with patch.object(old, "_run", side_effect=AssertionError("CLI must not run")):
            with self.assertRaisesRegex(CapabilityError, "10 or newer"):
                old.refill_for_analysis(self.board)

        cli = self.cli((10, 0, 4))

        def inconsistent(arguments, cwd, allowed_exit_codes, config_home):
            self.write_report(arguments)
            return 5

        with patch.object(cli, "_run", side_effect=inconsistent), \
                patch.object(cli, "_drc_config_home", return_value=self.root):
            with self.assertRaisesRegex(ValidationError, "status disagrees"):
                cli.refill_for_analysis(self.board)
        self.assertEqual(self.board.read_text(encoding="utf-8"), BOARD)

    def test_refill_rejects_changed_project_context(self):
        cli = self.cli((10, 0, 4))

        def mutating_run(arguments, cwd, allowed_exit_codes, config_home):
            staged = Path(arguments[-1])
            staged.write_text(BOARD, encoding="utf-8")
            self.project.write_text("changed", encoding="utf-8")
            self.write_report(arguments)
            return 0

        with patch.object(cli, "_run", side_effect=mutating_run), \
                patch.object(cli, "_drc_config_home", return_value=self.root):
            with self.assertRaisesRegex(ValidationError, "changed during zone refill"):
                cli.refill_for_analysis(self.board)

    def test_refill_rejects_changed_source_and_non_board_root(self):
        cli = self.cli((10, 0, 4))

        def mutating_run(arguments, cwd, allowed_exit_codes, config_home):
            self.board.write_text(BOARD.replace('20250101', '20250102'), encoding='utf-8')
            self.write_report(arguments)
            return 0

        with patch.object(cli, '_run', side_effect=mutating_run), \
                patch.object(cli, '_drc_config_home', return_value=self.root):
            with self.assertRaisesRegex(ValidationError, 'Candidate changed'):
                cli.refill_for_analysis(self.board)
        self.board.write_text('(not_a_board)', encoding='utf-8')
        with patch.object(cli, '_run', side_effect=AssertionError('CLI must not run')):
            with self.assertRaisesRegex(ValidationError, 'saved KiCad PCB'):
                cli.refill_for_analysis(self.board)


if __name__ == "__main__":
    unittest.main()
