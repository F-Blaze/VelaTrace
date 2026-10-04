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

from .candidate import canonical, context_matches, file_digest, live_board_text, read_board
from .sexpr import parse
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
    def delete(board, items) -> bool:
        """KiCad accepted the deletion. Reads inside an open commit still show pending
        removals and KiCad 10.0.6 returns no per-item results, so _mutate re-reads
        after the commit to prove the items are gone."""
        from kipy.proto.common.commands.editor_commands_pb2 import (
            IDS_OK, DeleteItems, DeleteItemsResponse)
        from kipy.proto.common.types.base_types_pb2 import IRS_OK
        command = DeleteItems()
        command.header.document.CopyFrom(board.document)
        command.item_ids.extend(item.id for item in items)
        response = board.client.send(command, DeleteItemsResponse)
        return response.status == IRS_OK and all(result.status == IDS_OK for result in response.deleted_items)

    @staticmethod
    def preview(plan, layer):
        from kipy.board_types import BoardSegment
        from kipy.geometry import Vector2
        from kipy.proto.common.types.enums_pb2 import SLS_DASH
        output, drawn = [], set()
        for track in plan.tracks:
            for start, end in zip(track.points_mm, track.points_mm[1:]):
                # One preview layer: same-net copper stacked on several copper layers
                # draws one line. Coincident lines of different nets are still drawn
                # twice, so the collision check refuses them.
                key = (track.net, frozenset((start, end)))
                if key in drawn:
                    continue
                drawn.add(key)
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


def _segment_key(item):
    from kipy.board_types import BoardShape
    if not isinstance(item, BoardShape) or item.proto.shape.WhichOneof("geometry") != "segment":
        return None
    segment = item.proto.shape.segment
    ends = sorted(((segment.start.x_nm, segment.start.y_nm),
                   (segment.end.x_nm, segment.end.y_nm)))
    return (item.proto.layer, *ends)


def _describe_lines(keys, limit=5):
    """KiCad-coordinate list of up to `limit` segment keys, for the user to find them."""
    keys = sorted(keys)
    text = "; ".join(f"({a[0]/1e6:g}, {a[1]/1e6:g})-({b[0]/1e6:g}, {b[1]/1e6:g}) mm" for _, a, b in keys[:limit])
    return text + (f"; and {len(keys) - limit} more" if len(keys) > limit else "")


def _preview_like(item, layer):
    """A dashed 0.1 mm segment on the preview layer: what ItemFactory.preview draws."""
    from kipy.proto.common.types.enums_pb2 import SLS_DASH
    key = _segment_key(item)
    stroke = item.proto.shape.attributes.stroke if key else None
    return bool(key and key[0] == layer and stroke.style == SLS_DASH and stroke.width.value_nm == 100_000)


def _check_graphic_collisions(additions, existing, removed_ids, layer="User.9"):
    """KiCad may replace an existing coincident graphic when creating a segment."""
    foreign = {_segment_key(item) for item in existing if item.id.value not in removed_ids}
    foreign.discard(None)
    seen = set()
    hits = set()
    for item in additions:
        value = _segment_key(item)
        if value is None:
            continue
        if value in foreign:
            hits.add(value)
        if value in seen:
            raise ValidationError("Preview contains duplicate segments that KiCad may merge; no preview was written.")
        seen.add(value)
    if hits:
        raise ValidationError(f"Preview would overlap {len(hits)} {layer} line(s) that VelaTrace did not create "
                              f"and cannot prove are its own: {_describe_lines(hits)}. KiCad may replace them, so "
                              "no preview was written. Delete them in KiCad if they are old previews, or move "
                              f"your own drawings off {layer}, then retry.")


