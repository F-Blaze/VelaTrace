"""Synthetic-only backend boundary; no native tool is needed for these tests."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from velatrace.benchmark import fixture_texts
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import CapabilityError, ValidationError
from velatrace.freerouting import ProcessResult
from velatrace.krt_benchmark import KrtBenchmarkBackend, _fixture_only, source_digest
from velatrace.router_resources.krt_runner import deny_external


class KrtBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def fixture(self, layers=4, dense=False, obstacles=False):
        for name, text in fixture_texts(layers, dense=dense, obstacles=obstacles).items():
            (self.folder/name).write_text(text, encoding='utf-8')
        path, board = self.folder/'crossed.dsn', self.folder/'crossed.kicad_pcb'
        return DsnInput(path, file_digest(path), ExportTicket.begin(board), frozenset({'N1'}),
                        frozenset({'F.Cu', 'B.Cu'}), 'crossed')

    def test_only_exact_authored_inputs_accepted(self):
        for layers in (4, 6, 8):
            for dense in (False, True):
                for obstacles in (False, True):
                    dsn = self.fixture(layers, dense, obstacles)
                    self.assertTrue(_fixture_only(dsn))
        # Even a benign user-like board edit must be rejected before any process.
        dsn.ticket.board_path.write_text(dsn.ticket.board_path.read_text().replace('BenchmarkPad', 'PrivatePart'))
        with self.assertRaisesRegex(ValidationError, 'changed'):
            _fixture_only(dsn)
        dsn = self.fixture()
        (self.folder/'crossed.kicad_dru').write_text('(version 1)')
        with self.assertRaisesRegex(ValidationError, 'authored'):
            _fixture_only(dsn)

    def test_source_pin_detects_edits_added_code_and_bytecode(self):
        (self.folder/'rust_router').mkdir()
        (self.folder/'rust_router/Cargo.toml').write_text('version="test"')
        (self.folder/'VERSION').write_text('test')
        path = self.folder/'entry.py'
        path.write_bytes(b'pass\r\n')
        original = source_digest(self.folder)
        path.write_bytes(b'pass\n')
        self.assertEqual(source_digest(self.folder), original)
        path.write_text('raise RuntimeError()')
        self.assertNotEqual(source_digest(self.folder), original)
        path.write_text('pass\n')
        (self.folder/'shadow.py').write_text('pass')
        self.assertNotEqual(source_digest(self.folder), original)
        (self.folder/'entry.pyc').write_bytes(b'fake')
        with self.assertRaises(CapabilityError):
            source_digest(self.folder)

    def test_runner_disallows_python_network_and_child_processes(self):
        for event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system', 'os.posix_spawn'):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                deny_external(event, ())
        deny_external('open', ())

    def test_process_only_sees_copies_and_changed_staged_rules_refuse(self):
        dsn = self.fixture()
        backend = KrtBenchmarkBackend(self.folder/'backend', self.folder/'python.exe', self.folder/'jobs')
        original = dsn.ticket.board_path.read_bytes()
        def process(args, directory, timeout):
            self.assertIn('-I', args)
            self.assertIn('-B', args)
            self.assertIn('--no-fix-drc-settings', args)
            self.assertNotIn(str(dsn.ticket.board_path), args)
            (directory/'crossed.kicad_pro').write_text('{}')
            return ProcessResult(0, 'changed rules')
        with patch('velatrace.krt_benchmark.source_digest', return_value='source'), \
                patch('velatrace.krt_benchmark.KRT_SOURCE_SHA256', 'source'), \
                patch('velatrace.krt_benchmark.KRT_WINDOWS_SHA256', 'binary'), \
                patch('velatrace.krt_benchmark.file_digest', side_effect=lambda path:
                      'binary' if path.suffix == '.pyd' else file_digest(path)), \
                patch('velatrace.krt_benchmark.run_bounded', side_effect=process):
            with self.assertRaisesRegex(ValidationError, 'staged source'):
                backend.route(dsn, timeout_seconds=3)
        self.assertEqual(dsn.ticket.board_path.read_bytes(), original)

    def test_change_after_fixture_capture_cannot_reach_child(self):
        dsn = self.fixture()
        backend = KrtBenchmarkBackend(self.folder/'backend', self.folder/'python.exe', self.folder/'jobs')
        def racing_fixture(*args, **kwargs):
            dsn.ticket.board_path.write_text('private board edited during capture')
            return fixture_texts(*args, **kwargs)
        with patch('velatrace.benchmark.fixture_texts', side_effect=racing_fixture), \
                patch('velatrace.krt_benchmark.source_digest', return_value='source'), \
                patch('velatrace.krt_benchmark.KRT_SOURCE_SHA256', 'source'), \
                patch('velatrace.krt_benchmark.KRT_WINDOWS_SHA256', 'binary'), \
                patch('velatrace.krt_benchmark.file_digest', side_effect=lambda path:
                      'binary' if path.suffix == '.pyd' else file_digest(path)), \
                patch('velatrace.krt_benchmark.run_bounded') as process:
            with self.assertRaises(ValidationError):
                backend.route(dsn, timeout_seconds=3)
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
