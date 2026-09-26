"""DSN handshake against placement/pin syntax that real KiCad and Freerouting emit."""
import os
from pathlib import Path
import tempfile
import time
import unittest

from velatrace.constraints import Constraint, Scope
from velatrace.dsn import ExportTicket, accept_export
from velatrace.errors import ValidationError
from velatrace.freerouting import constrained_dsn
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.ses import parse_ses
from velatrace.sexpr import parse

# KiCad's specctra_export names the pcb by its full output path (backslashes on
# Windows, never escaped), writes (PN <value>) and renames repeated pads "2", "2@1".
DSN = r'''(pcb "C:\Users\Me\My Boards\board.dsn"
 (parser (string_quote ") (space_in_quoted_tokens on) (host_cad KiCad) (host_version 9.0))
 (resolution um 10)
 (unit um)
 (structure (layer F.Cu (type signal)) (layer B.Cu (type signal)))
 (placement
  (component SOT223 (place U1 10000 -20000 front 0 (PN AMS1117)))
  (component R0805 (place R1 30000 -20000 front 90 (PN 10k))))
 (library)
 (network
  (net VIN (pins U1-3 R1-1))
  (net GND (pins U1-1))
  (net VOUT (pins U1-2 U1-2@1 R1-2)))
 (wiring))
'''

SNAPSHOT = DesignSnapshot((
    Component("U1", "AMS1117", "SOT223", (Pin("1", "GND"), Pin("2", "VOUT"), Pin("3", "VIN"),
                                          Pin("2", "VOUT")), position_mm=(10, 20)),
    Component("R1", "10k", "R0805", (Pin("1", "VIN"), Pin("2", "VOUT")), position_mm=(30, 20)),
), "ipc-pcb", copper_layers=("F.Cu", "B.Cu"))


class AcceptExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.board = root / "board.kicad_pcb"
        self.board.write_text("(kicad_pcb)", encoding="utf-8")
        self.snapshot = DesignSnapshot(SNAPSHOT.components, SNAPSHOT.source, self.board,
                                       SNAPSHOT.copper_layers)
        self.ticket = ExportTicket.begin(self.board)
        self.dsn = root / "board.dsn"

    def tearDown(self):
        self.folder.cleanup()

    def accept(self, text):
        self.dsn.write_text(text, encoding="utf-8")
        later = time.time_ns() + 1_000_000
        os.utime(self.dsn, ns=(later, later))
        return accept_export(self.ticket, self.dsn, self.snapshot, user_confirms_saved_and_exported=True)

    def test_real_kicad_placement_and_duplicate_pads(self):
        result = self.accept(DSN)
        # Placements stay in DSN Y-up coordinates for the SES comparison.
        self.assertEqual(result.placements["U1"], (10, -20, "front", 0))
        self.assertEqual(result.placements["R1"], (30, -20, "front", 90))

    def test_placement_and_connectivity_changes_still_refused(self):
        for text in (DSN.replace("(PN 10k)", "(PN 10k) (mirror x)"),
                     DSN.replace("(PN 10k)", "(lock_type position)"),
                     DSN.replace("U1-2@1 ", ""),
                     DSN.replace("U1-2@1", "U1-2@1 U1-2@2"),
                     DSN.replace("(pins U1-1)", "(pins U1-1 R1-2)")):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                self.accept(text)

    def test_freerouting_rounded_placement(self):
        # Freerouting writes integers at the SES resolution and whole-degree rotation.
        ses = '''(session board (base_design board)
 (placement (resolution um 10)
  (component R0805 (place R1 300000 -200000 front 90)))
 (routes (resolution um 10) (network_out)))'''
        placements = {"R1": (30.00004, -20.0, "front", 90.0)}
        parse_ses(ses, expected_design="board.dsn", nets=set(), layers={"F.Cu"},
                  expected_placements=placements)
        for text in (ses.replace("300000", "300010"), ses.replace("front 90", "front 91"),
                     ses.replace("90)))", "90 (lock_type position))))"),
                     ses.replace("(place R1", "(place (R1)")):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_ses(text, expected_design="board.dsn", nets=set(), layers={"F.Cu"},
                          expected_placements=placements)


class QuotingTests(unittest.TestCase):
    def test_specctra_backslash_is_literal_and_kicad_escapes_decode(self):
        self.assertEqual(parse(r'(pcb "C:\new\board.dsn")')[1], r"C:\new\board.dsn")
        self.assertEqual(parse(r'(gr_text "a\nb \"q\" \\")', kicad=True)[1], 'a\nb "q" \\')
        dsn = r'(pcb "C:\x\b.dsn" (unit um) (structure (rule (width 100))))'
        out = constrained_dsn(dsn, (Constraint("w", Scope.SESSION, "trace-width", "all nets", .2),))
        self.assertIn(r'"C:\x\b.dsn"', out)  # Freerouting would keep "\\" as two characters.
        self.assertIn("(width 200)", out)


if __name__ == "__main__":
    unittest.main()
