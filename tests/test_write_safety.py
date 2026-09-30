"""Failure-injection tests; these do not claim real KiCad undo/DRC execution."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from velatrace.candidate import (SafeCandidateValidator, candidate_text, canonical,
                                context_matches, live_board_text, prepare_copper, project_context)
from velatrace.constraints import Constraint, Scope
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import CapabilityError, ValidationError
from velatrace.kicad_cli import DrcResult
from velatrace.ses import RoutePlan, Track
from velatrace.sexpr import parse
from velatrace.write_safety import BoardSafety, SafeBoardWriter, UncertainWriteError


BOARD = '(kicad_pcb (version 20241229) (generator "pcbnew") (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (58 "User.9" user)) (net 0 "") (net 1 "N") (gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") (uuid "outline")))'


class Item:
    def __init__(self, identifier, value="geometry"):
        self.id = SimpleNamespace(value=identifier)
        self.signature = value.encode()


class FakeBoard:
    def __init__(self, path):
        self.name = str(path)
        self.path = path
        self.source = BOARD
        self.items = {}
        self.events = []
        self.fail = ""

    def get_as_string(self):
        self.events.append("backup")
        if self.fail == "backup":
            raise OSError("disk full")
        graphics = ''.join(f'(gr_line (start 1 1) (end 2 2) (layer "User.9") (uuid "{identifier}"))'
                           for identifier in self.items)
        return self.source[:-1] + graphics + ')'

    def save_as(self, filename, **options):
        # KiCad rewrites the real project on every copy; it crashed a live project manager.
        raise AssertionError("VelaTrace must never ask KiCad to save a copy of the board")

    def begin_commit(self):
        self.events.append("begin")
        self.previous = copy.deepcopy(self.items)
        if self.fail == "begin":
            raise TimeoutError()
        return 1

    def create_items(self, items):
        self.events.append("create")
        values = items[:1] if self.fail == "partial" else items
        for item in values:
            self.items[item.id.value] = item
        return values

    def remove_items(self, items):
        self.events.append("remove")
        if self.fail != "remove":
            for item in items:
                self.items.pop(item.id.value)

    def get_shapes(self):
        return list(self.items.values())

    def get_text(self):
        return []

    def get_tracks(self):
        return list(self.items.values())

    def get_vias(self):
        return []

    def push_commit(self, commit, message):
        self.events.append("push")
        if self.fail == "push":
            raise TimeoutError()

    def drop_commit(self, commit):
        self.events.append("drop")
        if self.fail == "rollback":
            raise TimeoutError()
        self.items = self.previous


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "board.kicad_pcb"
        self.path.write_text(BOARD, encoding="utf-8")
        self.path.with_suffix(".kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rule_severities": {}, "via_dimensions": []}}}), encoding="utf-8")
        self.board = FakeBoard(self.path)
        self.safety = BoardSafety(self.board, self.path)
        self.signatures = patch("velatrace.write_safety._signature", lambda item: item.signature)
        self.signatures.start()
        self.addCleanup(self.signatures.stop)
        echoes = patch("velatrace.write_safety._echoes", lambda sent, got: sent.signature == got.signature)
        echoes.start()
        self.addCleanup(echoes.stop)
        # Fake KiCad reports per-item deletion status like DeleteItemsResponse.
        delete = patch("velatrace.write_safety.ItemFactory.delete", staticmethod(lambda board, items: (
            board.remove_items(items), all(item.id.value not in board.items for item in items))[1]))
        delete.start()
        self.addCleanup(delete.stop)
        self.dsnpath = self.path.with_suffix(".dsn")
        self.dsnpath.write_text('(pcb "board" (unit mm) (library))', encoding="utf-8")
        self.dsn = DsnInput(self.dsnpath, file_digest(self.dsnpath), ExportTicket.begin(self.path), frozenset({"N"}), frozenset({"F.Cu", "B.Cu"}))
        self.plan = RoutePlan("board", (Track("N", "F.Cu", .25, ((1, -2), (10, -2))),), ())

    def mutate(self, items, **kwargs):
        return self.safety._mutate(items, remove_owned=True, message="test", **kwargs)

    def test_backup_failure_prevents_begin_but_unsaved_graphics_are_allowed(self):
        self.board.fail = "backup"
        with self.assertRaises(CapabilityError):
            self.mutate([Item("a")])
        self.assertNotIn("begin", self.board.events)
        self.board.fail = ""
        self.board.source = BOARD.replace("20 20", "21 20")
        self.mutate([Item("a")], temporary=True)
        self.assertIn("begin", self.board.events)
        self.assertEqual(self.path.read_text(encoding="utf-8"), BOARD)

    def test_unsaved_preview_can_apply_against_validated_live_board(self):
        # The source has a legitimate unsaved outline edit before validation.
        self.board.source = BOARD.replace("20 20", "21 20")
        calls = []
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(
            drc=lambda path: (calls.append(path.read_text()), DrcResult(0, 0, 0))[1]))
        report = validator.validate(self.dsn, self.plan, ())
        self.assertTrue(all("21 20" in text for text in calls))
        self.mutate([Item("preview-a"), Item("preview-b")], temporary=True)
        self.assertIn("preview-a", self.board.get_as_string())
        self.board.events.clear()
        self.safety.factory.copper = lambda items, board: [Item(item.id) for item in items]
        SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report)
        self.assertEqual(self.board.events.count("push"), 1)
        self.assertNotIn("preview-a", self.board.items)
        self.assertFalse(self.safety.owned)
        self.assertIn("21 20", self.board.source)
        self.assertEqual(self.path.read_text(encoding="utf-8"), BOARD)

    def test_board_changes_after_validation_refuse_but_cleanup_preserves_edits(self):
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(0, 0, 0)))
        report = validator.validate(self.dsn, self.plan, ())
        self.mutate([Item("preview")], temporary=True)
        self.board.source = BOARD.replace("20 20", "22 20")
        self.board.events.clear()
        with self.assertRaisesRegex(ValidationError, "changed after routing validation"):
            SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report)
        self.assertNotIn("begin", self.board.events)
        self.safety.clear_preview()
        self.assertIn("22 20", self.board.source)
        self.assertFalse(self.safety.owned)

    def test_snapshot_strips_only_owned_graphics_and_preserves_quoted_fields(self):
        text = BOARD[:-1] + r'(gr_text "quoted \"text\"\nC:\\tmp" (uuid "other")) (gr_line (uuid "ours")))'
        cleaned = live_board_text(text, {"ours"})
        self.assertEqual(canonical(parse(text, kicad=True), {"ours"}), canonical(parse(cleaned, kicad=True)))
        self.assertIn('"other"', cleaned)
        self.assertNotIn('"ours"', cleaned)

    def test_edit_between_backup_and_commit_cancels_before_copper_creation(self):
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(0, 0, 0)))
        report = validator.validate(self.dsn, self.plan, ())
        self.safety.factory.copper = lambda items, board: [Item(item.id) for item in items]
        begin = self.board.begin_commit
        def edit_then_begin():
            self.board.source = BOARD.replace("20 20", "23 20")
            return begin()
        self.board.begin_commit = edit_then_begin
        with self.assertRaises(ValidationError):
            SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report)
        self.assertNotIn("create", self.board.events)
        self.assertIn("drop", self.board.events)
        self.assertIn("23 20", self.board.source)
        self.assertFalse(self.safety.blocked)

    def test_partial_create_rolls_back_whole_transaction(self):
        self.board.fail = "partial"
        with self.assertRaises(ValidationError):
            self.mutate([Item("a"), Item("b")])
        self.assertEqual(self.board.items, {})
        self.assertEqual(self.board.events, ["backup", "begin", "create", "drop"])
        self.assertTrue(self.safety.last_backup.saved.is_file())
        self.assertTrue((self.safety.last_backup.directory / "intent.json").is_file())

    def test_uncertain_push_blocks_retry(self):
        self.board.fail = "push"
        with self.assertRaises(UncertainWriteError):
            self.mutate([Item("a")])
        count = len(self.board.events)
        with self.assertRaises(UncertainWriteError):
            self.mutate([Item("a")])
        self.assertEqual(len(self.board.events), count)

    def test_acknowledged_commit_without_readable_copper_never_reports_success(self):
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(0, 0, 0)))
        report = validator.validate(self.dsn, self.plan, ())
        self.safety.factory.copper = lambda items, board: [Item(item.id) for item in items]
        self.board.get_tracks = lambda: []
        writer = SafeBoardWriter(self.safety, validator)
        with self.assertRaisesRegex(UncertainWriteError, "actual copper could not be verified"):
            writer.apply(self.dsn, self.plan, report)
        self.assertTrue(self.safety.blocked)
        self.assertEqual(self.board.events.count("push"), 1)
        with self.assertRaises(UncertainWriteError):
            writer.apply(self.dsn, self.plan, report)
        self.assertEqual(self.board.events.count("push"), 1)

    def test_failed_rollback_is_not_claimed_safe(self):
        self.board.fail = "rollback"
        self.board.create_items = lambda items: []
        with self.assertRaises(UncertainWriteError):
            self.mutate([Item("a")])
        self.assertTrue(self.safety.blocked)

    def test_preview_removal_and_final_add_are_single_commit(self):
        foreign = Item("not-owned")
        self.board.items[foreign.id.value] = foreign
        self.mutate([Item("preview")], temporary=True)
        self.board.events.clear()
        self.mutate([Item("copper")])
        self.assertEqual(self.board.events, ["backup", "begin", "remove", "create", "push"])
        self.assertEqual(set(self.board.items), {"not-owned", "copper"})
        self.assertEqual(self.safety.owned, {})

    def test_failed_removal_and_edited_preview_refuse(self):
        self.mutate([Item("preview")], temporary=True)
        self.board.fail = "remove"
        with self.assertRaises(ValidationError):
            self.mutate([Item("copper")])
        self.assertEqual(set(self.board.items), {"preview"})
        self.board.items["preview"].signature = b"edited"
        with self.assertRaises(ValidationError):
            self.safety.clear_preview()

    def test_reported_but_unperformed_removal_blocks_after_commit(self):
        # Live KiCad returns no per-item delete results; only a post-commit read proves removal.
        self.mutate([Item("preview")], temporary=True)
        with patch("velatrace.write_safety.ItemFactory.delete", staticmethod(lambda board, items: True)):
            with self.assertRaises(UncertainWriteError):
                self.mutate([Item("copper")])
        self.assertTrue(self.safety.blocked)

    def test_postcommit_read_failure_blocks_retry_and_preserves_recovery_intent(self):
        self.mutate([Item("preview")], temporary=True)
        self.board.events.clear()
        get_shapes = self.board.get_shapes
        def fail_after_commit():
            if "push" in self.board.events:
                raise TimeoutError("IPC read failed after commit")
            return get_shapes()
        self.board.get_shapes = fail_after_commit
        with self.assertRaises(UncertainWriteError):
            self.mutate([Item("copper")])
        self.assertTrue(self.safety.blocked)
        self.assertEqual(set(self.board.items), {"copper"})
        self.assertTrue((self.safety.last_backup.directory / "intent.json").is_file())
        self.assertFalse((self.safety.last_backup.directory / "completion.json").exists())
        events = list(self.board.events)
        with self.assertRaises(UncertainWriteError):
            self.mutate([Item("retry")])
        self.assertEqual(self.board.events, events)

    def test_creation_without_removals_needs_no_postcommit_read(self):
        def fail_read():
            if "push" in self.board.events:
                raise TimeoutError("No cleanup read should be needed")
            return []
        self.board.get_shapes = fail_read
        self.mutate([Item("preview")], temporary=True)
        self.assertEqual(set(self.safety.owned), {"preview"})
        self.assertTrue((self.safety.last_backup.directory / "completion.json").is_file())

    def test_foreign_coincident_preview_refuses_before_commit(self):
        from kipy.proto.board.board_types_pb2 import BL_User_9
        from velatrace.write_safety import ItemFactory
        old = ItemFactory.preview(self.plan, BL_User_9)[0]
        new = ItemFactory.preview(self.plan, BL_User_9)[0]
        self.board.get_shapes = lambda: [old]
        with self.assertRaisesRegex(ValidationError, r"overlap 1 User.9 line.*\(1, 2\)-\(10, 2\) mm"):
            self.mutate([new], temporary=True)
        self.assertNotIn("begin", self.board.events)

    def preview_items(self, plan, layer):
        from velatrace.write_safety import ItemFactory
        items = ItemFactory.preview(plan, layer)
        for item in items:
            item.signature = item.id.value.encode()
        return items

    def routed_preview(self):
        """The UI order: prepare before routing, validate, then show the preview."""
        self.safety.factory.preview = self.preview_items
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(0, 0, 0)))
        self.safety.prepare_preview()
        validator.validate(self.dsn, self.plan, ())
        self.safety.show_preview(self.dsn, self.plan, validator.evidence[5])

    def test_stale_own_preview_from_earlier_run_is_replaced(self):
        self.routed_preview()
        stale = set(self.board.items)
        self.assertTrue(stale)
        # Plugin restarted (or the preview was saved/restored by Undo): only the journal knows it.
        self.safety = BoardSafety(self.board, self.path)
        self.routed_preview()
        self.assertFalse(stale & set(self.board.items))
        self.assertEqual(len(self.board.items), len(stale))
        self.assertEqual(set(self.board.items), set(self.safety.owned))

    def test_unjournaled_preview_lookalike_blocks_before_routing_and_is_kept(self):
        from kipy.proto.board.board_types_pb2 import BL_User_9, BL_User_8
        orphan = self.preview_items(self.plan, BL_User_9)[0]
        mine = self.preview_items(self.plan, BL_User_9)[0]
        mine.attributes.stroke.width = 150_000  # the user's own drawing, not preview-styled
        elsewhere = self.preview_items(self.plan, BL_User_8)[0]
        for item in (mine, elsewhere):
            self.board.items[item.id.value] = item
        self.safety.prepare_preview()  # foreign non-preview graphics do not block
        self.board.items[orphan.id.value] = orphan
        with self.assertRaisesRegex(ValidationError, r"1 dashed 0.1 mm line.*\(1, 2\)-\(10, 2\) mm.*not started"):
            self.safety.prepare_preview()
        self.assertNotIn("begin", self.board.events)
        self.assertEqual(set(self.board.items), {orphan.id.value, mine.id.value, elsewhere.id.value})

    def test_journaled_ids_on_other_layers_are_never_adopted(self):
        from kipy.proto.board.board_types_pb2 import BL_User_8
        other = self.preview_items(self.plan, BL_User_8)[0]
        self.board.items[other.id.value] = other
        folder = self.safety.directory / "old"
        folder.mkdir()
        (folder / "completion.json").write_text(json.dumps({"status": "committed", "owned_ids": [other.id.value]}))
        self.safety.prepare_preview()
        self.assertIn(other.id.value, self.board.items)
        self.assertNotIn("begin", self.board.events)

    def test_collision_check_is_direction_independent_and_preserves_layers(self):
        from kipy.proto.board.board_types_pb2 import BL_User_9, BL_User_8
        from velatrace.write_safety import ItemFactory, _check_graphic_collisions
        old = ItemFactory.preview(self.plan, BL_User_9)[0]
        new = ItemFactory.preview(self.plan, BL_User_9)[0]
        start, end = new.start, new.end
        new.start, new.end = end, start
        with self.assertRaises(ValidationError):
            _check_graphic_collisions([new], [old], set())
        _check_graphic_collisions([new], [old], {old.id.value})
        old.layer = BL_User_8
        _check_graphic_collisions([new], [old], set())
        with self.assertRaisesRegex(ValidationError, "duplicate segments"):
            _check_graphic_collisions([new, new], [], set())

    def test_candidate_preserves_original_geometry_and_quantizes_once(self):
        items = prepare_copper(self.plan, self.dsn)
        output = candidate_text(BOARD, items)
        self.assertEqual(canonical(parse(BOARD)), canonical(parse(output), {items[0].id}))
        self.assertEqual(items[0].start, (1, 2))
        self.assertEqual(self.path.read_text(encoding="utf-8"), BOARD)
        self.path.write_text(output, encoding="utf-8")
        with self.assertRaises(CapabilityError):
            prepare_copper(self.plan, self.dsn)

    def test_power_typed_inner_plane_is_copper(self):
        self.path.write_text(BOARD.replace('(31 "B.Cu" signal)', '(4 "In1.Cu" power) (31 "B.Cu" signal)'), encoding="utf-8")
        self.dsn = DsnInput(self.dsnpath, file_digest(self.dsnpath), ExportTicket.begin(self.path),
                            frozenset({"N"}), frozenset({"F.Cu", "In1.Cu", "B.Cu"}))
        self.assertEqual(len(prepare_copper(self.plan, self.dsn)), 1)

    def test_context_creation_and_ignored_rules_invalidate(self):
        _, context = project_context(self.path)
        self.assertTrue(context_matches(context))
        self.path.with_suffix(".kicad_dru").write_text("(version 1)")
        self.assertFalse(context_matches(context))
        self.path.with_suffix(".kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rule_severities": {"clearance": "ignore"}}}}))
        with self.assertRaises(CapabilityError):
            project_context(self.path)

    def test_real_validator_requires_cli_evidence_and_writer_binds_it(self):
        calls = []
        cli = SimpleNamespace(drc=lambda path: (calls.append(path.read_text()), DrcResult(0, 0, 0))[1])
        validator = SafeCandidateValidator(self.safety, cli)
        report = validator.validate(self.dsn, self.plan, ())
        self.assertEqual(len(calls), 2)  # unrouted baseline and the candidate (run concurrently)
        baseline, routed = sorted(calls, key=lambda text: "(segment" in text)
        self.assertNotIn("(segment", baseline)
        self.assertIn("(segment", routed)
        self.safety.factory = SimpleNamespace(copper=lambda items, board: [Item(item.id) for item in items])
        SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report)
        self.assertIsNone(validator.evidence)
        self.assertEqual(self.board.events.count("push"), 1)
        with self.assertRaises(ValidationError):
            SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report)

    def test_nonzero_drc_never_writes(self):
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(1, 0, 0)))
        report = validator.validate(self.dsn, self.plan, ())
        with self.assertRaises(ValidationError):
            SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report)
        self.assertNotIn("begin", self.board.events)

    def test_drc_override_bypasses_only_the_drc_gate_and_is_journaled(self):
        self.safety.factory = SimpleNamespace(copper=lambda items, board: [Item(item.id) for item in items])
        # Unconnected items still refuse, even with the override.
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(1, 1, 0)))
        report = validator.validate(self.dsn, self.plan, ())
        with self.assertRaises(ValidationError):
            SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report, drc_override=True)
        # A board change after validation still refuses, even with the override.
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(1, 0, 0)))
        report = validator.validate(self.dsn, self.plan, ())
        self.board.source = BOARD.replace("20 20", "22 20")
        with self.assertRaisesRegex(ValidationError, "changed after routing validation"):
            SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report, drc_override=True)
        self.assertNotIn("begin", self.board.events)
        self.board.source = BOARD
        report = validator.validate(self.dsn, self.plan, ())
        self.assertEqual(report.drc_violations, 1)
        SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report, drc_override=True)
        self.assertEqual(self.board.events.count("push"), 1)
        for name in ("intent.json", "completion.json"):
            record = json.loads((self.safety.last_backup.directory / name).read_text())["drc_override"]
            self.assertEqual(record, {"violations": 1, "blocking_reasons": list(report.blocking_reasons)})
            self.assertTrue(record["blocking_reasons"])

    def test_clean_approval_journals_no_override(self):
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(0, 0, 0)))
        report = validator.validate(self.dsn, self.plan, ())
        self.safety.factory = SimpleNamespace(copper=lambda items, board: [Item(item.id) for item in items])
        SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report, drc_override=True)
        self.assertNotIn("drc_override", json.loads((self.safety.last_backup.directory / "completion.json").read_text()))

    def test_preview_before_drc_binds_the_board_it_was_drawn_on(self):
        """The UI order: prepare, show the preview, then validate in the background."""
        self.safety.factory.preview = self.preview_items
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=lambda path: DrcResult(0, 0, 0)))
        self.safety.prepare_preview()
        snapshot = self.safety.show_preview(self.dsn, self.plan)
        self.assertTrue(self.safety.owned)
        report = validator.validate(self.dsn, self.plan, ())
        self.assertEqual(validator.evidence[5], snapshot)  # the preview is excluded from validation
        self.safety.factory.copper = lambda items, board: [Item(item.id) for item in items]
        self.board.events.clear()
        SafeBoardWriter(self.safety, validator).apply(self.dsn, self.plan, report)
        self.assertEqual(self.board.events.count("push"), 1)
        self.assertFalse(self.safety.owned)
        # An edit between preview and validation is visible as a different snapshot.
        snapshot = self.safety.show_preview(self.dsn, self.plan)
        self.board.source = BOARD.replace("20 20", "21 20")
        validator.validate(self.dsn, self.plan, ())
        self.assertNotEqual(validator.evidence[5], snapshot)

    def test_clearance_runs_original_and_supplemental_rules(self):
        rules = self.path.with_suffix(".kicad_dru")
        rules.write_text('(version 1)\n(rule "stronger" (constraint clearance (min 1.0)))')
        calls = []
        def drc(path):
            calls.append(path.with_suffix(".kicad_dru").read_text())
            return DrcResult(0, 0, 0)
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=drc))
        constraint = Constraint("clear", Scope.SESSION, "clearance", "all nets", .3)
        validator.validate(self.dsn, self.plan, (constraint,))
        self.assertEqual(len(calls), 4)  # (baseline, candidate) under project rules, then with the extra rule
        self.assertEqual(sum("VelaTrace confirmed clearance" in rules_text for rules_text in calls), 2)
        self.assertTrue(all("stronger" in rules_text for rules_text in calls))
        self.assertNotIn("VelaTrace", rules.read_text())

    def test_baseline_drc_is_reused_only_for_identical_board_and_rules(self):
        calls = []
        def drc(path):
            calls.append((path.read_text(), path.with_suffix(".kicad_dru").read_text()
                          if path.with_suffix(".kicad_dru").exists() else ""))
            return DrcResult(0, 0, 0)
        validator = SafeCandidateValidator(self.safety, SimpleNamespace(drc=drc))
        validator.prepare(self.dsn, ())  # Warm-up while the router would run.
        self.assertEqual(len(calls), 1)
        validator.validate(self.dsn, self.plan, ())
        self.assertEqual(sum("(segment" not in board for board, _ in calls), 1)  # baseline reused
        self.assertEqual(len(calls), 2)
        # Changed rules or an unsaved live edit are different inputs: the baseline runs again.
        self.path.with_suffix(".kicad_dru").write_text('(version 1)\n(rule "new" (constraint clearance (min 0.5)))')
        validator.validate(self.dsn, self.plan, ())
        self.board.source = BOARD.replace("20 20", "21 20")
        validator.validate(self.dsn, self.plan, ())
        self.assertEqual(sum("(segment" not in board for board, _ in calls), 3)
        self.assertIn('"new"', calls[-1][1])

    def test_rules_change_during_commit_rolls_back(self):
        _, context = project_context(self.path)
        create = self.board.create_items
        def change_rules(items):
            self.path.with_suffix(".kicad_dru").write_text("(version 1)")
            return create(items)
        self.board.create_items = change_rules
        with self.assertRaises(ValidationError):
            self.mutate([Item("copper")], context=context)
        self.assertEqual(self.board.items, {})
        self.assertNotIn("push", self.board.events)

    def test_live_board_text_metadata_is_not_a_design_change(self):
        # KiCad's in-memory board text stamps each footprint with format metadata.
        saved = '(kicad_pcb (version 20260206) (footprint "R" (layer "F.Cu") (uuid "u") (at 1 2)))'
        live = ('(kicad_pcb (version 20260206) (footprint "R" (version 20260206) (generator "pcbnew") '
                '(generator_version "10.0") (layer "F.Cu") (uuid "u") (at 1 2)))')
        self.assertEqual(canonical(parse(saved, kicad=True)), canonical(parse(live, kicad=True)))
        moved = live.replace("(at 1 2)", "(at 1 3)")
        self.assertNotEqual(canonical(parse(saved, kicad=True)), canonical(parse(moved, kicad=True)))

    def test_backup_reads_live_board_without_touching_the_project(self):
        project = self.path.with_suffix(".kicad_pro")
        before = (project.read_bytes(), project.stat().st_mtime_ns)
        backup = self.safety.backup()
        self.assertEqual(backup.live.read_text(encoding="utf-8"), BOARD)
        self.assertEqual((backup.directory / "saved.kicad_pro").read_bytes(), before[0])
        self.assertEqual((project.read_bytes(), project.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()


class EchoTests(unittest.TestCase):
    """Shapes captured from a live KiCad 10.0.6 create_items echo."""
    def test_sdk_unwrapped_preview_segment_keeps_exact_geometry(self):
        from kipy.board import pack_any, unwrap
        from kipy.board_types import BoardShape
        from kipy.proto.board.board_types_pb2 import BL_User_9
        from velatrace.write_safety import ItemFactory, _echoes
        for points in (((0, 0), (1, 0)), ((1, -1), (2, -1)), ((2, -1), (2, -2))):
            plan = RoutePlan("board", (Track("N", "F.Cu", .25, points),), ())
            sent = ItemFactory.preview(plan, BL_User_9)[0]
            echo = unwrap(pack_any(sent.proto))
            self.assertIs(type(echo), BoardShape)
            self.assertTrue(_echoes(sent, echo))
            echo.proto.shape.segment.start.x_nm += 1
            self.assertFalse(_echoes(sent, echo))
            echo = unwrap(pack_any(sent.proto))
            echo.proto.shape.circle.SetInParent()
            self.assertFalse(_echoes(sent, echo))

    def test_zero_coordinates_and_via_geometry_must_match(self):
        from kipy.board_types import Track, Via
        from kipy.geometry import Vector2
        from kipy.proto.board.board_types_pb2 import BL_B_Cu, PSS_RECTANGLE
        from velatrace.write_safety import _echoes
        for kind in (Track, Via):
            sent = kind()
            sent.id.value = "a"
            sent.proto.net.name = "VIN"
            sent.locked = False
            if kind is Track:
                sent.start, sent.end, sent.width = Vector2.from_xy(0, 0), Vector2.from_xy(1_000_000, 0), 250_000
                changes = (
                    lambda item: setattr(item, "start", Vector2.from_xy(9_000_000, 7_000_000)),
                    lambda item: setattr(item, "end", Vector2.from_xy(1_000_000, 8_000_000)),
                    lambda item: setattr(item, "layer", BL_B_Cu),
                )
            else:
                sent.position, sent.diameter, sent.drill_diameter = Vector2.from_xy(0, 0), 600_000, 300_000
                changes = (
                    lambda item: setattr(item, "position", Vector2.from_xy(9_000_000, 7_000_000)),
                    lambda item: setattr(item, "diameter", 700_000),
                    lambda item: setattr(item, "drill_diameter", 400_000),
                    lambda item: setattr(item.proto.pad_stack.drill, "start_layer", BL_B_Cu),
                    lambda item: setattr(item.proto.pad_stack.copper_layers[0].offset, "x_nm", 1),
                    lambda item: setattr(item.proto.pad_stack.copper_layers[0], "shape", PSS_RECTANGLE),
                )
            for change in (*changes, lambda item: setattr(item, "locked", True)):
                echo = kind(proto=type(sent.proto)())
                echo.proto.CopyFrom(sent.proto)
                self.assertTrue(_echoes(sent, echo))
                change(echo)
                with self.subTest(kind=kind.__name__, echo=str(echo.proto)):
                    self.assertFalse(_echoes(sent, echo))

    def test_zero_coordinates_in_preview_and_annotation_must_match(self):
        from kipy.geometry import Vector2
        from kipy.proto.board.board_types_pb2 import BL_User_9
        from velatrace.write_safety import ItemFactory, _echoes
        plan = RoutePlan("board", (Track("N", "F.Cu", .25, ((0, 0), (1, 0))),), ())
        segment = ItemFactory.preview(plan, BL_User_9)[0]
        text = ItemFactory.annotations([("annotation", 0, 0)], BL_User_9)[0]
        for sent, field in ((segment, "start"), (text, "position")):
            proto = type(sent.proto)()
            proto.CopyFrom(sent.proto)
            echo = type(sent)(proto=proto)
            setattr(echo, field, Vector2.from_xy(1, 0))
            self.assertFalse(_echoes(sent, echo))

    def test_server_defaults_and_nameonly_net_accepted_geometry_changes_refused(self):
        from kipy.board_types import Track, Via
        from kipy.geometry import Vector2
        from velatrace.write_safety import _echoes
        for kind in (Track, Via):
            sent = kind()
            sent.id.value = "a"
            sent.proto.net.code.value, sent.proto.net.name = 1, "VIN"
            if kind is Track:
                sent.start, sent.end, sent.width = Vector2.from_xy(0, 0), Vector2.from_xy(1_000_000, 0), 250_000
            else:
                sent.position, sent.diameter, sent.drill_diameter = Vector2.from_xy(0, 0), 600_000, 300_000
            echo = kind(proto=type(sent.proto)())
            echo.proto.CopyFrom(sent.proto)
            echo.proto.net.ClearField("code")  # KiCad 10 names nets only.
            echo.proto.parent.value = "board-uuid"
            if kind is Via:
                echo.proto.pad_stack.unconnected_layer_removal = 1  # server-filled default
            with self.subTest(kind=kind.__name__):
                self.assertTrue(_echoes(sent, echo))
                moved = kind(proto=type(echo.proto)())
                moved.proto.CopyFrom(echo.proto)
                moved.proto.net.name = "GND"
                self.assertFalse(_echoes(sent, moved))
        track = Track(); track.width = 250_000
        other = Track(); other.width = 300_000
        self.assertFalse(_echoes(track, other))


class RouteIssueTests(unittest.TestCase):
    """A route is judged by what it adds to the unrouted board's DRC."""
    WARN = ("lib_footprint_mismatch", "warning", ("fp-oled",))
    ERR = ("clearance", "error", ("pad-a", "pad-b"))

    def judge(self, before, after):
        from velatrace.candidate import route_issues
        return route_issues(DrcResult(len(before), 0, 0, tuple(before)), DrcResult(len(after), 0, 0, tuple(after)))

    def test_preexisting_warnings_are_reported_not_blocking(self):
        self.assertEqual(self.judge([self.WARN], [self.WARN]), (0, 1))

    def test_route_added_issue_blocks(self):
        new = ("clearance", "error", ("pad-a", "track-new"))
        self.assertEqual(self.judge([self.WARN], [self.WARN, new]), (1, 1))
        # Same type and pads as before, but involving the new track, is still new.
        self.assertEqual(self.judge([], [("silk", "warning", ("track-new",))]), (1, 0))

    def test_preexisting_errors_still_block(self):
        self.assertEqual(self.judge([self.ERR], [self.ERR]), (1, 0))

    def test_blocking_reasons_exclude_carried_warnings(self):
        from velatrace.candidate import blocking_reasons
        self.assertEqual(blocking_reasons(DrcResult(1, 0, 0, (self.WARN,)),
                                         DrcResult(2, 0, 0, (self.WARN, self.ERR))),
                         ("clearance (error): 1",))
        self.assertTrue(blocking_reasons(DrcResult(1, 0, 0), DrcResult(1, 0, 0)))

    def test_duplicates_counted_and_unknown_identities_count_everything(self):
        self.assertEqual(self.judge([self.WARN], [self.WARN, self.WARN]), (1, 1))
        from velatrace.candidate import route_issues
        self.assertEqual(route_issues(DrcResult(3, 0, 0), DrcResult(3, 0, 0)), (3, 0))
