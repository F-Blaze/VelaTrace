from pathlib import Path
from dataclasses import replace
import tempfile
import threading
import unittest
from unittest.mock import patch

from velatrace.constraints import ConstraintStore, Scope, propose_constraint
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import CapabilityError, ValidationError
from velatrace.routing import Mode, RoutingSession, RoutingStage, ValidationReport, plan_digest
from velatrace.ses import ViaSpec, parse_ses
from velatrace.sexpr import parse


SES = '''(session board.ses (base_design board.dsn)
 (routes (resolution um 10) (network_out
  (net N (wire (path F.Cu 2500 10000 20000 30000 20000 40000 30000) (type route))))))'''


class FakeRouter:
    def check_startup(self):
        pass

    def route(self, dsn, constraints):
        return SES


class FakeValidator:
    missing = 0
    violations = 0

    def supports(self, constraints):
        return True

    def validate(self, dsn, plan, constraints):
        return ValidationReport(plan_digest(plan), self.violations, self.missing,
                                enforced_constraint_ids=frozenset(c.id for c in constraints),
                                board_digest=dsn.ticket.board_digest)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        board = self.root / "board.kicad_pcb"
        board.write_text("board fixture")
        dsn = self.root / "board.dsn"
        dsn.write_text("dsn fixture")
        self.dsn = DsnInput(dsn, file_digest(dsn), ExportTicket.begin(board), frozenset({"N"}), frozenset({"F.Cu", "B.Cu"}))
        self.store = ConstraintStore(self.root / "constraints.json")
        self.validator = FakeValidator()
        self.session = RoutingSession(self.store, FakeRouter(), self.validator)

    def test_session_binds_ses_placement_precision_before_validation(self):
        self.dsn = replace(self.dsn, placements={"U1": (10.4, 20, "front", 0)},
                           placement_resolution_mm=.0001)
        placement = "(placement (resolution um 10) (component pkg (place U1 104000 200000 front 0)))"
        ses = SES.replace("(routes", placement + " (routes", 1)
        self.ready()
        with patch.object(self.session.router, "route", return_value=ses):
            self.session.run()
        self.session.reject("Verify refusal of changed precision")
        self.session.confirm_constraints(self.store.fingerprint)
        coarse = ses.replace("(resolution um 10)", "(resolution mm 1)", 1)
        with patch.object(self.session.router, "route", return_value=coarse), \
                patch.object(self.validator, "validate") as validate, self.assertRaises(ValidationError):
            self.session.run()
        validate.assert_not_called()

    def ready(self):
        self.session.command("/autoroute")
        self.session.set_input(self.dsn, all_footprints_placed=True)
        self.session.confirm_constraints(self.store.fingerprint)

    def test_ses_math_and_coordinates(self):
        plan = parse_ses(SES, expected_design="board.dsn", nets={"N"}, layers={"F.Cu"})
        self.assertEqual(plan.trace_count, 2)
        self.assertEqual(plan.tracks[0].width_mm, .25)
        self.assertEqual(plan.tracks[0].points_mm[0], (1, 2))
        self.assertEqual(plan.layers_used, ("F.Cu",))

    def test_progress_and_timings_do_not_reroute_on_approval(self):
        self.ready()
        stages = []
        self.session.progress = stages.append
        with patch("velatrace.routing.perf_counter", side_effect=[10, 12, 13, 18]), \
                patch.object(self.session.router, "route", wraps=self.session.router.route) as route:
            self.session.run()
            from unittest.mock import Mock
            writer = Mock()
            self.session.approve(writer)
        self.assertEqual(stages, ["Routing copper paths", "Checking candidate DRC"])
        self.assertEqual(self.session.timings, {"router": 2, "validation": 5})
        route.assert_called_once()
        writer.apply.assert_called_once()
        self.assertEqual(self.session.stage, RoutingStage.APPROVED)

    def test_validator_warmup_overlaps_router_and_its_errors_do_not_leak(self):
        self.ready()
        started, order = threading.Event(), []
        def prepare(dsn, constraints):
            order.append("prepare")
            started.set()
            raise ValidationError("warm-up only")
        def route(dsn, constraints):
            # Deadlocks (and fails) if the warm-up were run before or after routing.
            self.assertTrue(started.wait(5))
            order.append("route")
            return SES
        original = self.validator.validate
        def validate(*args):
            order.append("validate")
            return original(*args)
        with patch.object(self.validator, "prepare", side_effect=prepare, create=True), \
                patch.object(self.session.router, "route", side_effect=route), \
                patch.object(self.validator, "validate", side_effect=validate):
            self.session.run()
        self.assertEqual(order, ["prepare", "route", "validate"])
        self.assertEqual(self.session.stage, RoutingStage.PREVIEW)

    def test_malformed_ses_and_unknown_geometry_refuse_all(self):
        for text in ("(session", SES.replace("(path", "(arc"), SES.replace("2500", "NaN"), SES.replace("F.Cu", "In1.Cu")):
            with self.assertRaises(ValidationError):
                parse_ses(text, expected_design="board.dsn", nets={"N"}, layers={"F.Cu"})

    def test_ses_known_via_library(self):
        text = '''(session board.ses (base_design board.dsn) (routes (resolution um 10)
        (library_out (padstack V (shape (circle F.Cu 6000)) (shape (circle B.Cu 6000)) (attach off)))
        (network_out (net N (via V 10000 20000 (type route))))))'''
        args = dict(expected_design="board.dsn", nets={"N"}, layers={"F.Cu", "B.Cu"})
        with self.assertRaises(ValidationError):
            parse_ses(text, **args)
        plan = parse_ses(text, **args, via_catalog={"V": ViaSpec(.6, .3, ("F.Cu", "B.Cu"))})
        self.assertEqual(plan.vias[0].position_mm, (1, 2))

    def test_quote_parser_metadata(self):
        self.assertEqual(parse('(parser (string_quote "))'), ["parser", ["string_quote", '"']])

    def test_router_duplicate_stubs_are_dropped_once_for_preview_and_copper(self):
        # Actual Freerouting 2.1.0 output (practice board): VIN pad-escape stubs are
        # emitted on F.Cu and B.Cu, each twice in opposite directions.
        from kipy.proto.board.board_types_pb2 import BL_User_9
        from velatrace.ses import RoutePlan, Track
        from velatrace.write_safety import ItemFactory, _check_graphic_collisions
        text = (Path(__file__).parent / "fixtures" / "routing" / "freerouting-2.1.0-duplicate-stubs.ses").read_text()
        plan = parse_ses(text, expected_design="practice", nets={"VIN", "GND", "VOUT"}, layers={"F.Cu", "B.Cu"},
                         via_catalog={"Via[0-1]_600:300_um": ViaSpec(.6, .3, ("F.Cu", "B.Cu"))},
                         expected_placements={"J1": (6, -15, "front", 0), "R1": (32, -8, "front", 0),
                                              "C1": (32, -22, "front", 0), "U1": (18, -15, "front", 0)},
                         expected_placement_resolution_mm=.0001)
        steps = [(t.net, t.layer, t.width_mm, frozenset(pair)) for t in plan.tracks
                 for pair in zip(t.points_mm, t.points_mm[1:])]
        self.assertEqual((len(steps), len(set(steps)), plan.trace_count, len(plan.vias)), (44, 44, 44, 2))
        _check_graphic_collisions(ItemFactory.preview(plan, BL_User_9), [], set())
        # Coincident lines of different nets are still refused.
        a, b = Track("VIN", "F.Cu", .2, ((0, 0), (1, 0))), Track("GND", "B.Cu", .2, ((1, 0), (0, 0)))
        with self.assertRaisesRegex(ValidationError, "duplicate segments"):
            _check_graphic_collisions(ItemFactory.preview(RoutePlan("x", (a, b), ()), BL_User_9), [], set())
        # Zero-length steps and identical vias are dropped too; a cut repeat splits the path.
        extra = SES.replace("40000 30000)", "40000 30000 40000 30000 30000 20000 50000 20000)").replace(
            "(type route))", "(type route)) (via V 1 2) (via V 1 2)")
        plan = parse_ses(extra, expected_design="board.dsn", nets={"N"}, layers={"F.Cu", "B.Cu"},
                         via_catalog={"V": ViaSpec(.6, .3, ("F.Cu", "B.Cu"))})
        self.assertEqual([t.points_mm for t in plan.tracks], [((1, 2), (3, 2), (4, 3)), ((3, 2), (5, 2))])
        self.assertEqual(len(plan.vias), 1)

    def test_numeric_proposal_never_drops_clause(self):
        self.assertEqual(propose_constraint("keep traces away from headers").minimum_mm, 2)
        self.assertEqual(propose_constraint("keep traces 5 mm away from headers").minimum_mm, 5)
        with self.assertRaises(ValidationError):
            propose_constraint("keep traces away from headers and avoid mounting holes")

    def test_scope_persistence(self):
        self.store.add(propose_constraint("avoid headers"))
        universal = propose_constraint("avoid headers", Scope.UNIVERSAL)
        self.store.add(universal)
        self.assertEqual(ConstraintStore(self.store.path).items, (universal,))

    def test_confirmations_and_constraint_invalidation(self):
        with self.assertRaises(ValidationError):
            self.session.run()
        self.ready()
        self.session.add_constraint(propose_constraint("avoid headers"))
        with self.assertRaises(ValidationError):
            self.session.run()
        self.session.confirm_constraints(self.store.fingerprint)
        self.session.run()
        self.assertEqual(self.session.stage, RoutingStage.PREVIEW)
        self.assertIn("All connections verified", self.session.summary)
        self.assertEqual(self.session.command("/autoroute_exit"), Mode.AUDIT)

    def test_reject_requires_reason_even_after_other_edits(self):
        self.ready()
        self.session.run()
        with self.assertRaises(ValidationError):
            self.session.reject("")
        self.session.add_constraint(propose_constraint("avoid headers"))
        with self.assertRaises(ValidationError):
            self.session.confirm_constraints(self.store.fingerprint)
        self.session.reject("Too close to headers")
        self.session.confirm_constraints(self.store.fingerprint)
        self.assertEqual(len(self.store.items), 1)

    def test_shortfall_and_unknown_drc_never_approve(self):
        for missing, violations in ((1, 0), (None, None), (0, None), (0, 1)):
            self.ready()
            self.validator.missing, self.validator.violations = missing, violations
            self.session.run()
            with self.assertRaises(ValidationError):
                self.session.approve(None)

    def test_approve_anyway_lifts_only_the_known_drc_gate(self):
        from unittest.mock import Mock
        for missing, violations in ((1, 2), (0, None)):
            self.ready()
            self.validator.missing, self.validator.violations = missing, violations
            self.session.run()
            with self.assertRaises(ValidationError):
                self.session.approve(Mock(), drc_override=True)
        self.ready()
        self.validator.missing, self.validator.violations = 0, 2
        self.session.run()
        writer = Mock()
        with self.assertRaisesRegex(ValidationError, "Approve anyway"):
            self.session.approve(writer)
        writer.apply.assert_not_called()
        self.session.approve(writer, drc_override=True)
        writer.apply.assert_called_once_with(self.dsn, self.session.plan, self.session.report, drc_override=True)
        self.assertEqual(self.session.stage, RoutingStage.APPROVED)

    def test_background_check_is_discarded_after_reroute_or_reject(self):
        self.ready()
        plan = self.session.route()
        generation = self.session.generation
        self.assertEqual(self.session.stage, RoutingStage.VALIDATING)
        with self.assertRaises(ValidationError):
            self.session.approve(None)  # previewable, not approvable, before DRC
        report = self.session.check(plan)
        self.session.reject("Wrong layer")  # while DRC was running
        self.assertFalse(self.session.accept(plan, report, generation))
        self.assertEqual(self.session.stage, RoutingStage.REJECTED)
        self.assertIsNone(self.session.report)
        self.session.confirm_constraints(self.store.fingerprint)
        old = self.session.route()
        stale = self.session.generation
        self.session.confirm_constraints(self.store.fingerprint)  # reroute
        new = self.session.route()
        self.assertFalse(self.session.accept(old, self.session.check(old), stale))
        self.assertEqual(self.session.stage, RoutingStage.VALIDATING)
        self.assertTrue(self.session.accept(new, self.session.check(new), self.session.generation))
        self.assertEqual(self.session.stage, RoutingStage.PREVIEW)
        # A failed check (None) for the current plan invalidates it.
        self.session.confirm_constraints(self.store.fingerprint)
        plan = self.session.route()
        self.assertTrue(self.session.accept(plan, None, self.session.generation))
        self.assertEqual(self.session.stage, RoutingStage.SETUP)

    def test_board_change_invalidates_run(self):
        self.ready()
        self.dsn.ticket.board_path.write_text("changed")
        with self.assertRaises(ValidationError):
            self.session.run()


if __name__ == "__main__":
    unittest.main()
