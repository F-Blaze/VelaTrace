"""JLCPCB assignment write-back (fake IPC board, failure injection) and fabrication export."""
import csv
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

from kipy.board_types import BoardLayer, Field

from velatrace import jlc_assign, jlc_export
from velatrace.errors import ValidationError
from velatrace.kicad_cli import KiCadCli
from velatrace.write_safety import BoardSafety, UncertainWriteError

FIXTURES = Path(__file__).parent / "fixtures" / "audit"
HEAD = ('(kicad_pcb (version 20241229) (generator "pcbnew") (layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
        '(36 "B.SilkS" user) (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user)) ')


def field(name, value):
    item = Field()
    item.name, item.text.value = name, value
    return item


def footprint(reference, value="10k", fields=(), layer="F.Cu", library="Resistor_SMD:R_0402_1005Metric",
              attr="smd", at="10 20 90"):
    definition = SimpleNamespace(items=[field(n, v) for n, v in fields])
    definition.add_item = definition.items.append
    return SimpleNamespace(
        reference_field=field("Reference", reference), value=value, library=library, attr=attr, at=at,
        definition=definition, position=field("p", "").text.position,
        layer=BoardLayer.BL_F_Cu if layer == "F.Cu" else BoardLayer.BL_B_Cu, layer_name=layer)


class FakeBoard:
    def __init__(self, path, footprints):
        self.name, self.footprints, self.events, self.fail = str(path), footprints, [], ""
        self.pending = None

    def get_footprints(self):
        # Like kipy: the caller gets copies; the board changes only through update_items.
        return [footprint(fp.reference_field.text.value, fp.value,
                          [(f.name, f.text.value) for f in fp.definition.items], fp.layer_name,
                          fp.library, fp.attr, fp.at) for fp in self.footprints]

    def get_as_string(self):
        body = ""
        for fp in self.footprints:
            props = "".join(f'(property "{f.name}" "{f.text.value}" (at 0 0) (layer "F.Fab"))'
                            for f in fp.definition.items)
            body += (f'(footprint "{fp.library}" (layer "{fp.layer_name}") (uuid "u-{fp.reference_field.text.value}") '
                     f'(at {fp.at}) (property "Reference" "{fp.reference_field.text.value}") '
                     f'(property "Value" "{fp.value}") {props} (attr {fp.attr}))')
        return HEAD + body + ")"

    def begin_commit(self):
        self.events.append("begin")
        self.before = list(self.footprints)
        return 1

    def update_items(self, items):
        self.events.append("update")
        if self.fail == "update":
            raise TimeoutError()
        by_reference = {fp.reference_field.text.value: fp for fp in items}
        self.footprints = [by_reference.get(fp.reference_field.text.value, fp) for fp in self.footprints]
        if self.fail == "other":
            self.footprints[-1].value = "changed by KiCad"
        return items

    def push_commit(self, commit, message):
        self.events.append("push:" + message)
        if self.fail == "push":
            raise TimeoutError()

    def drop_commit(self, commit):
        self.events.append("drop")
        self.footprints = self.before  # KiCad discards the pending changes


class AssignTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "board.kicad_pcb"
        self.board = FakeBoard(self.path, [
            footprint("R1"), footprint("R2", fields=[("LCSC", "C111")]),
            footprint("R3", fields=[("MPN", "X"), ("SPN1", "C222")]),
            footprint("R4", fields=[("Supplier Part Number", ""), ("JLCPCB Part", "")]),
            footprint("C1", "100n", library="Capacitor_SMD:C_0402_1005Metric", layer="B.Cu")])
        self.path.write_text(self.board.get_as_string(), encoding="utf-8")
        self.safety = BoardSafety(self.board, self.path)

    def fields(self, reference):
        fp = next(fp for fp in self.board.footprints if fp.reference_field.text.value == reference)
        return [(f.name, f.text.value) for f in fp.definition.items]

    def test_one_backed_up_commit_writes_only_the_part_number_fields(self):
        result = jlc_assign.assign_lcsc(self.safety, {"R1": "c25744", "R2": "C25744", "R3": "C25744",
                                                      "R4": "C25744", "C1": "C1525"})
        self.assertEqual(result.changed, ("C1", "R1", "R2", "R3", "R4"))
        self.assertEqual(self.board.events, ["begin", "update", "push:" + jlc_assign.COMMIT_MESSAGE])
        self.assertEqual(self.fields("R1"), [("LCSC", "C25744")])               # new hidden field
        self.assertEqual(self.fields("R2"), [("LCSC", "C25744")])               # existing field reused
        self.assertEqual(self.fields("R3"), [("MPN", "X"), ("SPN1", "C25744")])  # the field it is read from
        self.assertEqual(self.fields("R4"), [("Supplier Part Number", ""), ("JLCPCB Part", "C25744")])
        new = next(fp for fp in self.board.footprints if fp.reference_field.text.value == "C1").definition.items[0]
        self.assertEqual((new.visible, new.layer), (False, BoardLayer.BL_B_Fab))
        journal = json.loads((result.backup / "completion.json").read_text(encoding="utf-8"))
        self.assertEqual((journal["status"], journal["assign"]["C1"]), ("committed", "C1525"))
        self.assertIn('"SPN1" "C222"', (result.backup / "live.kicad_pcb").read_text(encoding="utf-8"))
        self.assertIn("Update Schematic from PCB", result.summary)

    def test_nothing_to_do_makes_no_commit(self):
        result = jlc_assign.assign_lcsc(self.safety, {"R2": "C111"})
        self.assertEqual((result.changed, result.unchanged, self.board.events), ((), ("R2",), []))

    def test_bad_requests_are_refused_before_any_transaction(self):
        self.board.footprints.append(footprint("R1"))
        for request in ({"R1": "C1"}, {"R99": "C1"}, {"R2": "12345"}, {"R2": "C1; DROP"}, {}):
            with self.assertRaises(ValidationError):
                jlc_assign.assign_lcsc(self.safety, request)
        self.assertEqual(self.board.events, [])

    def test_failed_update_is_rolled_back(self):
        self.board.fail = "update"
        with self.assertRaisesRegex(ValidationError, "nothing was changed"):
            jlc_assign.assign_lcsc(self.safety, {"R1": "C1"})
        self.assertEqual((self.board.events, self.fields("R1"), self.safety.blocked),
                         (["begin", "update", "drop"], [], False))

    def test_uncertain_commit_blocks_retries(self):
        self.board.fail = "push"
        with self.assertRaises(UncertainWriteError):
            jlc_assign.assign_lcsc(self.safety, {"R1": "C1"})
        self.assertTrue(self.safety.blocked)
        self.board.fail = ""
        with self.assertRaises(UncertainWriteError):
            jlc_assign.assign_lcsc(self.safety, {"R1": "C1"})

    def test_any_other_board_change_is_detected_after_the_commit(self):
        self.board.fail = "other"
        with self.assertRaisesRegex(UncertainWriteError, "Undo"):
            jlc_assign.assign_lcsc(self.safety, {"R1": "C1"})

    def test_assignment_csv(self):
        target = Path(self.directory.name) / "assign.csv"
        jlc_assign.export_assignments(target, [("R10", "10k", "R_0402", "C25744"), ("R2", "1k", "R_0402", "C11702")])
        rows = list(csv.reader(target.open(encoding="utf-8")))
        self.assertEqual(rows, [["Reference", "Value", "Footprint", "LCSC"],
                                ["R2", "1k", "R_0402", "C11702"], ["R10", "10k", "R_0402", "C25744"]])


