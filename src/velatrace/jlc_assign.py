"""Write chosen LCSC part numbers to the footprints of the open board.

KiCad 10's IPC API can edit footprint fields on the board but not the schematic (kicad-python
marks its schematic module "KiCad 11"). So this writes the PCB side only, as one backed-up,
undoable commit, and the caller tells the user to run Tools > Update Schematic from PCB.
Only ever called from an explicit button click.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import re

from .bom import _LCSC_FIELDS, _field_key, _ref_key
from .candidate import canonical
from .jlc_export import csv_cell
from .errors import ValidationError
from .sexpr import parse
from .write_safety import BoardSafety, UncertainWriteError, _durable_json

FIELD = "LCSC"
COMMIT_MESSAGE = "VelaTrace: assign LCSC part numbers"
MAX_ASSIGNMENTS = 5000
_CODE = re.compile(r"C\d{1,9}")
_SUPPLIER = re.compile(r"spn|supplier")


@dataclass(frozen=True)
class AssignResult:
    changed: tuple[str, ...]      # references whose field was written
    unchanged: tuple[str, ...]    # already carried that part number
    backup: Path | None           # recovery copies made before the write

    @property
    def summary(self) -> str:
        if not self.changed:
            return "Nothing to write: the board already carries these part numbers."
        return (f"Wrote the {FIELD} field of {len(self.changed)} footprint(s) on the board "
                "(one Undo step in KiCad). Now run Tools > Update Schematic from PCB with "
                "'Other fields' ticked so the schematic gets the same numbers, then save.")


def _checked(assignments: dict[str, str]) -> dict[str, str]:
    result = {}
    for reference, code in assignments.items():
        code = str(code).strip().upper()
        if not isinstance(reference, str) or not reference or not _CODE.fullmatch(code):
            raise ValidationError(f"'{code}' is not an LCSC part number (C followed by digits).")
        result[reference] = code
    if not result or len(result) > MAX_ASSIGNMENTS:
        raise ValidationError("Choose between 1 and 5000 parts to assign.")
    return result


def _lcsc_fields(footprint):
    """Fields VelaTrace reads a part number from, in the order it reads them."""
    from kipy.board_types import Field
    return [item for item in footprint.definition.items
            if isinstance(item, Field) and _field_key(item.name) in _LCSC_FIELDS]


def _holding(fields):
    return [field for field in fields if _CODE.fullmatch(field.text.value.strip().upper())]


def _targets(fields):
    """The fields to overwrite: every one a part number is read from today, else an empty
    LCSC/JLC field. Generic supplier fields are only reused when they already hold a number."""
    return _holding(fields) or [field for field in fields
                                if not _SUPPLIER.search(_field_key(field.name))][:1]


def _other_content(text: str, references: set[str]):
    """The board with the part-number fields of these footprints removed, for comparison."""
    root = parse(text, kicad=True)
    for index, node in enumerate(root):
        if not isinstance(node, list) or not node or node[0] != "footprint":
            continue
        reference = next((str(child[2]) for child in node if isinstance(child, list)
                          and len(child) > 2 and child[:2] == ["property", "Reference"]), "")
        if reference in references:
            root[index] = [child for child in node if not (
                isinstance(child, list) and len(child) > 2 and child[0] == "property"
                and _field_key(str(child[1])) in _LCSC_FIELDS)]
    return canonical(root)


def assign_lcsc(safety: BoardSafety, assignments: dict[str, str]) -> AssignResult:
    """One backed-up IPC commit that sets the LCSC field of the given references.

    Refuses before touching anything when a reference is missing or duplicated. After the
    commit the live board is compared with the backup: only those fields may differ."""
    from kipy.board_types import BoardLayer, Field

    wanted = _checked(assignments)
    board = safety.board
    with safety._lock:
        backup = safety.backup()  # also refuses a different board or an uncertain earlier write
        before = backup.live.read_text(encoding="utf-8")
        by_reference: dict[str, list] = {}
        for footprint in board.get_footprints():
            by_reference.setdefault(footprint.reference_field.text.value, []).append(footprint)
        problems = [ref for ref in wanted if len(by_reference.get(ref, ())) != 1]
        if problems:
            raise ValidationError("Not on the board exactly once: " + ", ".join(
                sorted(problems, key=_ref_key)[:10]) + ". Nothing was written.")
        updates, changed, unchanged = [], [], []
        for reference, code in wanted.items():
            footprint = by_reference[reference][0]
            targets = _targets(_lcsc_fields(footprint))
            if targets and all(field.text.value.strip().upper() == code for field in targets):
                unchanged.append(reference)
                continue
            if not targets:
                target = Field()
                target.name = FIELD
                target.visible = False
                front = BoardLayer.Name(footprint.layer).startswith("BL_F")
                target.layer = BoardLayer.BL_F_Fab if front else BoardLayer.BL_B_Fab
                target.text.position = footprint.position
                footprint.definition.add_item(target)
                targets = [target]
            for target in targets:
                target.text.value = code
            updates.append(footprint)
            changed.append(reference)
        def order(refs):
            return tuple(sorted(refs, key=_ref_key))
        if not updates:
            return AssignResult((), order(unchanged), None)
        record = {"board": str(safety.path), "operation": COMMIT_MESSAGE,
                  "assign": {ref: wanted[ref] for ref in changed}}
        _durable_json(backup.directory / "intent.json", {**record, "status": "intent; inspect completion.json"})
        commit = None
        try:
            commit = board.begin_commit()
            if len(board.update_items(updates)) != len(updates):
                raise ValidationError("KiCad did not update every footprint.")
        except Exception as exc:
            if commit is None:
                safety.blocked = True
                raise UncertainWriteError(f"IPC transaction startup is uncertain. Recovery copies: {backup.directory}") from exc
            try:
                board.drop_commit(commit)
            except Exception as rollback:
                safety.blocked = True
                raise UncertainWriteError(f"IPC rollback failed. Inspect KiCad; recovery copies: {backup.directory}") from rollback
            raise ValidationError("KiCad refused the field update; nothing was changed.") from exc
        try:
            board.push_commit(commit, COMMIT_MESSAGE)
        except Exception as exc:
            safety.blocked = True
            raise UncertainWriteError(f"IPC commit result is uncertain; do not retry. Inspect KiCad and backups: {backup.directory}") from exc
        try:
            after = board.get_as_string()
            written = {}
            for footprint in board.get_footprints():
                reference = footprint.reference_field.text.value
                if reference in wanted:
                    written[reference] = {f.text.value for f in _holding(_lcsc_fields(footprint))}
            if (any(written.get(ref) != {wanted[ref]} for ref in changed)
                    or _other_content(after, set(changed)) != _other_content(before, set(changed))):
                raise ValidationError("The board differs from the intended change.")
        except Exception as exc:
            safety.blocked = True
            raise UncertainWriteError(
                "KiCad accepted the change but the result could not be verified. Press Undo "
                f"(Ctrl+Z) in KiCad and inspect the backup: {backup.directory}") from exc
        _durable_json(backup.directory / "completion.json", {**record, "status": "committed"})
        return AssignResult(order(changed), order(unchanged), backup.directory)


def export_assignments(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """Fallback and schematic-side helper: Reference, Value, Footprint, LCSC as CSV."""
    with Path(path).open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("Reference", "Value", "Footprint", FIELD))
        writer.writerows([csv_cell(cell) for cell in row]
                         for row in sorted(rows, key=lambda row: _ref_key(row[0])))