def _echoes(sent, got) -> bool:
    """Every field VelaTrace set comes back exactly; KiCad-filled defaults are allowed.

    Live KiCad returns created items with defaults filled in (text alignment, fill,
    padstack shape) and KiCad 10 identifies nets by name only, so byte equality never
    holds. Net codes are server-internal; the net name is compared.
    """
    from google.protobuf.json_format import MessageToDict
    from kipy.board_types import BoardShape, BoardText, Track, Via
    from kipy.proto.board.board_types_pb2 import DS_UNDEFINED, PSS_CIRCLE

    def geometry(item):
        proto = item.proto
        common = (proto.locked,)
        if isinstance(item, Track):
            return common + (proto.start, proto.end, proto.width, proto.layer, proto.net.name)
        if isinstance(item, Via):
            stack = proto.pad_stack
            # KiCad supplies these two shape defaults for the factory's circular
            # through-vias. All coordinates, dimensions and other shape values
            # remain exact, including fields whose scalar value is zero.
            layers = tuple((row.layer, row.size, row.shape or PSS_CIRCLE,
                            row.custom_anchor_shape or PSS_CIRCLE,
                            row.offset.x_nm, row.offset.y_nm, row.trapezoid_delta,
                            row.corner_rounding_ratio, row.chamfer_ratio,
                            row.chamfered_corners, tuple(row.custom_shapes))
                           for row in stack.copper_layers)
            drill = stack.drill
            return common + (proto.position, proto.type, proto.net.name, stack.type,
                             drill.start_layer, drill.end_layer, drill.diameter,
                             drill.shape or DS_UNDEFINED, layers, stack.angle.value_degrees,
                             stack.secondary_drill, stack.tertiary_drill,
                             stack.front_post_machining, stack.back_post_machining)
        # The SDK unwraps created graphics as BoardShape, even when the request
        # used BoardSegment. Require the same concrete protobuf geometry.
        if isinstance(item, BoardShape) and proto.shape.WhichOneof("geometry") == "segment":
            return common + (proto.shape.segment, proto.layer, proto.net.name)
        if isinstance(item, BoardText):
            return common + (proto.text.position, proto.text.attributes.angle.value_degrees,
                             proto.text.attributes.size, proto.text.attributes.stroke_width,
                             proto.layer, proto.knockout)
        return None

    if type(sent.proto) is not type(got.proto) or geometry(sent) is None or geometry(sent) != geometry(got):
        return False

    def plain(item):
        data = MessageToDict(item.proto)
        data.pop("parent", None)
        if isinstance(data.get("net"), dict):
            data["net"].pop("code", None)
        return data

    def subset(a, b):
        if isinstance(a, dict):
            return isinstance(b, dict) and all(key in b and subset(value, b[key]) for key, value in a.items())
        if isinstance(a, list):
            return isinstance(b, list) and len(a) == len(b) and all(map(subset, a, b))
        return a == b
    # Geometry is compared above as protobuf values, because JSON omits zero
    # coordinates. Subset comparison here permits KiCad's display defaults.
    return subset(plain(sent), plain(got))


PREVIEW_LAYERS = ("User.9", "User.8", "User.7", "User.6", "User.5", "User.4", "User.3", "User.2", "User.1",
                  "Eco2.User", "Eco1.User", "Cmts.User", "Dwgs.User")


