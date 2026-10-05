"""Strict file-output boundary tests for external route-only backends."""
import json
from pathlib import Path
import tempfile
import unittest

from velatrace.candidate import canonical
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import CapabilityError, ValidationError
from velatrace.external_copper import import_copper
from velatrace.ses import ViaSpec
from velatrace.sexpr import parse


UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"
BOARD = '''(kicad_pcb
 (version 20241229) (generator "pcbnew") (generator_version "9.0")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (net 0 "") (net 1 "N") (net 2 "OTHER")
 (footprint "R" (layer "F.Cu") (at 2 3) (property "Reference" "R1")
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "N")))
 (zone (net 2) (net_name "OTHER") (layer "B.Cu") (uuid "33333333-3333-4333-8333-333333333333")))'''
SEGMENT = f'''(segment (start 1 2) (end 4 6) (width 0.25) (layer "F.Cu") (net 1) (uuid "{UUID1}"))'''


class ExternalCopperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.board_path = self.root / "board.kicad_pcb"
        self.board_path.write_text(BOARD, encoding="utf-8")
        self.dsn_path = self.root / "board.dsn"
        self.dsn_path.write_text('(pcb "board" (unit mm) (library))', encoding="utf-8")
        self.dsn = DsnInput(self.dsn_path, file_digest(self.dsn_path), ExportTicket.begin(self.board_path),
                            frozenset({"N", "OTHER"}), frozenset({"F.Cu", "B.Cu"}), "board")

    def result(self, copper=SEGMENT):
        return BOARD[:-1] + " " + copper + ")"

    def test_imports_only_a_straight_segment_and_flips_kicad_y_once(self):
        plan = import_copper(BOARD, self.result(), self.dsn, via_catalog={})
        self.assertEqual(plan.base_design, "board")
        self.assertEqual(len(plan.tracks), 1)
        self.assertEqual(plan.tracks[0].net, "N")
        self.assertEqual(plan.tracks[0].layer, "F.Cu")
        self.assertEqual(plan.tracks[0].points_mm, ((1, -2), (4, -6)))

    def test_accepts_tracks_on_inner_copper_layers(self):
        board = BOARD.replace('(31 "B.Cu" signal)', '(4 "In1.Cu" power) (31 "B.Cu" signal)')
        dsn = DsnInput(self.dsn_path, self.dsn.digest, self.dsn.ticket, self.dsn.nets,
                       frozenset({"F.Cu", "In1.Cu", "B.Cu"}), "board")
        copper = SEGMENT.replace('"F.Cu"', '"In1.Cu"')
        plan = import_copper(board, board[:-1] + " " + copper + ")", dsn, via_catalog={})
        self.assertEqual(plan.tracks[0].layer, "In1.Cu")

    def test_kicad10_inline_net_name_is_supported(self):
        board = '''(kicad_pcb (version 20250101) (generator "pcbnew")
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (footprint "R" (layer "F.Cu") (at 0 0)
            (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N"))))'''
        dsn = DsnInput(self.dsn_path, self.dsn.digest, self.dsn.ticket, frozenset({"N"}),
                       frozenset({"F.Cu", "B.Cu"}), "board")
        copper = SEGMENT.replace('(net 1)', '(net "N")')
        plan = import_copper(board, board[:-1] + " " + copper + ")", dsn, via_catalog={})
        self.assertEqual(plan.tracks[0].net, "N")

    def test_refuses_any_non_copper_board_change(self):
        mutations = (
            ("(at 2 3)", "(at 2.1 3)"),
            ('(net 1 "N")))', '(net 2 "OTHER")))'),
            ('(net 1 "N")', '(net 1 "RENAMED")'),
            ('(net 2) (net_name "OTHER")', '(net 1) (net_name "N")'),
            ('(31 "B.Cu" signal)', '(31 "B.Cu" power)'),
            ('(31 "B.Cu" signal)', '(31 "In1.Cu" signal)'),
            ('(generator_version "9.0")', '(generator_version "10.0")'),
        )
        for before, after in mutations:
            with self.subTest(before=before):
                changed = self.result().replace(before, after)
                with self.assertRaises(ValidationError):
                    import_copper(BOARD, changed, self.dsn, via_catalog={})

    def test_refuses_arcs_blind_vias_and_other_added_board_shapes(self):
        for extra in (
            '(arc (start 1 2) (mid 2 3) (end 3 4) (width 0.2) (layer "F.Cu") (net 1) (uuid "' + UUID2 + '"))',
            '(gr_line (start 1 2) (end 3 4) (layer "F.Cu") (uuid "' + UUID2 + '"))',
        ):
            with self.subTest(extra=extra[:20]):
                with self.assertRaises(ValidationError):
                    import_copper(BOARD, self.result(SEGMENT + " " + extra), self.dsn, via_catalog={})
        via = f'''(via (at 2 3) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu")
                      (net 1) (uuid "{UUID2}"))'''
        with self.assertRaisesRegex(ValidationError, "through-vias"):
            import_copper(BOARD, self.result(SEGMENT + " " + via), self.dsn,
                          via_catalog={"trusted": ViaSpec(.6, .3, ("F.Cu", "B.Cu"))})

    def test_refuses_unknown_nets_layers_untrusted_attributes_and_invalid_geometry(self):
        cases = (
            SEGMENT.replace('(net 1)', '(net 900)'),
            SEGMENT.replace('"F.Cu"', '"In1.Cu"'),
            SEGMENT.replace('(width 0.25)', '(width 0.25) (locked yes)'),
            SEGMENT.replace('(uuid "' + UUID1 + '")', '(remove_unused_layers) (uuid "' + UUID1 + '")'),
            SEGMENT.replace('(end 4 6)', '(end 1 2)'),
            SEGMENT.replace('(width 0.25)', '(width 0)'),
            SEGMENT.replace(UUID1, "bad-uuid"),
            SEGMENT.replace(UUID1, "33333333-3333-4333-8333-333333333333"),
        )
        for copper in cases:
            with self.subTest(copper=copper):
                with self.assertRaises(ValidationError):
                    import_copper(BOARD, self.result(copper), self.dsn, via_catalog={})

    def test_refuses_duplicate_output_uuids_and_preexisting_copper(self):
        with self.assertRaisesRegex(ValidationError, "duplicate or conflicting"):
            import_copper(BOARD, self.result(SEGMENT + " " + SEGMENT), self.dsn, via_catalog={})
        with self.assertRaises(CapabilityError):
            import_copper(self.result(), self.result(), self.dsn, via_catalog={})

    def test_accepts_catalog_matched_through_via_and_converts_coordinates(self):
        spec = ViaSpec(.6, .3, ("F.Cu", "B.Cu"))
        # This trusted project/DSN fixture lets prepare_copper independently
        # re-check the same via geometry after import.
        self.board_path.with_suffix(".kicad_pro").write_text(json.dumps({"board": {"design_settings": {
            "rule_severities": {}, "via_dimensions": [{"diameter": .6, "drill": .3}]}}}), encoding="utf-8")
        self.dsn_path.write_text('''(pcb "board" (unit mm) (library
          (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 0.6))
            (shape (circle B.Cu 0.6)))))''', encoding="utf-8")
        self.dsn = DsnInput(self.dsn_path, file_digest(self.dsn_path), ExportTicket.begin(self.board_path),
                            frozenset({"N", "OTHER"}), frozenset({"F.Cu", "B.Cu"}), "board")
        via = f'''(via (at 7 8) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
                    (net 1) (uuid "{UUID2}") (capping no)
                    (covering (front no) (back no)) (plugging (front no) (back no))
                    (filling no))'''
        plan = import_copper(BOARD, self.result(SEGMENT + " " + via), self.dsn,
                             via_catalog={"Via[0-1]_600:300_um": spec})
        self.assertEqual(plan.vias[0].position_mm, (7, -8))
        self.assertIs(plan.vias[0].spec, spec)

    def test_accepts_absent_or_exact_default_no_via_fabrication_metadata(self):
        spec = ViaSpec(.6, .3, ("F.Cu", "B.Cu"))
        self.board_path.with_suffix(".kicad_pro").write_text(json.dumps({"board": {"design_settings": {
            "rule_severities": {}, "via_dimensions": [{"diameter": .6, "drill": .3}]}}}), encoding="utf-8")
        self.dsn_path.write_text('''(pcb "board" (unit mm) (library
          (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 0.6))
            (shape (circle B.Cu 0.6)))))''', encoding="utf-8")
        self.dsn = DsnInput(self.dsn_path, file_digest(self.dsn_path), ExportTicket.begin(self.board_path),
                            frozenset({"N", "OTHER"}), frozenset({"F.Cu", "B.Cu"}), "board")
        base = f'''(via (at 7 8) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
                     (net 1) (uuid "{UUID2}")'''
        metadata = (
            "(capping no)",
            "(covering (front no) (back no))",
            "(plugging (front no) (back no))",
            "(filling no)",
        )
        candidates = (base + ")", base + " " + " ".join(metadata) + ")")
        candidates += tuple(base + " " + item + ")" for item in metadata)
        for via in candidates:
            with self.subTest(via=via):
                plan = import_copper(BOARD, self.result(via), self.dsn, via_catalog={"trusted": spec})
                self.assertEqual(len(plan.vias), 1)

    def test_rejects_nondefault_malformed_duplicate_and_nested_via_metadata(self):
        spec = ViaSpec(.6, .3, ("F.Cu", "B.Cu"))
        self.board_path.with_suffix(".kicad_pro").write_text(json.dumps({"board": {"design_settings": {
            "rule_severities": {}, "via_dimensions": [{"diameter": .6, "drill": .3}]}}}), encoding="utf-8")
        self.dsn_path.write_text('''(pcb "board" (unit mm) (library
          (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 0.6))
            (shape (circle B.Cu 0.6)))))''', encoding="utf-8")
        self.dsn = DsnInput(self.dsn_path, file_digest(self.dsn_path), ExportTicket.begin(self.board_path),
                            frozenset({"N", "OTHER"}), frozenset({"F.Cu", "B.Cu"}), "board")
        base = f'''(via (at 7 8) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
                     (net 1) (uuid "{UUID2}")'''
        bad_metadata = (
            "(capping yes)",
            "(capping)",
            "(capping (front no))",
            "(covering (front yes) (back no))",
            "(covering (front no))",
            "(covering (front no) (back no) (inside no))",
            "(plugging (front no) (back yes))",
            "(plugging (front no) (back no) (foo no))",
            "(filling yes)",
            "(filling no (extra no))",
            "(unknown_metadata no)",
        )
        duplicate_metadata = (
            "(capping no) (capping no)",
            "(covering (front no) (back no)) (covering (front no) (back no))",
            "(plugging (front no) (back no)) (plugging (front no) (back no))",
            "(filling no) (filling no)",
        )
        for metadata in bad_metadata + duplicate_metadata:
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValidationError):
                    import_copper(BOARD, self.result(base + " " + metadata + ")"), self.dsn,
                                  via_catalog={"trusted": spec})

    def test_via_dimensions_must_match_catalog(self):
        via = f'''(via (at 7 8) (size 0.7) (drill 0.3) (layers "F.Cu" "B.Cu")
                    (net 1) (uuid "{UUID2}"))'''
        with self.assertRaisesRegex(ValidationError, "trusted via catalog"):
            import_copper(BOARD, self.result(via), self.dsn,
                          via_catalog={"trusted": ViaSpec(.6, .3, ("F.Cu", "B.Cu"))})

    def test_rejects_sub_nanometer_catalog_mismatch_and_via_layer_removal(self):
        almost = f'''(via (at 7 8) (size 0.6000006) (drill 0.3) (layers "F.Cu" "B.Cu")
                     (net 1) (uuid "{UUID2}"))'''
        spec = ViaSpec(.6, .3, ("F.Cu", "B.Cu"))
        with self.assertRaisesRegex(ValidationError, "trusted via catalog"):
            import_copper(BOARD, self.result(almost), self.dsn, via_catalog={"trusted": spec})
        remove_unused = f'''(via (at 7 8) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
                           (remove_unused_layers) (net 1) (uuid "{UUID2}"))'''
        with self.assertRaisesRegex(ValidationError, "unrecognized via attributes"):
            import_copper(BOARD, self.result(remove_unused), self.dsn, via_catalog={"trusted": spec})


if __name__ == "__main__":
    unittest.main()
