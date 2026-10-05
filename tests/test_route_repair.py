import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from velatrace.candidate import CopperItem
from velatrace.errors import ValidationError
from velatrace.kicad_cli import DrcResult
from velatrace.routing import ValidationReport, plan_digest
from velatrace.route_repair import repair_dangling
from velatrace.ses import RoutePlan, Track, Via, ViaSpec


BOARD = '(kicad_pcb (net 1 "N"))'


class _Safety:
    def __init__(self):
        self.stale = False

    def assert_matches(self, dsn, *, expected_board=None):
        if self.stale:
            raise ValidationError("Board changed during DRC; route again.")
        return BOARD


class _Dsn:
    def __init__(self):
        self.digest = "dsn-digest"
        self.ticket = SimpleNamespace(board_path=Path("board.kicad_pcb"), board_digest="board-digest")
        self.stale = False

    def assert_unchanged(self):
        if self.stale:
            raise ValidationError("DSN changed during DRC; route again.")


class _Validator:
    def __init__(self, plan, *, target_id="seg-1", failure=None, constraints=()):
        self.safety = _Safety()
        self.evidence = None
        self.constraints = constraints
        self.target_id = target_id
        self.failure = failure
        self.calls = []
        self.plan = plan
        items = []
        for ti, track in enumerate(plan.tracks):
            for ei, (a, b) in enumerate(zip(track.points_mm, track.points_mm[1:])):
                items.append(CopperItem(f"seg-{ti}-{ei+1}", "segment", track.net, track.layer,
                                         (round(a[0], 6), round(-a[1], 6)),
                                         (round(b[0], 6), round(-b[1], 6)), round(track.width_mm, 6)))
        for vi, via in enumerate(plan.vias):
            items.append(CopperItem(f"via-{vi+1}", "via", via.net,
                                    "F.Cu", (round(via.position_mm[0], 6),
                                             round(-via.position_mm[1], 6)), None,
                                    round(via.spec.diameter_mm, 6), round(via.spec.drill_mm, 6)))
        self.items = tuple(items)

    @staticmethod
    def _rule_sets(_constraints):
        return (0, .2)

    @staticmethod
    def _with_rule(files, _name, _clearance):
        return files

    @staticmethod
    def _context_files(_context):
        return {}

    def validate(self, dsn, plan, constraints):
        self.calls.append(plan)
        cleaned = plan != self.plan
        violations = 1 if not cleaned else 0
        report = ValidationReport(
            plan_digest(plan), violations, 0,
            enforced_constraint_ids=frozenset(item.id for item in constraints),
            board_digest=dsn.ticket.board_digest,
        )
        self.evidence = (dsn.digest, plan_digest(plan), report, self.items, {}, "snapshot")
        return report

    def _drc(self, _name, text, _files, _baseline):
        ids = set(re.findall(r'\(uuid "([^"]+)"\)', text))
        issues = []
        if self.target_id in ids:
            issues.append(("track_dangling", "warning", (self.target_id,)))
        if self.failure == "new_issue" and self.target_id not in ids:
            issues.append(("clearance", "error", ("foreign-pad", "foreign-track")))
        unconnected = int(self.failure == "unconnected" and self.target_id not in ids)
        return DrcResult(len(issues), unconnected, 0, tuple(issues))