BOARD = HEAD + "".join((
    '(footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 10 20 90) (property "Reference" "R1") '
    '(property "Value" "10k") (property "LCSC" "C25744") (attr smd))',
    '(footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 12 20) (property "Reference" "R2") '
    '(property "Value" "10k") (property "LCSC" "C25744") (attr smd))',
    '(footprint "Package_TO_SOT_SMD:SOT-23" (layer "B.Cu") (at 30 40 90) (property "Reference" "Q1") '
    '(property "Value" "2N7002") (property "SPN1" "C8545") (attr smd))',
    '(footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" (layer "F.Cu") (at 50 60 0) (property "Reference" "U1") '
    '(property "Value" "LM358") (attr smd))',
    '(footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 14 20) (property "Reference" "R3") '
    '(property "Value" "10k") (property "LCSC" "C25744") (attr smd dnp))',
    '(footprint "TestPoint:TestPoint_Pad_D1.0mm" (layer "F.Cu") (at 1 1) (property "Reference" "TP1") '
    '(property "Value" "TP") (attr exclude_from_pos_files exclude_from_bom))',
    '(footprint "Fiducial:Fiducial_1mm" (layer "F.Cu") (at 2 2) (property "Reference" "FID1") '
    '(property "Value" "Fiducial") (attr smd exclude_from_bom))')) + ")"
POSITIONS = ("Ref,Val,Package,PosX,PosY,Rot,Side\n"
             '"R1","10k","R_0402_1005Metric",10.000000,-20.000000,90.000000,top\n'
             '"R2","10k","R_0402_1005Metric",12.000000,-20.000000,0.000000,top\n'
             '"Q1","2N7002","SOT-23",30.000000,-40.000000,90.000000,bottom\n'
             '"U1","LM358","SOIC-8_3.9x4.9mm_P1.27mm",50.000000,-60.000000,0.000000,top\n'
             '"R3","10k","R_0402_1005Metric",14.000000,-20.000000,0.000000,top\n'
             '"FID1","Fiducial","Fiducial_1mm",2.000000,-2.000000,0.000000,top\n')


