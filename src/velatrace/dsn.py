"""DSN export (automatic or manual) and basic board correspondence checks.

Basic checks are necessary, not sufficient: candidate DRC against the real board
is required before approval because DSN pad geometry may differ from the board.
"""
from collections import Counter
from dataclasses import dataclass, field
import hashlib
from pathlib import Path, PureWindowsPath
import re
import time

from .errors import ValidationError
from .models import DesignSnapshot
from .ses import MAX_PLACEMENT_RESOLUTION_MM, coordinate, number, resolution
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
    placement_resolution_mm: float | None = None
    # DSN layer name -> KiCad canonical name, for boards whose copper layers were
    # renamed (e.g. "Front" for F.Cu). The DSN and SES use the user's names; board
    # items always use canonical ones.
    layer_aliases: dict[str, str] = field(default_factory=dict)
    # DSN references of footprints with no connected pad (logos, mounting holes).
    # Freerouting leaves pad-less footprints out of the SES placement list.
    unconnected_references: frozenset[str] = frozenset()

    @property
    def board_layers(self) -> frozenset[str]:
        """The DSN's copper layers under their canonical board names."""
        return frozenset(self.layer_aliases.get(name, name) for name in self.layers)

    def assert_unchanged(self):
        if file_digest(self.path) != self.digest or file_digest(self.ticket.board_path) != self.ticket.board_digest:
            raise ValidationError("The saved board or DSN changed during routing; click Route board again.")


_LAYER_ROW = re.compile(r'\(\s*\d+\s+"([^"]+)"\s+(?:signal|power|mixed|jumper)\s+"([^"]+)"\s*\)')


def layer_aliases(board_text: str) -> dict[str, str]:
    """User copper-layer name -> canonical name, from a board's (layers ...) table.
    Refuses a user name that is another layer's canonical name: it would be ambiguous."""
    start = board_text.find("(layers")
    rows = _LAYER_ROW.findall(board_text[start:start + 20_000]) if start >= 0 else []
    aliases = {user: name for name, user in rows if user != name}
    canonical = set(re.findall(r'\(\s*\d+\s+"([^"]+\.Cu)"', board_text[start:start + 20_000])) if start >= 0 else set()
    if len(aliases) != len([1 for name, user in rows if user != name]) or set(aliases) & canonical:
        raise ValidationError("Copper layer names are ambiguous; give each copper layer a unique name.")
    return aliases


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
        raise ValidationError("The saved board changed during DSN export; click Route board again.")
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
    # KiCad names the pcb by its full export path; PureWindowsPath splits on / and \.
    if PureWindowsPath(root[1]).stem != ticket.board_path.stem:
        raise ValidationError("DSN board name does not match the current board.")
    placement_resolution_mm = resolution(one(root, "resolution"))
    if placement_resolution_mm > MAX_PLACEMENT_RESOLUTION_MM:
        raise ValidationError("DSN placement resolution is coarser than the supported KiCad precision.")
    scale = dsn_scale(root)
    structure = one(root, "structure")
    layer_rows = children(structure, "layer")
    layers = frozenset(row[1] for row in layer_rows if len(row) >= 2 and isinstance(row[1], str))
    # The saved file is digest-pinned by the ticket; DSN layers carry the user's names.
    aliases = layer_aliases(ticket.board_path.read_text(encoding="utf-8"))
    aliases = {name: aliases[name] for name in layers if name in aliases}
    canonical_layers = frozenset(aliases.get(name, name) for name in layers)
    if (not snapshot.copper_layers or len(canonical_layers) != len(layers)
            or canonical_layers != frozenset(snapshot.copper_layers)):
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
    # KiCad preserves the first literal pad number, then appends @1, @2, ...
    # to repeated occurrences in footprint pad order. Never strip a suffix:
    # a literal pad "2@1" is distinct from a generated alias for pad "2".
    expected_nets = {}
    identifiers = set()
    for component in snapshot.components:
        occurrences = Counter()
        for pin in component.pins:
            occurrence = occurrences[pin.number]
            occurrences[pin.number] += 1
            # Empty pad numbers receive @1 even on their first occurrence.
            alias = (pin.number if occurrence == 0 and pin.number else
                     f"{pin.number}@{occurrence if pin.number else occurrence + 1}")
            identifier = f"{component.reference}-{alias}"
            if identifier in identifiers:
                raise ValidationError("Ambiguous DSN pad aliases collide with literal board identifiers.")
            identifiers.add(identifier)
            if pin.net:
                expected_nets.setdefault(pin.net, set()).add(identifier)
    if actual_nets != expected_nets:
        raise ValidationError("DSN pin-to-net connectivity differs from the board.")
    placements = {}
    for component in children(one(root, "placement"), "component"):
        for place in children(component, "place"):
            # KiCad appends (PN <value>) for every footprint with a value.
            if (len(place) not in {6, 7} or not isinstance(place[1], str) or place[1] in placements
                    or place[4] not in {"front", "back"}
                    or any(not (isinstance(extra, list) and len(extra) == 2 and extra[0] == "PN"
                                and isinstance(extra[1], str)) for extra in place[6:])):
                raise ValidationError("Unsupported or duplicate DSN placement.")
            placements[place[1]] = (coordinate(place[2], scale), coordinate(place[3], scale), place[4], number(place[5]))
    # KiCad exports every footprint and renames repeated or empty references
    # (logos, mounting holes: "G***" -> "G***_1"). Uniquely named footprints must match
    # by name and position; the renamed rest must be non-electrical and match the
    # leftover DSN placements position for position.
    counts = Counter(item.reference for item in snapshot.components)
    named = [item for item in snapshot.components if item.reference and counts[item.reference] == 1]
    renamed = [item for item in snapshot.components if not item.reference or counts[item.reference] > 1]
    if (any(pin.net for item in renamed for pin in item.pins)
            or not {item.reference for item in named} <= set(placements)
            or len(placements) != len(snapshot.components)):
        raise ValidationError("DSN footprint list differs from the board.")
    def spot(x, y):
        return round(x, 4), round(y, 4)
    for item in named:
        position = (placements[item.reference][0], -placements[item.reference][1])
        if item.position_mm is None or any(abs(a-b) > .00001 for a,b in zip(position, item.position_mm)):
            raise ValidationError("DSN footprint placement differs from the board.")
    leftover = Counter(spot(place[0], -place[1]) for name, place in placements.items()
                       if name not in {item.reference for item in named})
    if any(item.position_mm is None for item in renamed) or leftover != Counter(spot(*item.position_mm) for item in renamed):
        raise ValidationError("DSN footprint placement differs from the board.")
    connected = {item.reference for item in named if any(pin.net for pin in item.pins)}
    result = DsnInput(path, digest, ticket, frozenset(actual_nets), layers, root[1], placements,
                      placement_resolution_mm, aliases, frozenset(placements) - connected)
    result.assert_unchanged()
    return result


def export_live(cli, board_text: str, snapshot: DesignSnapshot, folder: Path, cancel=None) -> DsnInput:
    """One-click export: KiCad's own exporter turns the live board text (read at click
    time, never saved over the user's file) into a DSN, which must then pass exactly the
    checks a manual export does. The ticket binds it to the saved file's digest."""
    if not snapshot.path:
        raise ValidationError("Save the board once in KiCad so VelaTrace knows its project folder.")
    ticket = ExportTicket.begin(snapshot.path)
    path = cli.export_dsn(board_text, ticket.board_path, folder, cancel)
    return accept_export(ticket, path, snapshot, user_confirms_saved_and_exported=True)
