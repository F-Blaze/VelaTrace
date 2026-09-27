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


class GlassThemeTests(unittest.TestCase):
    def test_frosted_theme_is_complete_and_popups_stay_solid(self):
        from velatrace.ui import glass_stylesheet, theme_tokens
        for dark in (True, False):
            with self.subTest(dark=dark):
                tokens = theme_tokens(dark)
                sheet = glass_stylesheet(tokens)
                self.assertIn("qlineargradient", tokens["frost"])  # painted frosted backdrop
                self.assertIn(f"QWidget#glassRoot {{background:{tokens['frost']}}}", sheet)
                # Dropdown lists and tooltips must never be see-through.
                self.assertIn(f"QToolTip {{background:{tokens['solid']}", sheet)
                self.assertNotIn("{", tokens["solid"])
                self.assertIn("QPushButton#primary:disabled", sheet)
