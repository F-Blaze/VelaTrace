import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from velatrace.errors import CapabilityError, ValidationError
from velatrace.kicad_cli import (KiCadCli, footprint_libraries, local_tool_environment, parse_drc_report,
                                 trimmed_library_table)


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

    def test_incomplete_issue_identity_cannot_exempt_a_candidate_warning(self):
        from velatrace.candidate import route_issues
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drc.json"
            warning = {"type": "clearance", "severity": "warning", "items": [{"uuid": "pad-1"}]}
            incomplete = [warning | {"items": []}, warning | {"items": [{}]},
                          warning | {"items": [{"uuid": None}]}, warning | {"type": None},
                          warning | {"severity": None}]
            for issue in incomplete:
                with self.subTest(issue=issue):
                    path.write_text(json.dumps({"violations": [warning, issue],
                                               "unconnected_items": [], "schematic_parity": []}))
                    result = parse_drc_report(path)
                    self.assertEqual(result.violations, 2)
                    self.assertEqual(result.issues, ())
                    self.assertEqual(route_issues(result, result), (2, 0))
            path.write_text(json.dumps({"violations": [warning], "unconnected_items": [],
                                       "schematic_parity": []}))
            result = parse_drc_report(path)
            self.assertEqual(route_issues(result, result), (0, 1))

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

    def test_routing_requires_exact_editor_cli_version(self):
        cli = object.__new__(KiCadCli)
        with patch.object(cli, "check_startup", return_value=(10, 0, 6)):
            cli.require_editor_version((10, 0, 6))
            for version in ((9, 0, 6), (10, 0, 5), (11, 0, 0)):
                with self.subTest(version=version), self.assertRaisesRegex(CapabilityError, "does not match"):
                    cli.require_editor_version(version)

    def test_schematic_parity_is_explicit_and_export_failure_refuses_drc(self):
        cli = object.__new__(KiCadCli)
        cli._exportable, cli._export_lock = set(), threading.Lock()
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / "board.kicad_pcb"
            board.write_text("fixture")
            calls = []
            def run(args, cwd, allowed_exit_codes, config_home):
                calls.append(args)
                report = Path(args[args.index("--output") + 1])
                report.write_text(json.dumps({"violations": [], "unconnected_items": [],
                                              "schematic_parity": []}))
                return 0
            with patch.object(cli, "_run", side_effect=run), patch.object(cli, "schematic_snapshot") as export,                     patch.object(cli, "_drc_config_home", return_value=Path(directory)):
                cli.drc(board)
                self.assertNotIn("--schematic-parity", calls[-1])
                export.assert_not_called()
                board.with_suffix(".kicad_sch").write_text("fixture schematic")
                cli.drc(board)
                self.assertIn("--schematic-parity", calls[-1])
                export.assert_called_once_with(board.with_suffix(".kicad_sch"), saved_confirmed=True)
                # Byte-identical context was already proven exportable; changed bytes are proven again.
                cli.drc(board)
                export.assert_called_once()
                board.with_suffix(".kicad_sch").write_text("edited schematic")
                export.side_effect = ValidationError("unreadable schematic")
                before = len(calls)
                with self.assertRaisesRegex(ValidationError, "unreadable schematic"):
                    cli.drc(board)
                self.assertEqual(len(calls), before)

    def test_drc_library_table_keeps_only_the_boards_libraries(self):
        board = ('(kicad_pcb (footprint "Resistor_SMD:R_0603" (at 0 0)) (footprint "Odd\\"Lib:X")'
                 ' (footprint "NoLibrary") (footprint "Connector:J"))')
        self.assertEqual(footprint_libraries(board), {"Resistor_SMD", 'Odd"Lib', "Connector"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock-table"
            stock.write_text('(fp_lib_table (version 7)\n'
                             ' (lib (name "Resistor_SMD") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/Resistor_SMD.pretty") (options "") (descr ""))\n'
                             ' (lib (name "Battery") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/Battery.pretty") (options "") (descr "")))\n')
            user = root / "fp-lib-table"
            user.write_text(f'(fp_lib_table (version 7)\n (lib (name "KiCad") (type "Table") (uri "{stock.as_posix()}") (options "") (descr "Stock"))\n'
                            ' (lib (name "Connector") (type "KiCad") (uri "C:/libs/Connector.pretty") (options "") (descr "") (disabled))\n'
                            ' (lib (name "Unused") (type "KiCad") (uri "C:/libs/Unused.pretty") (options "") (descr "")))\n')
            out = root / "out"
            out.mkdir()
            text = trimmed_library_table(user, frozenset({"Resistor_SMD", "Connector"}), out)
            self.assertNotIn("Unused", text)
            self.assertIn('(lib (name "Connector") (type "KiCad") (uri "C:/libs/Connector.pretty") (options "") (descr "") (disabled))', text)
            nested = [path for path in out.iterdir()]
            self.assertEqual(len(nested), 1)
            self.assertIn(f'(uri "{nested[0].as_posix()}")', text)
            self.assertIn('(uri "${KICAD10_FOOTPRINT_DIR}/Resistor_SMD.pretty")', nested[0].read_text())
            self.assertNotIn("Battery", nested[0].read_text())
            # A nested table behind a path variable cannot be located exactly: no trimming.
            user.write_text('(fp_lib_table (version 7) (lib (name "KiCad") (type "Table") (uri "${KICAD_USER}/t") (options "") (descr "")))')
            with self.assertRaises(ValueError):
                trimmed_library_table(user, frozenset(), out)

    def test_saved_schematic_confirmation_precedes_subprocess(self):
        cli = object.__new__(KiCadCli)
        with self.assertRaisesRegex(ValidationError, "Save the schematic"):
            cli.schematic_snapshot(Path("example.kicad_sch"), saved_confirmed=False)


if __name__ == "__main__":
    unittest.main()
