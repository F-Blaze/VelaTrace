"""One-click DSN export: KiCad's bundled Python in a separate, isolated process (mocked)."""
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_dsn_export import DSN, SNAPSHOT
from velatrace.dsn import export_live
from velatrace.errors import ExportUnavailable, RoutingCancelled, ValidationError
from velatrace.kicad_cli import EXPORT_SCRIPT, KiCadCli
from velatrace.models import DesignSnapshot


class FakeProcess:
    """Popen stand-in: `behaviour` writes the DSN, fails, or hangs until killed."""
    calls = []

    def __init__(self, args, *, cwd, env, behaviour, **_):
        FakeProcess.calls.append((args, cwd, env))
        self.args, self.behaviour, self.returncode, self.killed = args, behaviour, None, False

    def communicate(self, timeout=None):
        if self.behaviour == "hang" and not self.killed:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if self.behaviour == "ok":
            Path(self.args[-1]).write_text(DSN, encoding="utf-8")
            self.returncode = 0
            return None, b""
        if self.behaviour == "keepout":
            self.returncode = 4
            return None, b"Traceback noise\nkeepout: AttributeError: no Intersects"
        self.returncode = 1 if self.behaviour == "no-pcbnew" else -9
        return None, b"ModuleNotFoundError: No module named 'pcbnew'"

    def kill(self):
        self.killed = True


class AutoExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project_dir = self.root / "project"
        self.project_dir.mkdir()
        self.board = self.project_dir / "board.kicad_pcb"
        self.board.write_text("(kicad_pcb saved)", encoding="utf-8")
        self.board.with_suffix(".kicad_pro").write_text('{"net_settings": {}}', encoding="utf-8")
        self.saved = {path: path.read_bytes() for path in self.project_dir.iterdir()}
        self.out = self.root / "export"
        self.out.mkdir()
        self.python = self.root / "python.exe"
        self.python.write_bytes(b"")
        self.user_config = self.root / "user-config"
        (self.user_config / "10.0").mkdir(parents=True)
        (self.user_config / "10.0" / "kicad_common.json").write_text("{}", encoding="utf-8")
        cli = object.__new__(KiCadCli)
        cli.executable, cli.timeout, cli.version = self.root / "kicad-cli.exe", 5, (10, 0, 6)
        cli._config_lock, cli.python, cli._export_home = threading.Lock(), self.python, None
        self.cli = cli
        self.snapshot = DesignSnapshot(SNAPSHOT.components, SNAPSHOT.source, self.board, SNAPSHOT.copper_layers)
        FakeProcess.calls = []

    def export(self, behaviour, cancel=None):
        def popen(args, **kwargs):
            return FakeProcess(args, behaviour=behaviour, **kwargs)
        with patch("velatrace.kicad_cli.subprocess.Popen", side_effect=popen), \
                patch.dict("os.environ", {"KICAD_CONFIG_HOME": str(self.user_config)}):
            return export_live(self.cli, "(kicad_pcb live)", self.snapshot, self.out, cancel)

    def test_live_text_is_exported_isolated_and_validated_like_a_manual_dsn(self):
        dsn = self.export("ok")
        (args, cwd, env), = FakeProcess.calls
        # Paths are arguments, never code; -I ignores PYTHONPATH and user site-packages.
        self.assertEqual(args[:4], [str(self.python), "-I", "-c", EXPORT_SCRIPT])
        self.assertEqual(args[4:], [str(self.out / "board.kicad_pcb"), str(self.out / "board.dsn")])
        self.assertEqual(Path(cwd), self.out)
        # The exported board is the live text; the saved project rides along for net classes.
        self.assertEqual((self.out / "board.kicad_pcb").read_text(encoding="utf-8"), "(kicad_pcb live)")
        self.assertTrue((self.out / "board.kicad_pro").is_file())
        # A private settings folder, never the user's own.
        home = Path(env["KICAD_CONFIG_HOME"])
        self.assertNotEqual(home, self.user_config)
        self.assertTrue((home / "10.0" / "kicad_common.json").is_file())
        self.assertEqual(dsn.path, (self.out / "board.dsn").resolve())
        self.assertEqual(dsn.ticket.board_path, self.board.resolve())
        self.assertEqual(sorted(dsn.placements), ["R1", "U1"])
        # Nothing in the project folder was written.
        self.assertEqual({path: path.read_bytes() for path in self.project_dir.iterdir()}, self.saved)

    def test_generated_dsn_must_still_match_the_live_board(self):
        moved = DesignSnapshot(tuple(item if item.reference != "R1" else
                                     type(item)(**{**item.__dict__, "position_mm": (31, 20)})
                                     for item in SNAPSHOT.components), "ipc-pcb", self.board, SNAPSHOT.copper_layers)
        self.snapshot = moved
        with self.assertRaisesRegex(ValidationError, "placement differs"):
            self.export("ok")

    def test_missing_python_or_pcbnew_offers_the_manual_path(self):
        self.cli.python = None
        with patch.object(KiCadCli, "kicad_python", return_value=None), \
                self.assertRaisesRegex(ExportUnavailable, "bundled Python was not found"):
            self.export("ok")
        self.cli.python = self.python
        with self.assertRaisesRegex(ExportUnavailable, "no pcbnew module"):
            self.export("no-pcbnew")

    def test_failed_keepouts_stop_the_export_with_a_reason(self):
        # VT-01: a DSN without the copper-graphic keepouts must never pass as complete.
        self.assertNotIn("except Exception:\n    pass", EXPORT_SCRIPT)
        with self.assertRaisesRegex(ExportUnavailable, r"keepouts .* \(AttributeError: no Intersects\)"):
            self.export("keepout")
        self.assertFalse((self.out / "board.dsn").exists())

    def test_cancel_kills_the_export_process(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(RoutingCancelled):
            self.export("hang", cancel)
        self.assertFalse((self.out / "board.dsn").exists())

    def test_hung_export_times_out_with_the_manual_path(self):
        self.cli.timeout = .3
        with self.assertRaisesRegex(ExportUnavailable, "exceeded"):
            self.export("hang")

    def test_unsaved_new_board_asks_for_one_save(self):
        self.snapshot = DesignSnapshot(SNAPSHOT.components, SNAPSHOT.source, None, SNAPSHOT.copper_layers)
        with self.assertRaisesRegex(ValidationError, "Save the board once"):
            self.export("ok")
        self.assertEqual(FakeProcess.calls, [])

    def test_python_is_found_next_to_kicad_cli(self):
        self.cli.python = None
        bundled = self.root / "python.exe"
        self.cli.executable = self.root / "kicad-cli.exe"
        self.assertEqual(self.cli.kicad_python(), bundled)


if __name__ == "__main__":
    unittest.main()
