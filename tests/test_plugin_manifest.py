"""KiCad refuses to list a plugin whose identifier fails its own validation."""
import json
from pathlib import Path
import re
import unittest

MANIFEST = json.loads((Path(__file__).parents[1] / "plugin.json").read_text(encoding="utf-8"))
# common/api/api_plugin.cpp, API_PLUGIN::IsValidIdentifier
KICAD_9_TO_10_0_4 = r"[\w\d]{2,}\.[\w\d]+\.[\w\d]+"  # unanchored: no hyphens anywhere usable
KICAD_10_0_6 = r"^[a-zA-Z]{2,}(\.([a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9]|[a-zA-Z0-9])){2,}$"


class ManifestTests(unittest.TestCase):
    def test_identifier_accepted_by_every_supported_kicad(self):
        identifier = MANIFEST["identifier"]
        for pattern in (KICAD_9_TO_10_0_4, KICAD_10_0_6):
            with self.subTest(pattern=pattern):
                self.assertRegex(identifier, pattern)
        # The old identifier is what KiCad 10.0.4 rejected in practice.
        self.assertIsNone(re.search(KICAD_9_TO_10_0_4, "com.f-blaze.velatrace"))

    def test_actions_have_relative_readable_entrypoints(self):
        root = Path(__file__).parents[1]
        for action in MANIFEST["actions"]:
            entry = Path(action["entrypoint"])
            self.assertFalse(entry.is_absolute())
            self.assertTrue((root / entry).is_file())
            self.assertIn("pcb", action["scopes"])


if __name__ == "__main__":
    unittest.main()
