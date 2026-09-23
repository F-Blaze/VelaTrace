"""Failure-injection tests; these do not claim real KiCad undo/DRC execution."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from velatrace.candidate import (SafeCandidateValidator, candidate_text, canonical,
                                context_matches, prepare_copper, project_context)
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

    def save_as(self, filename, **options):
        self.events.append("backup")
        if self.fail == "backup":
            raise OSError("disk full")
        Path(filename).write_text(self.source, encoding="utf-8")
        Path(filename).with_suffix(".kicad_pro").write_bytes(self.path.with_suffix(".kicad_pro").read_bytes())

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
        self.dsnpath = self.path.with_suffix(".dsn")
        self.dsnpath.write_text('(pcb "board" (unit mm) (library))', encoding="utf-8")
        self.dsn = DsnInput(self.dsnpath, file_digest(self.dsnpath), ExportTicket.begin(self.path), frozenset({"N"}), frozenset({"F.Cu", "B.Cu"}))
        self.plan = RoutePlan("board", (Track("N", "F.Cu", .25, ((1, -2), (10, -2))),), ())

    def mutate(self, items, **kwargs):
        return self.safety._mutate(items, remove_owned=True, message="test", **kwargs)

    def test_backup_failure_and_unsaved_edits_prevent_begin(self):
        self.board.fail = "backup"
        with self.assertRaises(CapabilityError):
            self.mutate([Item("a")])
        self.assertNotIn("begin", self.board.events)
        self.board.fail = ""
        self.board.source = BOARD.replace("20 20", "21 20")
        with self.assertRaises(ValidationError):
            self.mutate([Item("a")])
        self.assertNotIn("begin", self.board.events)

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

    def test_candidate_preserves_original_geometry_and_quantizes_once(self):
        items = prepare_copper(self.plan, self.dsn)
        output = candidate_text(BOARD, items)
        self.assertEqual(canonical(parse(BOARD)), canonical(parse(output), {items[0].id}))
        self.assertEqual(items[0].start, (1, 2))
        self.assertEqual(self.path.read_text(encoding="utf-8"), BOARD)
        self.path.write_text(output, encoding="utf-8")
        with self.assertRaises(CapabilityError):
            prepare_copper(self.plan, self.dsn)

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
        self.assertEqual(len(calls), 1)
        self.assertIn("(segment", calls[0])
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
        self.assertEqual(len(calls), 2)
        self.assertNotIn("VelaTrace", calls[0])
        self.assertIn("stronger", calls[1])
        self.assertIn("VelaTrace confirmed clearance", calls[1])
        self.assertNotIn("VelaTrace", rules.read_text())

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

    def test_unsaved_live_project_changes_refuse_before_begin(self):
        save = self.board.save_as
        def changed_project(filename, **kwargs):
            save(filename, **kwargs)
            Path(filename).with_suffix(".kicad_pro").write_text(json.dumps({"board": {"design_settings": {"different": True}}}))
        self.board.save_as = changed_project
        with self.assertRaises(ValidationError):
            self.mutate([Item("copper")])
        self.assertNotIn("begin", self.board.events)


if __name__ == "__main__":
    unittest.main()
