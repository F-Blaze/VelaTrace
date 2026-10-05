"""External programs are never taken from the working directory or the project (VT-02).

The planted files are empty stand-ins; nothing here is executed."""
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from velatrace.errors import CapabilityError
from velatrace.freerouting import Freerouting
from velatrace.kicad_cli import KiCadCli, find_tool

EXE = ".exe" if os.name == "nt" else ""


def plant(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"")
    path.chmod(0o755)
    return path.resolve()


class ToolLookupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / "downloaded-project"
        self.project.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.project)  # VelaTrace started from inside an untrusted project.

    def path(self, *folders):
        return patch.dict(os.environ, {"PATH": os.pathsep.join(map(str, folders))})

    def test_bare_names_never_resolve_in_the_working_directory(self):
        for name in ("kicad-cli", "java", "python3"):
            plant(self.project, name + EXE)
        plant(self.project, "java.cmd")
        plant(self.project, "java.bat")
        # Even with the folder on PATH three different ways.
        with self.path(".", "", self.project):
            for name in ("kicad-cli", "java", "python3"):
                self.assertIsNone(find_tool(name))
            with self.assertRaisesRegex(CapabilityError, "kicad-cli is missing"):
                KiCadCli()
            self.assertIsNone(Freerouting(self.root / "x.jar", work_directory=self.root / "work").java)

    def test_a_missing_java_is_reported_not_searched_by_windows(self):
        plant(self.project, "java" + EXE)
        jar = plant(self.root, "x.jar")
        with self.path("."), patch("velatrace.freerouting.JAR_SHA256", hashlib.sha256(jar.read_bytes()).hexdigest()):
            with self.assertRaisesRegex(CapabilityError, "Java is missing"):
                Freerouting(jar, work_directory=self.root / "work").check_startup()

    def test_real_installations_on_path_are_found_as_absolute_paths(self):
        real = plant(self.root / "Program Files" / "bin", "java" + EXE)
        with self.path(self.project, self.root / "Program Files" / "bin"):
            self.assertEqual(find_tool("java"), real)
            self.assertEqual(Freerouting(self.root / "x.jar", work_directory=self.root / "work").java, real)
        self.assertEqual(find_tool(real), real)  # A configured absolute path is used as given.

    def test_the_project_folder_is_refused_even_when_it_is_not_the_working_directory(self):
        os.chdir(self.root)
        plant(self.project, "kicad-cli" + EXE)
        with self.path(self.project):
            self.assertIsNone(find_tool("kicad-cli", forbidden=(self.project,)))
            with self.assertRaisesRegex(CapabilityError, "kicad-cli is missing"):
                KiCadCli(forbidden=(self.project,))

    def test_relative_paths_are_refused(self):
        plant(self.project / "bin", "java" + EXE)
        for relative in (Path("bin") / ("java" + EXE), Path(".") / "bin" / ("java" + EXE)):
            self.assertIsNone(find_tool(relative))
            self.assertIsNone(find_tool(str(relative)))

    @unittest.skipUnless(os.name == "nt", "Windows batch shims")
    def test_java_must_be_a_native_executable_on_windows(self):
        for name in ("java.cmd", "java.bat"):
            with self.assertRaisesRegex(CapabilityError, "native java.exe"):
                Freerouting(self.root / "x.jar", plant(self.root / "shims", name), work_directory=self.root / "work")


class StartupDirectoryTests(unittest.TestCase):
    """VT-S1: the folder VelaTrace is started in is neither searched nor imported from."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name).resolve()
        self.addCleanup(os.chdir, os.getcwd())
        self.addCleanup(sys.path.__setitem__, slice(None), list(sys.path))
        os.chdir(self.project)

    def test_module_entry_point_leaves_the_working_directory(self):
        from velatrace import __main__ as entry
        sys.path[:0] = ["", str(self.project)]
        entry.leave_working_directory()
        self.assertEqual(Path.cwd(), Path(entry.__file__).resolve().parent)
        self.assertNotIn("", sys.path)
        self.assertNotIn(str(self.project), sys.path)

    def test_relative_arguments_still_mean_the_folder_they_were_typed_in(self):
        from velatrace import __main__ as entry
        (self.project / "n.xml").write_text("<export/>", encoding="utf-8")
        seen = []
        with patch.object(sys, "argv", ["velatrace", "--netlist", "n.xml"]), \
                patch.object(entry, "read_xml_netlist", side_effect=lambda path: seen.append(path) or {}), \
                patch.object(entry, "asdict", dict), patch("builtins.print"):
            entry.main()
        self.assertEqual(seen, [self.project / "n.xml"])

    def test_kicad_launcher_leaves_the_working_directory_before_importing(self):
        path = Path(__file__).parents[1] / "launch.py"
        spec = importlib.util.spec_from_file_location("velatrace_launch_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        seen = []
        with patch("velatrace.__main__.main", side_effect=lambda: seen.append(Path.cwd())):
            module.main()
        self.assertEqual(seen, [path.parent.resolve()])


if __name__ == "__main__":
    unittest.main()
