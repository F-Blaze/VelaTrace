"""Behaviour that only real boards exercised: renamed layers and footprints, pours,
one-point SES paths, pre-existing DRC errors and dead-end stub repair."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from velatrace.candidate import CopperItem, Inspection, blocking_reasons, preexisting_errors, route_issues
from velatrace.dsn import ExportTicket, accept_export, layer_aliases
from velatrace.errors import ValidationError
from velatrace.freerouting import route_outer_pours
from velatrace.kicad_cli import _FONT_FACE, DrcResult, KiCadCli
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.route_repair import repair_dangling
from velatrace.routing import ValidationReport
from velatrace.ses import RoutePlan, Track, Via, ViaSpec, parse_ses
from velatrace.sexpr import parse

BOARD = '(kicad_pcb (layers (0 "F.Cu" signal "Front") (2 "B.Cu" signal "Back") (5 "F.SilkS" user "F.Silkscreen")))'
DSN = '''(pcb board.dsn
 (parser (string_quote ") (space_in_quoted_tokens on) (host_cad KiCad) (host_version 10.0))
 (resolution um 10)
 (unit um)
 (structure (layer Front (type signal)) (layer Back (type signal))
  (plane GND (polygon Back 0 0 0 9 0 9 9 0 0)))
 (placement
  (component R0805 (place R1 30000 -20000 front 0 (PN 10k)))
  (component Logo (place G*** 1000 -2000 front 0) (place G***_1 5000 -6000 back 0)))
 (library)
 (network (net GND (pins R1-1)) (net VIN (pins R1-2)))
 (wiring))
'''
PARTS = (Component("R1", "10k", "R0805", (Pin("1", "GND"), Pin("2", "VIN")), position_mm=(30, 20)),
         Component("G***", "", "Logo", (), position_mm=(5, 6)),
         Component("G***", "", "Logo", (), position_mm=(1, 2)))


class RenamedLayersAndFootprintsTests(unittest.TestCase):
    def accept(self, parts=PARTS, text=DSN, board=BOARD):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "board.kicad_pcb"
        path.write_text(board, encoding="utf-8")
        ticket = ExportTicket.begin(path)
        dsn = path.with_suffix(".dsn")
        dsn.write_text(text, encoding="utf-8")
        later = time.time_ns() + 1_000_000
        os.utime(dsn, ns=(later, later))
        return accept_export(ticket, dsn, DesignSnapshot(parts, "ipc-pcb", path, ("F.Cu", "B.Cu")),
                             user_confirms_saved_and_exported=True)

    def test_user_layer_names_map_to_canonical_board_layers(self):
        result = self.accept()
        self.assertEqual(result.layers, {"Front", "Back"})
        self.assertEqual(result.layer_aliases, {"Front": "F.Cu", "Back": "B.Cu"})
        self.assertEqual(result.board_layers, {"F.Cu", "B.Cu"})
        with self.assertRaises(ValidationError):  # A DSN layer the board does not have.
            self.accept(text=DSN.replace("(layer Back", "(layer Inner"))
        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            layer_aliases(BOARD.replace('"Front"', '"B.Cu"'))

    def test_ses_tracks_and_vias_use_canonical_layers_and_one_point_paths_are_dropped(self):
        ses = '''(session board (base_design board.dsn)
 (routes (resolution um 10)
  (library_out (padstack "Via[0-1]_600:300_um" (shape (circle Front 6000)) (shape (circle Back 6000)) (attach off)))
  (network_out (net VIN
   (wire (path Front 2500 0 0 10000 0))
   (wire (path Back 2500 10000 0))
   (via "Via[0-1]_600:300_um" 10000 0)))))'''
        spec = ViaSpec(.6, .3, ("F.Cu", "B.Cu"))
        kwargs = dict(expected_design="board.dsn", nets={"VIN"}, layers={"Front", "Back"},
                      via_catalog={"Via[0-1]_600:300_um": spec})
        plan = parse_ses(ses, **kwargs, layer_aliases={"Front": "F.Cu", "Back": "B.Cu"})
        self.assertEqual(plan.tracks, (Track("VIN", "F.Cu", .25, ((0, 0), (1, 0))),))
        self.assertEqual(plan.vias, (Via("VIN", (1, 0), spec),))
        with self.assertRaises(ValidationError):  # Without the mapping the via cannot span F.Cu/B.Cu.
            parse_ses(ses, **kwargs)

    def test_footprints_kicad_renames_on_export_match_by_position(self):
        result = self.accept()
        self.assertEqual(set(result.placements), {"R1", "G***", "G***_1"})
        # Freerouting omits pad-less footprints from the SES placement; connected ones must stay.
        self.assertEqual(result.unconnected_references, {"G***", "G***_1"})
        ses = "(session board (base_design board.dsn) (placement (resolution um 10) %s) (routes (resolution um 10) (network_out)))"
        kwargs = dict(expected_design="board.dsn", nets=set(), layers={"Front"}, expected_placements=result.placements,
                      expected_placement_resolution_mm=result.placement_resolution_mm)
        r1 = "(component R0805 (place R1 300000 -200000 front 0))"
        parse_ses(ses % r1, **kwargs, optional_placements=result.unconnected_references)
        parse_ses(ses % r1.replace("R0805 ", ""), **kwargs, optional_placements=result.unconnected_references)
        for text, optional in ((ses % r1, frozenset()), (ses % "", result.unconnected_references)):
            with self.subTest(text=text), self.assertRaisesRegex(ValidationError, "reference list changed"):
                parse_ses(text, **kwargs, optional_placements=optional)
        moved = (*PARTS[:2], Component("G***", "", "Logo", (), position_mm=(1, 2.5)))
        netted = (*PARTS[:2], Component("G***", "", "Logo", (Pin("1", "GND"),), position_mm=(1, 2)))
        for parts in (moved, netted, PARTS[:2], (*PARTS, Component("H1", "", "Hole", (), position_mm=(0, 0)))):
            with self.subTest(parts=parts), self.assertRaises(ValidationError):
                self.accept(parts)

    def test_outer_layer_pours_are_routed_with_tracks_and_inner_planes_kept(self):
        self.assertNotIn("(plane", route_outer_pours(DSN))
        four = DSN.replace("(layer Back (type signal))", "(layer In1.Cu (type signal)) (layer Back (type signal))")
        inner = four.replace("(polygon Back", "(polygon In1.Cu")
        self.assertEqual(route_outer_pours(inner), inner)
        both = route_outer_pours(inner.replace("(plane GND", "(plane VIN (polygon Front 0 0 0 9 0 9 9 0 0)) (plane GND"))
        self.assertEqual([row[1] for row in parse(both)[5] if row[0] == "plane"], ["GND"])


class DrcTests(unittest.TestCase):
    ERROR, WARN = ("starved_thermal", "error", ("a", "b")), ("silk", "warning", ("c",))
    NEW = ("clearance", "error", ("d", "e"))

    def test_errors_already_on_the_unrouted_board_are_reported_as_such_and_still_block(self):
        before = DrcResult(2, 3, 0, (self.ERROR, self.WARN))
        after = DrcResult(3, 0, 0, (self.ERROR, self.WARN, self.NEW))
        self.assertEqual(route_issues(before, after), (2, 1))
        self.assertEqual(preexisting_errors(before, after), 1)
        self.assertEqual(blocking_reasons(before, after),
                         ("clearance (error): 1", "starved thermal (error): 1 already on the unrouted board"))
        self.assertEqual(preexisting_errors(DrcResult(1, 0, 0), DrcResult(1, 0, 0)), 0)  # Identities unknown.
        with self.assertRaises(ValidationError):
            ValidationReport("d", 1, 0, preexisting_errors=-1)

    def test_candidate_drc_refills_zones_without_saving(self):
        cli = object.__new__(KiCadCli)
        cli._exportable, cli._export_lock = set(), threading.Lock()
        cli.version = (10, 0, 4)
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / "board.kicad_pcb"
            board.write_text("(kicad_pcb (zone (net 1)))")
            calls = []
            def run(args, cwd, allowed_exit_codes, config_home):
                calls.append(args)
                Path(args[args.index("--output") + 1]).write_text(json.dumps(
                    {"violations": [], "unconnected_items": [], "schematic_parity": []}))
                return 0
            with patch.object(cli, "_run", side_effect=run), \
                    patch.object(cli, "_drc_config_home", return_value=Path(directory)):
                cli.drc(board)
        self.assertIn("--refill-zones", calls[0])
        self.assertNotIn("--save-board", calls[0])

    def test_export_copy_drops_uninstalled_font_faces_only(self):
        text = '(gr_text "(face \\"x\\")" (effects (font (face "Avenir Next") (size 1 1))))'
        self.assertEqual(_FONT_FACE.sub("", text), '(gr_text "(face \\"x\\")" (effects (font  (size 1 1))))')


class FakeValidator:
    """inspect()/reuse() of SafeCandidateValidator; `judge(items)` plays KiCad DRC."""
    def __init__(self, judge):
        self.judge, self.reused, self.trials = judge, None, 0

    def inspect(self, dsn, plan, constraints, items=None):
        self.trials += 1
        if items is None:
            items = [CopperItem(f"s{t}.{e}", "segment", track.net, track.layer, a, b, track.width_mm)
                     for t, track in enumerate(plan.tracks)
                     for e, (a, b) in enumerate(zip(track.points_mm, track.points_mm[1:]))]
            items += [CopperItem(f"v{v}", "via", via.net, "F.Cu", via.position_mm, None, .6, .3)
                      for v, via in enumerate(plan.vias)]
        candidate = self.judge({item.id for item in items})
        return Inspection("dsn", repr(plan), tuple(constraints), tuple(items), {}, "snapshot",
                          [(DrcResult(0, 2, 0), candidate)])

    def reuse(self, inspection):
        self.reused = inspection


def dangling(*ids, unconnected=0, extra=()):
    issues = tuple(("via_dangling" if i.startswith("v") else "track_dangling", "warning", (i,)) for i in ids) + extra
    return DrcResult(len(issues), unconnected, 0, issues)


class RepairTests(unittest.TestCase):
    # Track 0: pad to pad with a spur (edges 0, 1 real; edge 2 then via 0 dead ends). Track 1 is clean.
    PLAN = RoutePlan("board", (Track("N", "F.Cu", .25, ((0, 0), (1, 0), (2, 0), (2, 1))),
                               Track("M", "F.Cu", .25, ((0, 5), (4, 5)))),
                     (Via("N", (2, 1), ViaSpec(.6, .3, ("F.Cu", "B.Cu"))),))

    def test_dead_end_chain_is_removed_and_the_final_inspection_is_what_validate_reuses(self):
        def judge(ids):  # KiCad reports the end of a stub first: the via, then the segment behind it.
            return dangling("v0") if "v0" in ids else dangling("s0.2") if "s0.2" in ids else dangling()
        validator = FakeValidator(judge)
        plan = repair_dangling(self.PLAN, None, (), validator).plan
        self.assertEqual(plan, RoutePlan("board", (Track("N", "F.Cu", .25, ((0, 0), (1, 0), (2, 0))),
                                                   self.PLAN.tracks[1]), ()))
        self.assertEqual(validator.reused.plan_digest, repr(plan))
        self.assertEqual({item.id for item in validator.reused.items}, {"s0.0", "s0.1", "s1.0"})

    def test_clean_route_costs_one_inspection_and_is_returned_unchanged(self):
        validator = FakeValidator(lambda ids: dangling())
        self.assertIs(repair_dangling(self.PLAN, None, (), validator).plan, self.PLAN)
        self.assertEqual((validator.trials, validator.reused.plan_digest), (1, repr(self.PLAN)))

    def test_removal_that_loses_a_connection_or_adds_an_issue_is_not_kept(self):
        new_issue = (("clearance", "error", ("s0.1", "pad")),)
        for after in (dangling(unconnected=1), dangling(extra=new_issue)):
            def judge(ids, after=after):
                return dangling("s0.2", "v0") if "s0.2" in ids else after
            validator = FakeValidator(judge)
            with self.subTest(after=after):
                self.assertIs(repair_dangling(self.PLAN, None, (), validator).plan, self.PLAN)
                self.assertEqual(validator.reused.plan_digest, repr(self.PLAN))

    def test_no_trial_starts_after_the_time_budget(self):
        validator = FakeValidator(lambda ids: dangling("s0.2", "v0"))
        self.assertIs(repair_dangling(self.PLAN, None, (), validator, budget_seconds=-1).plan, self.PLAN)
        self.assertEqual((validator.trials, validator.reused.plan_digest), (1, repr(self.PLAN)))

    def test_unknown_issue_identities_leave_the_plan_alone(self):
        validator = FakeValidator(lambda ids: DrcResult(1, 0, 0))
        self.assertIs(repair_dangling(self.PLAN, None, (), validator).plan, self.PLAN)


if __name__ == "__main__":
    unittest.main()
