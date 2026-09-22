import json
from pathlib import Path
import tempfile
import unittest

from velatrace.theme import saved_kicad_theme


class ThemeTests(unittest.TestCase):
    def test_explicit_version_theme_and_automatic_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "10.0" / "kicad_common.json"
            path.parent.mkdir()
            for value, expected in ((0, "Light"), (1, "Dark"), (2, None), (True, None), (8, None)):
                path.write_text(json.dumps({"appearance": {"app_theme": value}}))
                self.assertEqual(saved_kicad_theme(10, config_root=folder, platform="win32"), expected)
                self.assertIsNone(saved_kicad_theme(9, config_root=folder, platform="win32"))
                self.assertIsNone(saved_kicad_theme(10, config_root=folder, platform="linux"))
            path.write_text("invalid")
            self.assertIsNone(saved_kicad_theme(10, config_root=folder, platform="win32"))
