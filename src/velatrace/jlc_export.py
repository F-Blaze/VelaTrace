"""JLCPCB fabrication files: BOM, CPL (pick-and-place) and a Gerber/drill zip.

Offline. KiCad's own kicad-cli does the plotting on a temporary copy of the board, so the
user's project folder is never written by KiCad; only the output folder the user chose
receives files. Column layouts and plot settings follow JLCPCB's help pages (docs/jlcpcb.md).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
import io
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from .bom import _ref_key, lcsc_code
from .errors import ValidationError
from .models import Component
from .sexpr import parse

BOM_HEADER = ("Comment", "Designator", "Footprint", "LCSC Part #")
CPL_HEADER = ("Designator", "Mid X", "Mid Y", "Layer", "Rotation")
ROTATIONS_FILE = "jlc-rotations.csv"
# Degrees added to KiCad's rotation because JLCPCB's reel orientation differs from KiCad's
# footprint zero for these package families. Deliberately short: only families whose angle
# was cross-checked on 2026-10-04 against the community corrections list (which is GPL-3.0
# and therefore not bundled; these are the plain angles, i.e. facts). A starting point only:
# always check the placement preview JLCPCB shows before paying. Extend or override it with
# jlc-rotations.csv.
DEFAULT_ROTATIONS: tuple[tuple[str, int], ...] = (
    (r"^SOT-223", 180), (r"^SOT-23", 270), (r"^SOT-89", 180), (r"^SOT-353", 180),
    (r"^SOT-363", 180), (r"^SOIC-", 270), (r"^SSOP-", 270), (r"^TSSOP-", 270),
    (r"^LQFP-", 270), (r"^TQFP-", 270), (r"^DFN-", 270), (r"^CP_EIA-", 180),
    (r"^R_Array_Convex_", 90), (r"^R_Array_Concave_", 90),
)
_SILK = {"F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"}
_PLOT = {"F.Paste", "B.Paste", "F.Mask", "B.Mask", "Edge.Cuts"} | _SILK


@dataclass(frozen=True)
class BoardPart:
    reference: str
    value: str
    footprint: str            # library item name without the library nickname
    side: str                 # "top" | "bottom"
    lcsc: str = ""
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False


@dataclass
class ExportResult:
    folder: Path
    files: list[Path] = field(default_factory=list)
    bom_lines: int = 0
    placements: int = 0
    missing_lcsc: list[str] = field(default_factory=list)   # placed parts without a part number
    rotated: list[str] = field(default_factory=list)        # "U1 +270° (^SOIC-)"
    skipped: list[str] = field(default_factory=list)        # DNP / excluded references
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        text = (f"Wrote {', '.join(path.name for path in self.files)} to {self.folder}. "
                f"{self.bom_lines} BOM line(s), {self.placements} placement(s)")
        if self.rotated:
            text += f", {len(self.rotated)} rotation correction(s)"
        if self.skipped:
            text += f", {len(self.skipped)} part(s) left out (DNP/excluded)"
        if self.missing_lcsc:
            text += (f". {len(self.missing_lcsc)} placed part(s) have no LCSC number: "
                     + ", ".join(self.missing_lcsc[:8]) + (" …" if len(self.missing_lcsc) > 8 else ""))
        return text + ". " + " ".join(self.notes)


def board_parts(board_text: str) -> tuple[list[BoardPart], list[str]]:
    """Footprints of a .kicad_pcb with the attributes the snapshot does not carry, and the
    board's layer names."""
    root = parse(board_text, kicad=True)
    if not root or root[0] != "kicad_pcb":
        raise ValidationError("Expected a saved KiCad PCB.")
    parts, layers = [], []
    for node in root[1:]:
        if not isinstance(node, list) or not node:
            continue
        if node[0] == "layers":
            layers = [str(item[1]) for item in node[1:] if isinstance(item, list) and len(item) > 1]
        if node[0] != "footprint" or len(node) < 2:
            continue
        fields, attrs, layer = {}, set(), "F.Cu"
        for child in node[2:]:
            if not isinstance(child, list) or not child:
                continue
            if child[0] == "property" and len(child) > 2 and isinstance(child[2], str):
                fields[str(child[1])] = str(child[2])
            elif child[0] == "fp_text" and len(child) > 2 and child[1] in ("reference", "value"):
                fields.setdefault(str(child[1]).capitalize(), str(child[2]))  # KiCad 7 and older
            elif child[0] == "attr":
                attrs = {str(item) for item in child[1:]}
            elif child[0] == "layer" and len(child) > 1:
                layer = str(child[1])
        reference = fields.get("Reference", "")
        if not reference:
            continue
        component = Component(reference, fields.get("Value", ""), str(node[1]), fields=fields)
        parts.append(BoardPart(
            reference, component.value.strip(), str(node[1]).rsplit(":", 1)[-1],
            "bottom" if layer.startswith("B.") else "top", lcsc_code(component) or "",
            "dnp" in attrs,
            "exclude_from_bom" in attrs, "exclude_from_pos_files" in attrs))
    return parts, layers


