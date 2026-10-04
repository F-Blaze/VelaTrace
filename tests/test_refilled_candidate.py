import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from dataclasses import replace
from velatrace.candidate import canonical, candidate_text, prepare_copper
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import CapabilityError, ValidationError
from velatrace.kicad_cli import DrcResult, RefilledBoard
from velatrace.refilled_candidate import RefilledCandidateValidator
from velatrace.reference_benchmark import validate_and_refill, refill_preserves_copper
from velatrace.ses import RoutePlan, Track
from velatrace.sexpr import parse


BOARD = ('(kicad_pcb (version 20241229) (generator "pcbnew") '
         '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (58 "User.9" user)) '
         '(net 0 "") (net 1 "N") '
         '(gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) (type default)) '
         '(fill none) (layer "Edge.Cuts") (uuid "outline")))')


class FixtureSafety:
    def __init__(self, directory, board):
        self.directory = directory
        self.board_path = board

    def assert_matches(self, dsn, *, expected_board=None):
        dsn.assert_unchanged()
        text = self.board_path.read_text(encoding="utf-8")
        if expected_board is not None and canonical(parse(text, kicad=True)) != expected_board:
            raise ValidationError("Benchmark source changed during validation.")
        return text


class FakeCli:
    version = (10, 0, 4)

    def __init__(self, root, *, candidate_drc=None, mutation=None, bad_provenance=None):
        self.root = root
        self.candidate_drc = candidate_drc or DrcResult(0, 0, 0)
        self.mutation = mutation
        self.bad_provenance = bad_provenance
        self.calls = []

    @staticmethod
    def _digest_context(folder):
        names = ("board.kicad_pro", "board.kicad_dru", "board.kicad_sch")
        files = {name: (folder / name).read_bytes() for name in names if (folder / name).is_file()}
        return hashlib.sha256(repr(sorted(
            (name, hashlib.sha256(data).hexdigest()) for name, data in files.items()
        )).encode()).hexdigest()

    def drc(self, candidate):
        self.calls.append(("baseline", candidate.read_text(encoding="utf-8")))
        return DrcResult(0, 0, 0)

    def refill_for_analysis(self, candidate):
        text = candidate.read_text(encoding="utf-8")
        context_digest = self._digest_context(candidate.parent)
        source_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        self.calls.append(("refill", text))
        if self.mutation == "board":
            board = self.root / "board.kicad_pcb"
            board.write_text(BOARD.replace("20 20", "21 20"), encoding="utf-8")
        elif self.mutation == "project":
            project = self.root / "board.kicad_pro"
            project.write_text(project.read_text(encoding="utf-8") + " ", encoding="utf-8")
        values = {
            "text": text + "\n", "source_digest": source_digest,
            "context_digest": context_digest, "tool_version": self.version,
            "drc": self.candidate_drc,
        }
        if self.bad_provenance:
            values[self.bad_provenance] = "bad" if self.bad_provenance != "tool_version" else (9, 0, 6)
        return RefilledBoard(**values)


class RefilledCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.board = self.root / "board.kicad_pcb"
        self.board.write_text(BOARD, encoding="utf-8")
        self.board.with_suffix(".kicad_pro").write_text(json.dumps({"board": {"design_settings": {
            "rule_severities": {}, "via_dimensions": []}}}), encoding="utf-8")
        self.board.with_suffix(".kicad_dru").write_text("(version 1)\n", encoding="utf-8")
        dsn_path = self.root / "board.dsn"
        dsn_path.write_text('(pcb "board" (unit mm) (library))', encoding="utf-8")
        self.dsn = DsnInput(dsn_path, file_digest(dsn_path), ExportTicket.begin(self.board),
                            frozenset({"N"}), frozenset({"F.Cu", "B.Cu"}))
        self.plan = RoutePlan("board", (Track("N", "F.Cu", .25, ((1, -2), (10, -2))),), ())

    def validator(self, **kwargs):
        cli = FakeCli(self.root, **kwargs)
        return RefilledCandidateValidator(FixtureSafety(self.root, self.board), cli), cli

    def test_reuses_one_fresh_candidate_drc_and_keeps_actual_candidate_text(self):
        validator, cli = self.validator()
        report = validator.validate(self.dsn, self.plan, ())
        self.assertEqual([kind for kind, _ in cli.calls].count("baseline"), 1)
        self.assertEqual([kind for kind, _ in cli.calls].count("refill"), 1)
        self.assertIn("(segment", validator.candidate_text)
        self.assertEqual(next(text for kind, text in cli.calls if kind == 'refill'), validator.candidate_text)
        self.assertEqual(validator.fresh_result.drc, DrcResult(0, 0, 0))
        self.assertEqual(report.drc_violations, 0)
        self.assertIsNone(validator.evidence)  # Research results cannot authorize a board write.

    def test_nonzero_fresh_drc_remains_blocking(self):
        validator, _ = self.validator(candidate_drc=DrcResult(1, 1, 0))
        report = validator.validate(self.dsn, self.plan, ())
        self.assertEqual(report.drc_violations, 1)
        self.assertEqual(report.unconnected_count, 1)
        self.assertTrue(report.blocking_reasons)

    def test_stale_source_or_context_invalidates_and_clears_result(self):
        for mutation in ("board", "project"):
            with self.subTest(mutation=mutation):
                self.board.write_text(BOARD, encoding='utf-8')
                validator, _ = self.validator(mutation=mutation)
                with self.assertRaises(ValidationError):
                    validator.validate(self.dsn, self.plan, ())
                self.assertIsNone(validator.fresh_result)
                self.assertIsNone(validator.candidate_text)
                self.assertIsNone(validator.evidence)

    def test_mismatched_refill_provenance_is_rejected(self):
        for field in ("source_digest", "context_digest", "tool_version"):
            with self.subTest(field=field):
                validator, _ = self.validator(bad_provenance=field)
                with self.assertRaises(ValidationError):
                    validator.validate(self.dsn, self.plan, ())
                self.assertIsNone(validator.fresh_result)
                self.assertIsNone(validator.candidate_text)

    def test_constraints_are_refused_and_each_call_clears_previous_result(self):
        validator, _ = self.validator()
        validator.validate(self.dsn, self.plan, ())
        self.assertIsNotNone(validator.fresh_result)
        with self.assertRaises(CapabilityError):
            validator.validate(self.dsn, self.plan, (object(),))
        self.assertIsNone(validator.fresh_result)
        self.assertIsNone(validator.candidate_text)

    def test_second_candidate_runs_fresh_refill_and_reuses_only_identical_baseline(self):
        validator, cli = self.validator()
        validator.validate(self.dsn, self.plan, ())
        first_text = validator.candidate_text
        second = RoutePlan('board', (Track('N', 'F.Cu', .25, ((1, -2), (11, -2))),), ())
        validator.validate(self.dsn, second, ())
        self.assertNotEqual(first_text, validator.candidate_text)
        self.assertEqual([kind for kind, _ in cli.calls].count('refill'), 2)
        self.assertEqual([kind for kind, _ in cli.calls].count('baseline'), 1)
        self.assertIsNone(validator.evidence)
        self.assertTrue(validator.supports(()))
        self.assertFalse(validator.supports((object(),)))

    def test_benchmark_fusion_removes_one_drc_without_changing_acceptance(self):
        reports = []
        for fused in (False, True):
            work = self.root/str(fused)
            work.mkdir()
            cli = FakeCli(self.root)
            report, fresh, seconds = validate_and_refill(self.dsn, self.plan, cli, work, fused=fused)
            reports.append(report)
            self.assertEqual([kind for kind, _ in cli.calls].count('baseline'), 1 if fused else 2)
            self.assertEqual([kind for kind, _ in cli.calls].count('refill'), 1)
            self.assertEqual(fresh.drc, DrcResult(0, 0, 0))
            self.assertGreaterEqual(seconds, 0)
            self.assertEqual((work/'board.kicad_pro').read_bytes(), self.board.with_suffix('.kicad_pro').read_bytes())
        self.assertEqual(reports[0], reports[1])

    def test_refill_geometry_comparison_accepts_only_bound_dsn_identity_aliases(self):
        dsn = replace(self.dsn, base_design='C:/private/export/board.dsn')
        filled = candidate_text(BOARD, prepare_copper(self.plan, self.dsn))
        for identity in ('board', 'board.dsn', dsn.base_design):
            plan = replace(self.plan, base_design=identity)
            self.assertTrue(refill_preserves_copper(plan, filled, dsn, via_catalog={}))
        with self.assertRaises(ValidationError):
            refill_preserves_copper(replace(self.plan, base_design='other'), filled, dsn, via_catalog={})
        changed = filled.replace('(width 0.250000)', '(width 0.300000)')
        self.assertNotEqual(changed, filled)
        self.assertFalse(refill_preserves_copper(self.plan, changed, dsn, via_catalog={}))


if __name__ == "__main__":
    unittest.main()
