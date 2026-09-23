import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from velatrace.errors import CapabilityError, ValidationError
from velatrace.kicad_cli import KiCadCli, local_tool_environment, parse_drc_report


class KiCadCliTests(unittest.TestCase):
    def test_required_drc_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drc.json"
            path.write_text(json.dumps({"violations": [{"type": "clearance"}],
                                       "unconnected_items": [{}, {}], "schematic_parity": []}))
            report = parse_drc_report(path)
            self.assertEqual((report.violations, report.unconnected, report.schematic_parity), (1, 2, 0))
            path.write_text('{"violations":[]}')
            with self.assertRaises(ValidationError):
                parse_drc_report(path)

    def test_missing_cli_is_actionable(self):
        with patch("velatrace.kicad_cli.shutil.which", return_value=None):
            with self.assertRaisesRegex(CapabilityError, "Install KiCad 9"):
                KiCadCli()

    def test_incomplete_drc_coverage_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drc.json"
            clean = {"violations": [], "unconnected_items": [], "schematic_parity": []}
            for extra in ({"ignored_checks": [{"key": "clearance", "description": "Clearance"}]},
                          {"ignored_checks": None},
                          {"included_severities": ["error"]}):
                path.write_text(json.dumps(clean | extra))
                with self.assertRaises(ValidationError):
                    parse_drc_report(path)
            path.write_text(json.dumps(clean | {"ignored_checks": [], "included_severities":
                                                ["error", "warning", "exclusion"]}))
            self.assertEqual(parse_drc_report(path).violations, 0)

    def test_child_environment_excludes_secrets(self):
        with patch.dict(os.environ, {"VELATRACE_API_KEY": "fixture", "KICAD_API_TOKEN": "fixture",
                                     "PATH": "local-path", "KICAD9_SYMBOL_DIR": "symbols",
                                     "KICAD_CONFIG_HOME": "isolated-config"}, clear=True):
            self.assertEqual(local_tool_environment(), {"PATH": "local-path", "KICAD9_SYMBOL_DIR": "symbols",
                                                       "KICAD_CONFIG_HOME": "isolated-config"})

    def test_saved_schematic_confirmation_precedes_subprocess(self):
        cli = object.__new__(KiCadCli)
        with self.assertRaisesRegex(ValidationError, "Save the schematic"):
            cli.schematic_snapshot(Path("example.kicad_sch"), saved_confirmed=False)


if __name__ == "__main__":
    unittest.main()
