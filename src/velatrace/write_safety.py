"""Sole live board mutation boundary: backups, exact ownership and IPC commits.

No API in this module saves over the user's board file. Approved copper remains
in KiCad's undo history until the user saves it using KiCad.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import threading
import uuid

from .candidate import canonical, context_matches, file_digest, read_board
from .errors import CapabilityError, ValidationError, VelaTraceError
from .routing import plan_digest


class UncertainWriteError(VelaTraceError):
    """The IPC result is uncertain; retry is blocked until manual reconciliation."""


@dataclass(frozen=True)
class Backup:
    directory: Path
    saved: Path
    live: Path


def _durable_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


class ItemFactory:
    """Only official client wrappers; imports are lazy for offline unit tests."""
    @staticmethod
    def copper(items, board):
        from kipy.board_types import Track, Via
        from kipy.geometry import Vector2
        from kipy.util.board_layer import CANONICAL_LAYER_NAMES
        layers = {name: value for value, name in CANONICAL_LAYER_NAMES.items()}
        nets = {net.name: net for net in board.get_nets()}
        output = []
        def vector(point):
            return Vector2.from_xy(*(round(value * 1_000_000) for value in point))
        for item in items:
            if item.net not in nets or item.layer not in layers:
                raise ValidationError("Live board net/layer mapping changed.")
            if item.kind == "segment":
                obj = Track()
                obj.start = vector(item.start)
                obj.end = vector(item.end)
                obj.layer = layers[item.layer]
                obj.width = round(item.width * 1_000_000)
            else:
                obj = Via()
                obj.position = vector(item.start)
                obj.diameter = round(item.width * 1_000_000)
                obj.drill_diameter = round(item.drill * 1_000_000)
            obj.net = nets[item.net]
            obj.locked = False
            obj.id.value = item.id
            output.append(obj)
        return output

    @staticmethod
    def preview(plan, layer):
        from kipy.board_types import BoardSegment
        from kipy.geometry import Vector2
        from kipy.proto.common.types.enums_pb2 import SLS_DASH
        output = []
        for track in plan.tracks:
            for start, end in zip(track.points_mm, track.points_mm[1:]):
                item = BoardSegment()
                item.id.value = str(uuid.uuid4())
                item.start = Vector2.from_xy_mm(start[0], -start[1])
                item.end = Vector2.from_xy_mm(end[0], -end[1])
                item.layer = layer
                item.locked = False
                item.attributes.stroke.width = 100_000
                item.attributes.stroke.style = SLS_DASH
                output.append(item)
        # Vias are shown as a small cross; no copper is created for previews.
        for via in plan.vias:
            x, y = via.position_mm
            for a, b in (((x-.2, -y), (x+.2, -y)), ((x, -y-.2), (x, -y+.2))):
                item = BoardSegment()
                item.id.value = str(uuid.uuid4())
                item.start, item.end = Vector2.from_xy_mm(*a), Vector2.from_xy_mm(*b)
                item.layer = layer
                item.locked = False
                item.attributes.stroke.width = 100_000
                item.attributes.stroke.style = SLS_DASH
                output.append(item)
        return output

    @staticmethod
    def annotations(rows, layer):
        from kipy.board_types import BoardText
        from kipy.geometry import Vector2
        output = []
        for text, x, y in rows:
            if not isinstance(text, str) or len(text) > 500:
                raise ValidationError("Annotation text exceeds its safe display limit.")
            item = BoardText()
            item.id.value = str(uuid.uuid4())
            item.value = text.replace("\n", " ").replace("\r", " ")
            item.position = Vector2.from_xy_mm(x, y)
            item.layer = layer
            item.locked = False
            item.attributes.size = Vector2.from_xy_mm(1, 1)
            item.attributes.stroke_width = 150_000
            output.append(item)
        return output


def _signature(item):
    # Parent is server-supplied; all actual geometry, UUID and nets must match.
    proto = type(item.proto)()
    proto.CopyFrom(item.proto)
    if "parent" in proto.DESCRIPTOR.fields_by_name:
        proto.ClearField("parent")
    return proto.SerializeToString(deterministic=True)


class BoardSafety:
    def __init__(self, board, board_path: Path, *, factory=None):
        self.board = board
        self.path = Path(board_path).resolve(strict=True)
        if self.path.suffix != ".kicad_pcb":
            raise ValidationError("Board safety requires a saved KiCad PCB.")
        self.directory = self.path.parent / ".velatrace" / "backups"
        if not self.directory.resolve().is_relative_to(self.path.parent):
            raise ValidationError("Backup directory resolves outside the board's project folder.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.factory = factory or ItemFactory()
        self.owned = {}
        self.blocked = False
        self.last_backup = None
        self._lock = threading.RLock()

    def _identity(self):
        name = Path(self.board.name)
        if not name.is_absolute():
            project = Path(self.board.document.project.path)
            if not project.is_absolute():
                raise ValidationError("Live board path is unavailable.")
            name = (project.parent if project.suffix == ".kicad_pro" else project) / name
        if name.resolve() != self.path:
            raise ValidationError("A different board is open; operation refused.")
        if self.blocked:
            raise UncertainWriteError("Previous IPC write is uncertain. Inspect KiCad and the backup/journal before restarting VelaTrace.")

    def backup(self) -> Backup:
        """Create new immutable copies before any board mutation, never overwrite."""
        self._identity()
        folder = self.directory / str(uuid.uuid4())
        folder.mkdir()
        saved = folder / "saved.kicad_pcb"
        shutil.copyfile(self.path, saved)
        with saved.open("r+b") as stream:
            os.fsync(stream.fileno())
        live = folder / "live.kicad_pcb"
        try:
            self.board.save_as(str(live), overwrite=False, include_project=True)
            read_board(live)
        except Exception as exc:
            raise CapabilityError("Cannot create the live board backup; no board mutation was attempted.") from exc
        if file_digest(saved) != file_digest(self.path):
            raise ValidationError("Saved board changed during backup; operation refused.")
        result = Backup(folder, saved, live)
        self.last_backup = result
        return result

    def _check_snapshot(self, backup, expected_digest=None):
        if expected_digest and file_digest(self.path) != expected_digest:
            raise ValidationError("Saved board changed; obtain a fresh export and approval.")
        _, saved = read_board(backup.saved)
        _, live = read_board(backup.live)
        if canonical(saved, frozenset(self.owned)) != canonical(live, frozenset(self.owned)):
            raise ValidationError("Live board has unsaved changes. Save it, then export and validate again.")
        project = self.path.with_suffix(".kicad_pro")
        if project.exists():
            live_project = backup.live.with_suffix(".kicad_pro")
            if not live_project.is_file():
                raise CapabilityError("KiCad did not include a live project backup; rule consistency cannot be verified.")
            try:
                if json.loads(project.read_text(encoding="utf-8")) != json.loads(live_project.read_text(encoding="utf-8")):
                    raise ValidationError("Live project rules differ from disk; save the project and validate again.")
            except (ValueError, OSError) as exc:
                raise ValidationError("Cannot verify the live project rule backup.") from exc
        # Do not delete an owned preview that the user changed after creation.
        self._owned_items()

    def assert_matches(self, dsn):
        with self._lock:
            dsn.assert_unchanged()
            if dsn.ticket.board_path != self.path:
                raise ValidationError("DSN belongs to a different board.")
            backup = self.backup()
            self._check_snapshot(backup, dsn.ticket.board_digest)

    def _owned_items(self):
        if not self.owned:
            return []
        current = {item.id.value: item for item in [*self.board.get_shapes(), *self.board.get_text()]}
        result = []
        for identifier, signature in self.owned.items():
            item = current.get(identifier)
            if item is None or _signature(item) != signature:
                raise ValidationError("A VelaTrace preview was edited or undone; reconcile it before continuing.")
            result.append(item)
        return result

    def _mutate(self, additions, *, remove_owned, message, expected_digest=None, temporary=False, context=None):
        with self._lock:
            if len(additions) > 100_000:
                raise ValidationError("Too many items for one safe transaction.")
            backup = self.backup()
            self._check_snapshot(backup, expected_digest)
            removals = self._owned_items() if remove_owned else []
            expected = {item.id.value: _signature(item) for item in additions}
            if len(expected) != len(additions) or "" in expected:
                raise ValidationError("Mutation item IDs must be unique and explicit.")
            _durable_json(backup.directory / "intent.json", {
                "board": str(self.path), "operation": message, "add": list(expected),
                "remove": [item.id.value for item in removals], "temporary": temporary,
                "saved_digest": file_digest(backup.saved), "status": "intent; inspect completion.json"})
            self._identity()
            if file_digest(self.path) != file_digest(backup.saved) or (context is not None and not context_matches(context)):
                raise ValidationError("Saved board/project changed before transaction.")
            commit = None
            try:
                commit = self.board.begin_commit()
                if removals:
                    self.board.remove_items(removals)
                    remaining = {item.id.value for item in [*self.board.get_shapes(), *self.board.get_text()]}
                    if remaining & {item.id.value for item in removals}:
                        raise ValidationError("KiCad did not remove every owned preview item.")
                created = self.board.create_items(additions) if additions else []
                received = {item.id.value: _signature(item) for item in created}
                if len(created) != len(additions) or received != expected:
                    raise ValidationError("KiCad did not create the complete exact route; transaction cancelled.")
                self._identity()
                if file_digest(self.path) != file_digest(backup.saved) or (context is not None and not context_matches(context)):
                    raise ValidationError("Saved board/project changed during transaction.")
            except Exception as exc:
                if commit is not None:
                    try:
                        self.board.drop_commit(commit)
                    except Exception as rollback:
                        self.blocked = True
                        raise UncertainWriteError(f"IPC rollback failed. Inspect KiCad; recovery copies: {backup.directory}") from rollback
                else:
                    # A lost begin response may leave an open transaction.
                    self.blocked = True
                    raise UncertainWriteError(f"IPC transaction startup is uncertain. Recovery copies: {backup.directory}") from exc
                raise ValidationError("Board operation failed and its IPC transaction was cancelled.") from exc
            try:
                self.board.push_commit(commit, message)
            except Exception as exc:
                self.blocked = True
                raise UncertainWriteError(f"IPC commit result is uncertain; do not retry. Inspect KiCad and backups: {backup.directory}") from exc
            if remove_owned:
                self.owned.clear()
            if temporary:
                self.owned.update(received)
            try:
                _durable_json(backup.directory / "completion.json", {"status": "committed", "owned_ids": list(self.owned)})
            except OSError as exc:
                self.blocked = True
                raise UncertainWriteError("Board commit succeeded but its recovery journal failed; inspect KiCad before continuing.") from exc

    def _preview_layer(self):
        from kipy.proto.board.board_types_pb2 import BL_User_9
        _, root = read_board(self.path)
        if not any(isinstance(row, list) and len(row) > 1 and row[1] == "User.9" for row in one_layers(root)):
            raise CapabilityError("Enable User.9 in KiCad for temporary VelaTrace graphics; no layer settings are changed automatically.")
        return BL_User_9

    def show_preview(self, dsn, plan):
        self.assert_matches(dsn)
        additions = self.factory.preview(plan, self._preview_layer())
        self._mutate(additions, remove_owned=True, message="VelaTrace routing preview", expected_digest=dsn.ticket.board_digest, temporary=True)

    def show_annotations(self, rows):
        additions = self.factory.annotations(rows, self._preview_layer())
        self._mutate(additions, remove_owned=True, message="VelaTrace audit suggestions", temporary=True)

    def clear_preview(self):
        if self.owned:
            self._mutate([], remove_owned=True, message="Remove VelaTrace temporary graphics")


def one_layers(root):
    from .sexpr import one
    return one(root, "layers")[1:]


class SafeBoardWriter:
    def __init__(self, safety: BoardSafety, validator):
        self.safety, self.validator = safety, validator

    def apply(self, dsn, plan, report):
        evidence = self.validator.evidence
        if (evidence is None or evidence[:3] != (dsn.digest, plan_digest(plan), report)
                or report.drc_violations != 0 or report.unconnected_count != 0):
            raise ValidationError("Approval requires this adapter's complete verified candidate evidence.")
        _, _, _, items, context = evidence
        if not context_matches(context):
            raise ValidationError("Project rules changed after validation; route again.")
        self.safety.assert_matches(dsn)
        additions = self.safety.factory.copper(items, self.safety.board)
        self.safety._mutate(additions, remove_owned=True, message="VelaTrace approved routing",
                            expected_digest=dsn.ticket.board_digest, context=context)
        self.validator.evidence = None