class BoardSafety:
    preview_layer_name = "User.9"

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
            # Never ask KiCad to save a copy (SaveCopyOfDocument): it rewrites the real
            # project file on every call, and KiCad 10.0.4's project manager crashed in
            # _eeschema.dll after repeated copies. The in-memory board text writes nothing.
            with live.open("x", encoding="utf-8") as stream:
                stream.write(self.board.get_as_string())
                stream.flush()
                os.fsync(stream.fileno())
            read_board(live)
        except Exception as exc:
            raise CapabilityError("Cannot create the live board backup; no board mutation was attempted.") from exc
        project = self.path.with_suffix(".kicad_pro")
        if project.is_file():
            shutil.copyfile(project, folder / "saved.kicad_pro")
        if file_digest(saved) != file_digest(self.path):
            raise ValidationError("Saved board changed during backup; operation refused.")
        result = Backup(folder, saved, live)
        self.last_backup = result
        return result

    def _check_snapshot(self, backup, expected_digest=None, expected_board=None):
        if expected_digest and file_digest(self.path) != expected_digest:
            raise ValidationError("Saved board changed; obtain a fresh export and approval.")
        return self._check_live_source(backup.live.read_text(encoding="utf-8"), expected_board)

    def _check_live_source(self, text, expected_board=None):
        self._owned_items()
        source = live_board_text(text, frozenset(self.owned))
        if expected_board is not None and canonical(parse(source, kicad=True)) != expected_board:
            raise ValidationError("The board changed after routing validation. Reject the preview and validate a new route; no save is required to approve an unchanged preview.")
        # Unsaved Board Setup changes cannot be read without KiCad rewriting the project
        # (see backup). Validation uses the saved project, and its digest is re-checked
        # around every commit via the candidate context.
        return source

    def assert_matches(self, dsn, *, expected_board=None):
        with self._lock:
            dsn.assert_unchanged()
            if dsn.ticket.board_path != self.path:
                raise ValidationError("DSN belongs to a different board.")
            backup = self.backup()
            return self._check_snapshot(backup, dsn.ticket.board_digest, expected_board)

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

    def _mutate(self, additions, *, remove_owned, message, expected_digest=None, temporary=False, context=None, expected_board=None, verify_copper=False, record=None):
        """record: extra audit fields journaled in intent.json and completion.json."""
        with self._lock:
            if len(additions) > 100_000:
                raise ValidationError("Too many items for one safe transaction.")
            backup = self.backup()
            self._check_snapshot(backup, expected_digest, expected_board)
            removals = self._owned_items() if remove_owned else []
            if temporary:
                _check_graphic_collisions(additions, self.board.get_shapes(), {item.id.value for item in removals},
                                          self.preview_layer_name)
            expected = {item.id.value: _signature(item) for item in additions}
            if len(expected) != len(additions) or "" in expected:
                raise ValidationError("Mutation item IDs must be unique and explicit.")
            _durable_json(backup.directory / "intent.json", {
                "board": str(self.path), "operation": message, "add": list(expected),
                "remove": [item.id.value for item in removals], "temporary": temporary,
                "saved_digest": file_digest(backup.saved), "status": "intent; inspect completion.json",
                **(record or {})})
            self._identity()
            if file_digest(self.path) != file_digest(backup.saved) or (context is not None and not context_matches(context)):
                raise ValidationError("Saved board/project changed before transaction.")
            commit = None
            try:
                commit = self.board.begin_commit()
                if expected_board is not None:
                    # Close the interval between the backup/journal and transaction.
                    # Read before adding/removing anything, so our own writes cannot
                    # hide an intervening editor change.
                    self._check_live_source(self.board.get_as_string(), expected_board)
                if removals and not self.factory.delete(self.board, removals):
                    raise ValidationError("KiCad did not remove every owned preview item.")
                created = self.board.create_items(additions) if additions else []
                # Owned signatures are KiCad's own echo, so later edit detection compares like with like.
                received = {item.id.value: _signature(item) for item in created}
                by_id = {item.id.value: item for item in created}
                if (len(created) != len(additions) or received.keys() != expected.keys()
                        or not all(_echoes(item, by_id[item.id.value]) for item in additions)):
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
            if verify_copper:
                try:
                    committed = {item.id.value: item for item in [*self.board.get_tracks(), *self.board.get_vias()]}
                    if not all(item.id.value in committed and _echoes(item, committed[item.id.value])
                               for item in additions):
                        raise ValidationError("The committed copper does not match the approved route.")
                except Exception as exc:
                    self.blocked = True
                    raise UncertainWriteError(f"KiCad acknowledged approval, but the actual copper could not be verified. Do not retry; inspect KiCad and backups: {backup.directory}") from exc
            removed = {item.id.value for item in removals}
            if removed:
                try:
                    remaining = {item.id.value for item in [*self.board.get_shapes(), *self.board.get_text()]}
                    if removed & remaining:
                        raise ValidationError("KiCad still shows VelaTrace preview items.")
                except Exception as exc:
                    self.blocked = True
                    raise UncertainWriteError(f"Committed, but preview removal could not be verified. Use Undo in KiCad and inspect backups: {backup.directory}") from exc
            if remove_owned:
                self.owned.clear()
            if temporary:
                self.owned.update(received)
            try:
                _durable_json(backup.directory / "completion.json",
                              {"status": "committed", "owned_ids": list(self.owned), **(record or {})})
            except OSError as exc:
                self.blocked = True
                raise UncertainWriteError("Board commit succeeded but its recovery journal failed; inspect KiCad before continuing.") from exc

    def _preview_layer(self):
        """User.9 when the board has it, else the first enabled spare drawing layer:
        a new KiCad board has no User.9, and VelaTrace never changes layer settings.
        Only VelaTrace's own journaled graphics are ever removed from that layer."""
        from kipy.proto.board import board_types_pb2
        _, root = read_board(self.path)
        enabled = {row[1] for row in one_layers(root) if isinstance(row, list) and len(row) > 1}
        for name in PREVIEW_LAYERS:
            if name in enabled:
                self.preview_layer_name = name
                return getattr(board_types_pb2, "BL_" + name.replace(".", "_"))
        raise CapabilityError("Temporary VelaTrace graphics need a user drawing layer (User.1-User.9, User.Eco1/2, "
                              "User.Comments or User.Drawings). Enable one in File > Board Setup > Board Editor "
                              "Layers, then save the board. VelaTrace never changes layer settings itself.")

    def _journaled_ids(self):
        """Temporary-graphic UUIDs committed by any VelaTrace run on this board (completion.json)."""
        ids = set()
        for path in self.directory.glob("*/completion.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") == "committed":
                    ids.update(value for value in data.get("owned_ids", ()) if isinstance(value, str))
            except (OSError, ValueError, AttributeError, TypeError):
                continue
        return ids

    def prepare_preview(self):
        """Before routing, fail fast on everything that would refuse the later preview.

        Old VelaTrace previews (saved into the board, restored by Undo, or left by an
        earlier run) are recognised by journaled UUID on User.9 and removed through the
        normal backed-up transaction. Unjournaled look-alikes are never deleted.
        """
        layer = self._preview_layer()
        with self._lock:
            self._identity()
            journaled = self._journaled_ids()
            for item in [*self.board.get_shapes(), *self.board.get_text()]:
                if item.id.value in journaled and item.proto.layer == layer:
                    self.owned.setdefault(item.id.value, _signature(item))
            self.clear_preview()
            # ponytail: pre-route we cannot know the route, so refuse on preview-styled
            # foreign lines (the only realistic exact overlap); the post-route check stays.
            orphans = {_segment_key(item) for item in self.board.get_shapes() if _preview_like(item, layer)}
        if orphans:
            raise ValidationError(f"{self.preview_layer_name} has {len(orphans)} dashed 0.1 mm line(s) that look like old VelaTrace "
                                  f"previews, but no VelaTrace journal beside this board proves it created them, so "
                                  f"they were left untouched: {_describe_lines(orphans)}. Delete them in KiCad if they "
                                  f"are old previews, or move your own drawings off {self.preview_layer_name}, then retry. Routing was not started.")

    def show_preview(self, dsn, plan, expected_board=None):
        """Draw the preview; returns the live-board snapshot it was drawn against.

        Without expected_board (preview before DRC) the current live board is the
        snapshot; the caller must require that validation later saw the same board."""
        source = self.assert_matches(dsn, expected_board=expected_board)
        snapshot = expected_board if expected_board is not None else canonical(parse(source, kicad=True))
        additions = self.factory.preview(plan, self._preview_layer())
        self._mutate(additions, remove_owned=True, message="VelaTrace routing preview", expected_digest=dsn.ticket.board_digest, temporary=True, expected_board=snapshot)
        return snapshot

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

    def apply(self, dsn, plan, report, *, drc_override=False):
        """drc_override (the user's explicit 'Approve anyway') lifts only the gate on
        known DRC violations; they are journaled with the commit as an audit trail."""
        evidence = self.validator.evidence
        if (evidence is None or evidence[:3] != (dsn.digest, plan_digest(plan), report)
                or report.drc_violations is None or report.unconnected_count != 0
                or (report.drc_violations and not drc_override)):
            raise ValidationError("Approval requires this adapter's complete verified candidate evidence.")
        record = {"drc_override": {"violations": report.drc_violations,
                                   "blocking_reasons": list(report.blocking_reasons)}} if report.drc_violations else None
        _, _, _, items, context, snapshot = evidence
        if not context_matches(context):
            raise ValidationError("Project rules changed after validation; route again.")
        self.safety.assert_matches(dsn, expected_board=snapshot)
        additions = self.safety.factory.copper(items, self.safety.board)
        self.safety._mutate(additions, remove_owned=True, message="VelaTrace approved routing",
                            expected_digest=dsn.ticket.board_digest, context=context, expected_board=snapshot,
                            verify_copper=True, record=record)
        self.validator.evidence = None