def load_rotations(*folders: Path) -> list[tuple[str, re.Pattern | None, int]]:
    """User rows first (they win), then the built-in table. A row is `pattern,degrees`: the
    pattern is an LCSC number (C123456) or a regular expression matched at the start of the
    footprint name. The community cpl_rotations_db.csv has the same two columns."""
    rows: list[tuple[str, int]] = []
    for folder in folders:
        path = Path(folder) / ROTATIONS_FILE
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        for row in csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))):
            if len(row) >= 2 and re.fullmatch(r"\s*-?\d+(\.0*)?\s*", row[1]) and len(row[0]) <= 200:
                rows.append((row[0].strip(), int(float(row[1])) % 360))
    result = []
    for pattern, degrees in rows + list(DEFAULT_ROTATIONS):
        if re.fullmatch(r"C\d{1,9}", pattern):
            result.append((pattern, None, degrees))
            continue
        try:
            result.append((pattern, re.compile(pattern), degrees))
        except re.error:
            continue  # a bad user pattern never blocks the export
    return result


def jlc_rotation(kicad_degrees: float, side: str, footprint: str, lcsc: str,
                 rotations) -> tuple[float, str]:
    """KiCad orientation -> JLCPCB CPL rotation. Returns (degrees, applied rule or "")."""
    rotation = kicad_degrees % 360
    if side == "bottom":
        rotation = (180 - rotation) % 360  # seen from the bottom the angle is mirrored
    for pattern, regex, degrees in rotations:
        if (lcsc and pattern == lcsc) if regex is None else regex.match(footprint):
            return (rotation + degrees) % 360, f"+{degrees}° ({pattern})"
    return rotation, ""


def bom_rows(parts: list[BoardPart]) -> list[tuple[str, str, str, str]]:
    groups: dict[tuple, list[str]] = {}
    for part in parts:
        if not (part.dnp or part.exclude_from_bom):
            groups.setdefault((part.lcsc, part.value, part.footprint), []).append(part.reference)
    rows = [(value, ",".join(sorted(refs, key=_ref_key)), footprint, code)
            for (code, value, footprint), refs in groups.items()]
    return sorted(rows, key=lambda row: _ref_key(row[1].split(",")[0]))


def cpl_rows(position_csv: str, parts: list[BoardPart], rotations) -> tuple[list[tuple], list[str]]:
    """kicad-cli's position CSV (Ref,Val,Package,PosX,PosY,Rot,Side) -> JLCPCB CPL rows."""
    by_ref = {part.reference: part for part in parts}
    rows, rotated = [], []
    for row in csv.DictReader(io.StringIO(position_csv)):
        part = by_ref.get(row.get("Ref", ""))
        if part is None or part.dnp or part.exclude_from_pos:
            continue
        try:
            x, y, kicad = float(row["PosX"]), float(row["PosY"]), float(row["Rot"])
        except (KeyError, ValueError) as exc:
            raise ValidationError("kicad-cli wrote a position file in an unexpected format.") from exc
        side = "bottom" if row.get("Side", "").strip().lower() in ("bottom", "back") else "top"
        rotation, rule = jlc_rotation(kicad, side, part.footprint, part.lcsc, rotations)
        if rule:
            rotated.append(f"{part.reference} {rule}")
        rows.append((part.reference, f"{x:.4f}mm", f"{y:.4f}mm", side.capitalize(), f"{rotation:g}"))
    return sorted(rows, key=lambda row: _ref_key(row[0])), rotated