class ExportTests(unittest.TestCase):
    def test_board_parts_reads_attributes_side_and_part_number(self):
        parts, layers = jlc_export.board_parts(BOARD)
        by_ref = {part.reference: part for part in parts}
        self.assertEqual((by_ref["Q1"].side, by_ref["Q1"].lcsc, by_ref["Q1"].footprint), ("bottom", "C8545", "SOT-23"))
        self.assertTrue(by_ref["R3"].dnp and by_ref["TP1"].exclude_from_bom and by_ref["TP1"].exclude_from_pos)
        self.assertIn("Edge.Cuts", layers)
        with self.assertRaises(ValidationError):
            jlc_export.board_parts("(kicad_sch)")

    def test_bom_groups_lines_and_respects_dnp_and_exclude_from_bom(self):
        parts, _ = jlc_export.board_parts(BOARD)
        self.assertEqual(jlc_export.bom_rows(parts), [
            ("2N7002", "Q1", "SOT-23", "C8545"),
            ("10k", "R1,R2", "R_0402_1005Metric", "C25744"),
            ("LM358", "U1", "SOIC-8_3.9x4.9mm_P1.27mm", "")])

    def test_cpl_uses_jlcpcb_rotation_convention_and_corrections(self):
        parts, _ = jlc_export.board_parts(BOARD)
        rows, rotated = jlc_export.cpl_rows(POSITIONS, parts, jlc_export.load_rotations())
        self.assertEqual(rows, [
            ("FID1", "2.0000mm", "-2.0000mm", "Top", "0"),
            ("Q1", "30.0000mm", "-40.0000mm", "Bottom", "0"),     # (180-90) mirrored, +270 for SOT-23
            ("R1", "10.0000mm", "-20.0000mm", "Top", "90"),
            ("R2", "12.0000mm", "-20.0000mm", "Top", "0"),
            ("U1", "50.0000mm", "-60.0000mm", "Top", "270")])     # SOIC +270; DNP R3 is left out
        self.assertEqual(rotated, ["Q1 +270° (^SOT-23)", "U1 +270° (^SOIC-)"])

    def test_user_rotation_file_wins_and_bad_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / jlc_export.ROTATIONS_FILE).write_text(
                "Footprint pattern,Correction\n^SOT-23,90\nC25744,45\n([bad,10\n^SOIC-,many\n", encoding="utf-8")
            rotations = jlc_export.load_rotations(Path(directory))
        self.assertEqual(jlc_export.jlc_rotation(0, "top", "SOT-23", "", rotations), (90, "+90° (^SOT-23)"))
        self.assertEqual(jlc_export.jlc_rotation(0, "top", "R_0402", "C25744", rotations)[0], 45)
        self.assertEqual(jlc_export.jlc_rotation(0, "top", "SOIC-8", "", rotations)[0], 270)
        self.assertEqual(jlc_export.jlc_rotation(-90, "bottom", "R_0402", "", rotations), (270, ""))

    @unittest.skipUnless(os.getenv("VELATRACE_TEST_KICAD_CLI"), "Set VELATRACE_TEST_KICAD_CLI for the real export")
    def test_real_kicad_cli_writes_bom_cpl_and_gerber_zip_without_touching_the_project(self):
        with tempfile.TemporaryDirectory(prefix="velatrace-jlc-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            for suffix in (".kicad_pcb", ".kicad_pro"):
                (project / ("necessity" + suffix)).write_bytes((FIXTURES / ("necessity" + suffix)).read_bytes())
            before = {path.name: path.read_bytes() for path in project.iterdir()}
            out = Path(directory) / "out"
            result = jlc_export.export_fabrication(
                KiCadCli(os.environ["VELATRACE_TEST_KICAD_CLI"]), project / "necessity.kicad_pcb", out)
            self.assertEqual({path.name: path.read_bytes() for path in project.iterdir()}, before)
            self.assertEqual(sorted(path.name for path in out.iterdir()),
                             ["BOM-necessity.csv", "CPL-necessity.csv", "GERBER-necessity.zip"])
            bom = list(csv.reader((out / "BOM-necessity.csv").open(encoding="utf-8")))
            cpl = list(csv.reader((out / "CPL-necessity.csv").open(encoding="utf-8")))
            self.assertEqual(tuple(bom[0]), jlc_export.BOM_HEADER)
            self.assertEqual(tuple(cpl[0]), jlc_export.CPL_HEADER)
            self.assertEqual(len(cpl) - 1, result.placements)
            self.assertGreater(result.placements, 0)
            names = zipfile.ZipFile(out / "GERBER-necessity.zip").namelist()
            self.assertTrue(any(name.endswith(".gtl") for name in names) and any(name.endswith(".drl") for name in names)
                            and any(name.endswith(".gm1") for name in names), names)
            self.assertIn("saved board file", result.summary)


class CsvFormulaTests(unittest.TestCase):
    """Board text must not become a spreadsheet formula in exported CSV files."""

    def test_formula_like_cells_are_neutralised_and_plain_values_kept(self):
        hostile = ["=HYPERLINK(1)", "@SUM(1)", "+cmd|x!A0", "-2+3+cmd|x!A0", chr(9) + "=1", chr(13) + "=1"]
        for cell in hostile:
            self.assertEqual(jlc_export.csv_cell(cell), "'" + cell)
        for cell in ["10k", "100nF", "-5V", "+3.3V", "-12", "C25804", "R1, R2", "", 90]:
            self.assertEqual(jlc_export.csv_cell(cell), str(cell))

    def test_both_csv_writers_use_the_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            bom, sheet = Path(folder) / "bom.csv", Path(folder) / "assign.csv"
            jlc_export._write_csv(bom, ("Comment", "Designator"), [("=1+1", "R1")])
            jlc_assign.export_assignments(sheet, [("R1", "=1+1", "R_0603", "C25804")])
            for path in (bom, sheet):
                rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
                self.assertIn("'=1+1", rows[1])


if __name__ == "__main__":
    unittest.main()
