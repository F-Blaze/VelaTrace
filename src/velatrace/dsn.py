"""Fresh human DSN export handshake and basic board correspondence checks.

Basic checks are necessary, not sufficient: candidate DRC against the real board
is required before approval because DSN pad geometry may differ from the board.
"""
from collections import Counter
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import time

from .errors import ValidationError
from .models import DesignSnapshot
from .ses import coordinate, number, resolution
from .sexpr import children, one, parse


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class ExportTicket:
    board_path: Path
    board_digest: str
    requested_ns: int

    @classmethod
    def begin(cls, board_path: Path):
        path = Path(board_path).resolve(strict=True)
        if path.suffix != ".kicad_pcb":
            raise ValidationError("Routing requires a saved KiCad PCB file.")
        return cls(path, file_digest(path), time.time_ns())


@dataclass(frozen=True)
class DsnInput:
    path: Path
    digest: str
    ticket: ExportTicket
    nets: frozenset[str]
    layers: frozenset[str]
    base_design: str = ""
    placements: dict[str, tuple[float, float, str, float]] = field(default_factory=dict)

    def assert_unchanged(self):
        if file_digest(self.path) != self.digest or file_digest(self.ticket.board_path) != self.ticket.board_digest:
            raise ValidationError("Board or DSN changed; save the board and export a fresh DSN.")


def dsn_scale(root: list) -> float:
    """DSN decimal coordinates use unit; resolution is the precision declaration.

    SES integer coordinates instead use unit/resolution. They must not be confused.
    """
    units = children(root, "unit")
    if len(units) > 1:
        raise ValidationError("Duplicate DSN coordinate unit.")
    if units:
        if len(units[0]) != 2 or units[0][1] not in {"mm", "um", "mil", "inch"}:
            raise ValidationError("Unsupported DSN coordinate unit.")
        return {"mm": 1, "um": .001, "mil": .0254, "inch": 25.4}[units[0][1]]
    res = one(root, "resolution")
    resolution(res)
    return {"mm": 1, "um": .001, "mil": .0254, "inch": 25.4}[res[1]]


def accept_export(ticket: ExportTicket, path: Path, snapshot: DesignSnapshot,
                  *, user_confirms_saved_and_exported: bool) -> DsnInput:
    path = Path(path).resolve(strict=True)
    if not user_confirms_saved_and_exported:
        raise ValidationError("Confirm the open board is saved and DSN freshly exported after this request.")
    if path.suffix.lower() != ".dsn" or path.stat().st_mtime_ns < ticket.requested_ns:
        raise ValidationError("Export a fresh .dsn after requesting routing.")
    if file_digest(ticket.board_path) != ticket.board_digest:
        raise ValidationError("Board changed after export request; start a fresh export request.")
    if not snapshot.path or snapshot.path.resolve() != ticket.board_path:
        raise ValidationError("PCB snapshot does not belong to this saved board.")
    if path.stat().st_size > 32_000_000:
        raise ValidationError("DSN input exceeds 32 MB.")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        root = parse(raw.decode("utf-8"))
    except UnicodeError:
        raise ValidationError("DSN text must use UTF-8.") from None
    if root[0] != "pcb" or len(root) < 3 or not isinstance(root[1], str):
        raise ValidationError("Invalid DSN PCB root.")
    if Path(root[1]).stem != ticket.board_path.stem:
        raise ValidationError("DSN board name does not match the current board.")
    resolution(one(root, "resolution"))
    scale = dsn_scale(root)
    structure = one(root, "structure")
    layer_rows = children(structure, "layer")
    layers = frozenset(row[1] for row in layer_rows if len(row) >= 2 and isinstance(row[1], str))
    if not snapshot.copper_layers or layers != frozenset(snapshot.copper_layers):
        raise ValidationError("DSN layers differ from the board or board stackup evidence is unavailable.")
    network = one(root, "network")
    actual_nets = {}
    for net in children(network, "net"):
        if len(net) < 2 or not isinstance(net[1], str) or net[1] in actual_nets:
            raise ValidationError("Malformed or duplicate DSN net.")
        pins = one(net, "pins")[1:]
        if any(not isinstance(pin, str) for pin in pins) or len(pins) != len(set(pins)):
            raise ValidationError("Malformed DSN pins.")
        actual_nets[net[1]] = set(pins)
    # KiCad exports a repeated pad number as "2", "2@1", "2@2"… (SOT-223 tabs,
    # connector shields). Compare pin multiplicity per net, not a deduplicated set.
    expected_nets = {net: Counter(f"{ref}-{pin}" for ref, pin in nodes)
                     for net, nodes in snapshot.connectivity().items()}
    if {net: Counter(re.sub(r"@\d+$", "", pin) for pin in pins)
            for net, pins in actual_nets.items()} != expected_nets:
        raise ValidationError("DSN pin-to-net connectivity differs from the board.")
    placements = {}
    for component in children(one(root, "placement"), "component"):
        for place in children(component, "place"):
            # KiCad always appends (PN <value>) and (lock_type position) when locked.
            if (len(place) < 6 or not isinstance(place[1], str) or place[1] in placements
                    or place[4] not in {"front", "back"}
                    or any(extra != ["lock_type", "position"] and not (
                        isinstance(extra, list) and len(extra) == 2 and extra[0] == "PN"
                        and isinstance(extra[1], str)) for extra in place[6:])):
                raise ValidationError("Unsupported or duplicate DSN placement.")
            placements[place[1]] = (coordinate(place[2], scale), coordinate(place[3], scale), place[4], number(place[5]))
    if set(placements) != {item.reference for item in snapshot.components}:
        raise ValidationError("DSN footprint list differs from the board.")
    for item in snapshot.components:
        position = (placements[item.reference][0], -placements[item.reference][1])
        if item.position_mm is None or any(abs(a-b) > .00001 for a,b in zip(position, item.position_mm)):
            raise ValidationError("DSN footprint placement differs from the board.")
    result = DsnInput(path, digest, ticket, frozenset(actual_nets), layers, root[1], placements)
    result.assert_unchanged()
    return result