class RepairDanglingTests(unittest.TestCase):
    def plan(self, points=((0, 0), (1, 0), (2, 0))):
        return RoutePlan("board", (Track("N", "F.Cu", .25, points),), ())

    def test_removes_issue_segment_and_preserves_other_route_geometry(self):
        plan = self.plan()
        validator, dsn = _Validator(plan, target_id="seg-0-1"), _Dsn()
        result = repair_dangling(plan, dsn, (), validator)
        self.assertEqual(result.removed_segments, 1)
        self.assertEqual(result.passes, 1)
        self.assertEqual(result.plan.tracks,
                         (Track("N", "F.Cu", .25, ((1, 0), (2, 0))),))
        self.assertIsNone(validator.evidence)
        self.assertIn("final independent DRC passed", result.details[-1])

    def test_middle_edge_removal_splits_polyline(self):
        plan = self.plan(((0, 0), (1, 0), (2, 0), (3, 0)))
        validator, dsn = _Validator(plan, target_id="seg-0-2"), _Dsn()
        result = repair_dangling(plan, dsn, (), validator)
        self.assertEqual(result.removed_segments, 1)
        self.assertEqual(result.plan.tracks, (
            Track("N", "F.Cu", .25, ((0, 0), (1, 0))),
            Track("N", "F.Cu", .25, ((2, 0), (3, 0))),
        ))

    def test_new_drc_issue_rolls_back(self):
        plan = self.plan()
        validator, dsn = _Validator(plan, target_id="seg-0-1", failure="new_issue"), _Dsn()
        result = repair_dangling(plan, dsn, (), validator)
        self.assertEqual(result.plan, plan)
        self.assertEqual(result.removed_segments, 0)
        self.assertIsNone(validator.evidence)

    def test_unconnected_candidate_rolls_back(self):
        plan = self.plan()
        validator, dsn = _Validator(plan, target_id="seg-0-1", failure="unconnected"), _Dsn()
        result = repair_dangling(plan, dsn, (), validator)
        self.assertEqual(result.plan, plan)
        self.assertEqual(result.removed_segments, 0)

    def test_stale_input_propagates_and_evidence_is_cleared(self):
        plan = self.plan()
        validator, dsn = _Validator(plan, target_id="seg-0-1"), _Dsn()
        validator.safety.stale = True
        with self.assertRaisesRegex(ValidationError, "Board changed"):
            repair_dangling(plan, dsn, (), validator)
        self.assertIsNone(validator.evidence)

    def test_via_warning_is_never_treated_as_a_removable_segment(self):
        plan = RoutePlan("board", (Track("N", "F.Cu", .25, ((0, 0), (1, 0))),),
                         (Via("N", (.5, 0), ViaSpec(.6, .3, ("F.Cu", "B.Cu"))),))
        validator, dsn = _Validator(plan, target_id="via-1"), _Dsn()
        result = repair_dangling(plan, dsn, (), validator)
        self.assertEqual(result.plan, plan)
        self.assertEqual(result.removed_segments, 0)

    def test_mismatched_uuid_to_edge_evidence_is_refused(self):
        plan = self.plan()
        validator, dsn = _Validator(plan, target_id="seg-0-1"), _Dsn()
        validator.items = (validator.items[0].__class__(
            validator.items[0].id, "segment", "N", "F.Cu", (99, 0), (1, 0), .25),
            *validator.items[1:])
        with self.assertRaisesRegex(ValidationError, "mapping is ambiguous"):
            repair_dangling(plan, dsn, (), validator)

    def test_duplicate_via_uuid_is_refused(self):
        plan = RoutePlan("board", (Track("N", "F.Cu", .25, ((0, 0), (1, 0))),),
                         (Via("N", (.5, 0), ViaSpec(.6, .3, ("F.Cu", "B.Cu"))),))
        validator, dsn = _Validator(plan, target_id="seg-0-1"), _Dsn()
        via = validator.items[-1]
        validator.items = (*validator.items[:-1], CopperItem(
            "seg-0-1", via.kind, via.net, via.layer, via.start, via.end, via.width, via.drill))
        with self.assertRaisesRegex(ValidationError, "UUID mapping is ambiguous"):
            repair_dangling(plan, dsn, (), validator)

    def test_bad_final_evidence_rolls_back(self):
        plan = self.plan()
        validator, dsn = _Validator(plan, target_id="seg-0-1"), _Dsn()
        original_validate = validator.validate

        def mismatch(dsn_arg, candidate, constraints):
            report = original_validate(dsn_arg, candidate, constraints)
            if candidate != plan:
                validator.evidence = (dsn_arg.digest, "wrong-plan", report, validator.items, {}, "snapshot")
            return report

        validator.validate = mismatch
        result = repair_dangling(plan, dsn, (), validator)
        self.assertEqual(result.plan, plan)
        self.assertEqual(result.removed_segments, 0)

    def test_deadline_after_candidate_drc_rolls_back(self):
        plan = self.plan()
        validator, dsn = _Validator(plan, target_id="seg-0-1"), _Dsn()
        clock = [0.0]
        original_drc = validator._drc

        def late_drc(*args):
            result = original_drc(*args)
            clock[0] = 2.0
            return result

        validator._drc = late_drc
        with patch("velatrace.route_repair.time.monotonic", side_effect=lambda: clock[0]):
            result = repair_dangling(plan, dsn, (), validator, timeout_seconds=1)
        self.assertEqual(result.plan, plan)
        self.assertEqual(result.removed_segments, 0)

    def test_already_clean_route_skips_trial_drc(self):
        plan = self.plan()
        validator, dsn = _Validator(plan), _Dsn()
        validator.target_id = "missing"
        def clean_validate(dsn_arg, candidate, constraints):
            report = ValidationReport(plan_digest(candidate), 0, 0,
                enforced_constraint_ids=frozenset(item.id for item in constraints),
                board_digest=dsn_arg.ticket.board_digest)
            validator.evidence = (dsn_arg.digest, plan_digest(candidate), report, validator.items, {}, "snapshot")
            return report
        validator.validate = clean_validate
        calls = []
        validator._drc = lambda *args: calls.append(args)
        result = repair_dangling(plan, dsn, (), validator)
        self.assertEqual(result.plan, plan)
        self.assertFalse(calls)
        self.assertEqual(result.details, ("Initial independent validation is already clean.",))

    def test_tool_failure_before_evidence_cannot_hide_changed_input(self):
        plan = self.plan()
        validator, dsn = _Validator(plan), _Dsn()
        def fail(*args):
            dsn.stale = True
            raise RuntimeError('tool failed')
        validator.validate = fail
        with self.assertRaisesRegex(ValidationError, 'DSN changed'):
            repair_dangling(plan, dsn, (), validator)
        self.assertIsNone(validator.evidence)


if __name__ == "__main__":
    unittest.main()