def _write_csv(path: Path, header, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(header)
        writer.writerows(rows)


def export_fabrication(cli, board_path: Path, out_dir: Path, *, board_text: str | None = None,
                       rotation_folders: tuple[Path, ...] = (), gerbers: bool = True) -> ExportResult:
    """Write BOM-<name>.csv, CPL-<name>.csv and GERBER-<name>.zip into out_dir.

    board_text: the live board from the editor; the saved file is used when it is None.
    Existing files with those names are replaced; nothing else is touched."""
    board_path, out_dir = Path(board_path), Path(out_dir)
    text = board_text if board_text is not None else board_path.read_text(encoding="utf-8")
    if len(text) > 32_000_000:
        raise ValidationError("Board exceeds the 32 MB safety limit.")
    parts, layers = board_parts(text)
    name = board_path.stem
    result = ExportResult(out_dir)
    result.skipped = sorted((p.reference for p in parts if p.dnp or p.exclude_from_bom
                             or p.exclude_from_pos), key=_ref_key)
    rotations = load_rotations(*rotation_folders)
    work = Path(tempfile.mkdtemp(prefix="velatrace-jlc-"))
    try:
        board = work / board_path.name
        board.write_text(text, encoding="utf-8")
        project = board_path.with_suffix(".kicad_pro")
        if project.is_file():
            shutil.copyfile(project, work / project.name)
        home = cli._export_config_home()
        cli._run(["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
                  "--exclude-dnp", "--output", "positions.csv", board.name], work, config_home=home)
        placements, result.rotated = cpl_rows(
            (work / "positions.csv").read_text(encoding="utf-8-sig"), parts, rotations)
        rows = bom_rows(parts)
        placed = {row[0] for row in placements}
        result.missing_lcsc = sorted((p.reference for p in parts if p.reference in placed
                                      and not p.lcsc and not p.exclude_from_bom), key=_ref_key)
        staged = [(work / f"BOM-{name}.csv", BOM_HEADER, rows),
                  (work / f"CPL-{name}.csv", CPL_HEADER, placements)]
        for path, header, content in staged:
            _write_csv(path, header, content)
        outputs = [path for path, _, _ in staged]
        if gerbers:
            plot = [layer for layer in layers if layer.endswith(".Cu") or layer in _PLOT]
            if "Edge.Cuts" not in plot or not any(layer.endswith(".Cu") for layer in plot):
                raise ValidationError("The board has no copper or Edge.Cuts layer to plot.")
            fab = work / "gerber"
            fab.mkdir()
            # JLCPCB's KiCad guide: Protel extensions (kicad-cli default), no X2, subtract mask
            # from silkscreen, zones checked; drill: Excellon, mm, decimal, alternate oval mode.
            cli._run(["pcb", "export", "gerbers", "--output", "gerber/", "--layers", ",".join(plot),
                      "--no-x2", "--no-netlist", "--subtract-soldermask", "--check-zones",
                      board.name], work, config_home=home)
            cli._run(["pcb", "export", "drill", "--output", "gerber/", "--format", "excellon",
                      "--drill-origin", "absolute", "--excellon-zeros-format", "decimal",
                      "--excellon-oval-format", "alternate", "--excellon-units", "mm",
                      "--excellon-separate-th", board.name], work, config_home=home)
            plotted = sorted(path for path in fab.iterdir()
                             if path.is_file() and path.suffix != ".gbrjob")
            if len(plotted) <= len(plot):  # every layer plus at least one drill file
                raise ValidationError("kicad-cli did not plot every layer; no files were written.")
            archive = work / f"GERBER-{name}.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in plotted:
                    bundle.write(path, path.name)
            outputs.append(archive)
            result.notes.append(f"Gerbers: {len(plot)} layers plus drill files.")
        out_dir.mkdir(parents=True, exist_ok=True)
        for path in outputs:  # only after everything succeeded
            shutil.copyfile(path, out_dir / path.name)
            result.files.append(out_dir / path.name)
        result.bom_lines, result.placements = len(rows), len(placements)
        if board_text is None:
            result.notes.append("Made from the saved board file.")
        result.notes.append("Check rotations in JLCPCB's placement preview before ordering.")
        return result
    except OSError as exc:
        raise ValidationError(f"Could not write the JLCPCB files: {exc.strerror or exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
