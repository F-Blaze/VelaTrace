from pathlib import Path
import tempfile
import unittest

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

    def test_board_change_invalidates_run(self):
        self.ready()
        self.dsn.ticket.board_path.write_text("changed")
        with self.assertRaises(ValidationError):
            self.session.run()


if __name__ == "__main__":
    unittest.main()
