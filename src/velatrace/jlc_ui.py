"""The JLCPCB window: parts table, catalogue download, live stock, assignment, fabrication files.

Every network or board-changing action here starts from its own button and says what it will
do before it does it. Opening the window is offline and changes nothing.
"""
from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import jlc_assign, jlc_catalog, jlc_export, jlc_live, jlc_parts
from .bom import JLC_EXTENDED_FEE_USD, _ref_list, format_value, lcsc_code, parse_value, passive_kind
from .errors import VelaTraceError
from .kicad_cli import KiCadCli
from .models import Component

COLUMNS = ("Parts", "Qty", "Value", "Package", "LCSC", "Tier", "Stock", "Unit $", "Ext. fee", "Data")
TIER_COLORS = {"basic": "#0E9F6E", "preferred": "#0E9F6E", "extended": "#D97706", "unlisted": "#DC2626"}
ACCENT = "#8B5CF6"


def _confirm(parent, title: str, text: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return box.exec() == QMessageBox.StandardButton.Ok


def _table(headers) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().hide()
    table.horizontalHeader().setStretchLastSection(True)
    # Neutral translucent header and grid: readable on both the light and the dark theme.
    table.setStyleSheet("QHeaderView::section {background:rgba(127,127,127,45); border:0; padding:4px; "
                        "font-weight:600} QTableWidget {gridline-color:rgba(127,127,127,60)}")
    return table


def _fill(table: QTableWidget, rows, colors=()) -> None:
    table.setRowCount(len(rows))
    for y, row in enumerate(rows):
        for x, text in enumerate(row):
            item = QTableWidgetItem(str(text))  # plain text: catalogue strings are untrusted
            if y < len(colors) and x in colors[y]:
                item.setForeground(QColor(colors[y][x]))
            table.setItem(y, x, item)
    table.resizeColumnsToContents()
    for column in range(table.columnCount() - 1):
        table.setColumnWidth(column, min(table.columnWidth(column), 260))


class _Job(QThread):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, parent, operation):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.done.emit(self.operation(self.progress.emit))
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class SearchDialog(QDialog):
    """Pick a catalogue part for one BOM line. Offline search of the downloaded catalogue."""

    def __init__(self, parent, catalogue, line: jlc_parts.BomLine):
        super().__init__(parent)
        self.catalogue, self.code = catalogue, ""
        self.setWindowTitle(f"Assign a part to {_ref_list(line.refs, 6)}")
        self.resize(900, 460)
        layout = QVBoxLayout(self)
        kind = passive_kind_of(line)
        parsed = parse_value(line.value, kind) if kind else None
        value = format_value(parsed.value, kind) if parsed else line.value
        self.exact = (kind, parsed.value, line.package) if parsed else None
        self.query = QLineEdit(value if parsed else (line.mpn or value))
        self.query.setPlaceholderText("LCSC code, manufacturer part number or words (3+ letters each)")
        self.package = QLineEdit(line.package if parsed else "")
        self.package.setPlaceholderText("Package")
        self.package.setMaximumWidth(140)
        self.economic = QCheckBox("Basic/Preferred only")
        self.in_stock = QCheckBox("In stock")
        self.in_stock.setChecked(True)
        self.same_value = QCheckBox("Exact value")
        self.same_value.setChecked(parsed is not None)
        self.same_value.setVisible(parsed is not None)
        top = QHBoxLayout()
        for widget in (self.query, self.package, self.same_value, self.economic, self.in_stock):
            top.addWidget(widget, 1 if widget is self.query else 0)
        layout.addLayout(top)
        self.table = _table(("LCSC", "Tier", "MPN", "Package", "Description", "Stock", "Unit $", "Ext. fee"))
        layout.addWidget(self.table, 1)
        self.note = QLabel(f"Line: {line.value} · {line.footprint}. Source: {catalogue.label}.")
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.ok = QPushButton("Use this part")
        self.ok.setObjectName("primary")
        self.ok.clicked.connect(self.choose)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        for signal in (self.query.textChanged, self.package.textChanged):
            signal.connect(self.search)
        for box in (self.economic, self.in_stock, self.same_value):
            box.toggled.connect(self.search)
        self.table.itemDoubleClicked.connect(self.choose)
        self.results = []
        self.search()

    def search(self, *_):
        try:
            self.results = self.catalogue.search(
                self.query.text(), package=self.package.text().strip(),
                tiers=("basic", "preferred") if self.economic.isChecked() else (),
                in_stock=self.in_stock.isChecked(),
                exact=self.exact if self.same_value.isChecked() else None, limit=200)
        except VelaTraceError as exc:
            self.results = []
            self.note.setText(str(exc))
        _fill(self.table, [(p.lcsc, p.tier.capitalize(), p.mpn, p.package, p.description[:90],
                            "" if p.stock is None else p.stock, p.unit_price(1) or "",
                            f"${JLC_EXTENDED_FEE_USD}" if p.tier == "extended" else "none")
                           for p in self.results],
              [{1: TIER_COLORS.get(p.tier, "")} for p in self.results])
        if self.results:
            self.table.selectRow(0)
        self.ok.setEnabled(bool(self.results))

    def choose(self, *_):
        row = self.table.currentRow()
        if 0 <= row < len(self.results):
            self.code = self.results[row].lcsc
            self.accept()


def passive_kind_of(line: jlc_parts.BomLine):
    return passive_kind(Component(line.refs[0], line.value, line.footprint))


class JlcDialog(QDialog):
    def __init__(self, window):
        super().__init__(window if isinstance(window, QWidget) else None)
        self.window_ = window
        self.config_dir = Path(window.config_dir)
        self.base = window.audit_snapshot          # the design as loaded
        self.snapshot = self.base                  # with pending assignments applied
        self.pending: dict[str, str] = {}          # reference -> LCSC code, not yet written
        self.changed = False                       # the main window should re-check the design
        self.catalogue = None
        self.job = None
        self.cancel = threading.Event()
        self.lines: list[jlc_parts.BomLine] = []
        self.excluded = self._excluded()
        self.setWindowTitle("JLCPCB parts and fabrication files")
        self.resize(1040, 600)
        layout = QVBoxLayout(self)
        self.source = QLabel()
        self.source.setTextFormat(Qt.TextFormat.PlainText)
        self.source.setWordWrap(True)
        layout.addWidget(self.source)
        data = QHBoxLayout()
        self.catalogue_button = self._button(
            "", self.download_catalogue,
            "Downloads the public parts catalogue (size shown first). No board data is sent. "
            "Afterwards search and checks work offline.")
        self.live_button = self._button(
            "Refresh JLCPCB stock/prices", self.refresh_live,
            "Asks JLCPCB for current stock and prices of the LCSC numbers in this table: one "
            "request per part number, one per second. Only the part numbers are sent.")
        for button in (self.catalogue_button, self.live_button):
            data.addWidget(button)
        data.addStretch()
        layout.addLayout(data)
        self.table = _table(COLUMNS)
        self.table.itemDoubleClicked.connect(self.assign_selected)
        layout.addWidget(self.table, 1)
        self.cost = QLabel()
        self.cost.setTextFormat(Qt.TextFormat.PlainText)
        self.cost.setWordWrap(True)
        layout.addWidget(self.cost)
        actions = QHBoxLayout()
        self.assign_button = self._button("Assign part…", self.assign_selected,
                                          "Search the catalogue for the selected line. Nothing is written yet.")
        self.suggest_button = self._button(
            "Use suggested Basic parts", self.use_suggestions,
            "Fill every unassigned passive that has a matching in-stock Basic/Preferred part. "
            "Nothing is written yet.")
        self.write_button = self._button(
            "Write LCSC to board", self.write_board,
            "Writes the pending part numbers to the footprints' LCSC field in the open PCB: one "
            "backed-up change, one Undo step.")
        self.csv_button = self._button("Export assignment CSV", self.export_csv,
                                       "Reference, Value, Footprint, LCSC for every line with a part number.")
        self.export_button = self._button(
            "Export JLCPCB files…", self.export_files,
            "BOM, CPL (pick-and-place) and a Gerber/drill zip, made by KiCad's own kicad-cli. Offline.")
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        for button in (self.assign_button, self.suggest_button, self.write_button, self.csv_button,
                       self.export_button):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(close)
        layout.addLayout(actions)
        status = QHBoxLayout()
        self.status = QLabel("Offline. Nothing is sent or written until you click a button.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(200)
        self.bar.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel.set)
        self.cancel_button.hide()
        status.addWidget(self.status, 1)
        status.addWidget(self.bar)
        status.addWidget(self.cancel_button)
        layout.addLayout(status)
        self.reload()

    # ---- plumbing -------------------------------------------------------------------------
    def _button(self, text, slot, tooltip):
        button = QPushButton(text)
        button.setProperty("tip", tooltip)
        button.setToolTip(tooltip)
        button.clicked.connect(slot)
        return button

    def _excluded(self) -> set[str]:
        """DNP / exclude-from-BOM references, read from the saved board when there is one."""
        path = self.base.path
        try:
            if path and Path(path).suffix == ".kicad_pcb" and Path(path).stat().st_size < 32_000_000:
                parts, _ = jlc_export.board_parts(Path(path).read_text(encoding="utf-8"))
                return {p.reference for p in parts if p.dnp or p.exclude_from_bom}
        except (OSError, VelaTraceError):
            pass
        return set()

    def run(self, title, operation, done):
        """Run `operation(progress)` off the UI thread; one job at a time."""
        if self.job is not None:
            return
        self.cancel.clear()
        self.status.setText(title + "…")
        self.bar.setRange(0, 0)
        self.bar.show()
        self.cancel_button.show()
        self._buttons(False)
        self.job = _Job(self, operation)
        self.job.progress.connect(lambda value, total: (self.bar.setRange(0, 1000),
                                                        self.bar.setValue(int(1000 * value / max(total, 1)))))
        self.job.done.connect(lambda value: self._finish(done, value))
        self.job.failed.connect(lambda message: self._finish(None, message))
        self.job.start()

    def _finish(self, done, value):
        self.job.wait()
        self.job = None
        self.bar.hide()
        self.cancel_button.hide()
        try:
            if done is None:
                self.status.setText("Stopped: " + value)
            else:
                done(value)
        except VelaTraceError as exc:
            self.status.setText("Stopped: " + str(exc))
        if self.job is None:  # `done` may have started the next step
            self.reload()

    def _buttons(self, enabled: bool):
        for button in (self.catalogue_button, self.live_button, self.assign_button, self.suggest_button,
                       self.write_button, self.csv_button, self.export_button):
            button.setEnabled(enabled)

    def reject(self):
        if self.job is not None:
            self.cancel.set()
            self.job.wait(5000)
        if self.catalogue is not None:
            self.catalogue.close()
            self.catalogue = None
        super().reject()

    # ---- table ----------------------------------------------------------------------------
    def reload(self):
        """Rebuild everything from what is on disk. Offline."""
        note = ""
        try:
            if self.catalogue is None:
                self.catalogue = jlc_catalog.load(self.config_dir)
            small = None if self.catalogue else jlc_catalog.load_small_list(self.config_dir)
        except VelaTraceError as exc:
            small, note = None, f" ({exc})"
        db = self.catalogue or small
        self.snapshot = jlc_parts.with_assignments(self.base, self.pending)
        self.lines = jlc_parts.bom_table(self.snapshot, db, jlc_live.load_live(self.config_dir),
                                         excluded=self.excluded)
        rows, colors = [], []
        for line in self.lines:
            pending = any(ref in self.pending for ref in line.refs)
            price = line.unit_price()
            rows.append((_ref_list(line.refs, 6), line.quantity, line.value, line.package,
                         (line.lcsc + (" (pending)" if pending else "")) or
                         (f"suggest {line.suggestion}" if line.suggestion else "—"),
                         line.tier.capitalize() or "—", "" if line.stock is None else line.stock,
                         "" if price is None else price,
                         f"${JLC_EXTENDED_FEE_USD}" if line.fee else "", line.source))
            colors.append({4: ACCENT if pending else "", 5: TIER_COLORS.get(line.tier, ""),
                           6: "#DC2626" if line.stock == 0 else ""})
        _fill(self.table, rows, colors)
        if self.catalogue:
            self.source.setText(f"Catalogue: {self.catalogue.label}, downloaded "
                                f"{self.catalogue.fetched_at[:10]}. Stock and prices in it are as old "
                                "as that date; Refresh asks JLCPCB for the parts on this BOM.")
        else:
            self.source.setText(
                ("Using the small Basic/Preferred list: tiers only, no search, stock or prices. "
                 if small else "No JLCPCB parts data yet. ") + "Download the full catalogue for "
                "search, stock and price breaks." + note)
        self.catalogue_button.setText("Update catalogue…" if self.catalogue else "Download catalogue…")
        estimate = jlc_parts.cost_estimate(self.lines)
        priced = sum(1 for line in self.lines if line.lcsc and line.unit_price() is not None)
        fees = sum(1 for line in self.lines if line.fee)
        self.cost.setText(
            "Parts + Extended fees per board: " + " · ".join(
                f"${row.per_board} at {row.boards}" for row in estimate)
            + f".  {priced} of {len(self.lines)} lines priced; {fees} Extended line(s) × "
              f"${JLC_EXTENDED_FEE_USD} per order. PCB, assembly, shipping and tax are not included."
            if priced else "No prices yet: assign LCSC numbers and download the catalogue or refresh.")
        self._buttons(True)
        board_open = getattr(self.window_, "safety", None) is not None
        codes = any(line.lcsc for line in self.lines)
        for button, reason in (
                (self.live_button, "" if codes else "No LCSC numbers on this BOM yet."),
                (self.assign_button, "" if self.lines else "No parts."),
                (self.suggest_button, "" if any(l.suggestion for l in self.lines) else
                 "No unassigned passive has a matching Basic/Preferred part."),
                (self.write_button, "Nothing pending: assign a part first." if not self.pending else
                 "" if board_open else "Needs the open PCB (KiCad IPC). Export the CSV instead."),
                (self.csv_button, "" if codes else "No LCSC numbers yet."),
                (self.export_button, "" if board_open else "Needs the open PCB (KiCad IPC).")):
            button.setEnabled(not reason)
            button.setToolTip(reason or button.property("tip"))

    # ---- data buttons ---------------------------------------------------------------------
    def download_catalogue(self):
        def sized(info):
            if not _confirm(self, "Download the JLCPCB catalogue",
                            f"Download {info.total / 1e6:.0f} MB from bouni.github.io "
                            f"(published {info.published[:10] or 'unknown'})?\n\n"
                            f"It unpacks to roughly 4 to 5 times that in\n{self.config_dir / jlc_catalog.CACHE_DIR}\n"
                            "and is then used offline. No board data is sent. An interrupted "
                            "download resumes when you click again."):
                self.status.setText("Nothing was downloaded.")
                return
            if self.catalogue is not None:   # Windows cannot replace a file that is open
                self.catalogue.close()
                self.catalogue = None

            def fetch(progress):
                return jlc_catalog.download(self.config_dir, info=info, progress=progress,
                                            cancel=self.cancel.is_set)

            def done(meta):
                self.changed = True
                self.status.setText(f"Catalogue installed: {meta['counts']['parts']:,} parts, "
                                    f"published {meta['published'][:10]}.")
            self.run("Downloading and verifying the catalogue", fetch, done)
        self.run("Asking the catalogue server for the download size", lambda _: jlc_catalog.remote_info(), sized)

    def refresh_live(self):
        codes = sorted({line.lcsc for line in self.lines if line.lcsc})
        if not codes or not _confirm(
                self, "Refresh JLCPCB stock/prices",
                f"Ask cart.jlcpcb.com about {len(codes)} LCSC part number(s)?\n\n"
                "Only the part numbers are sent, one request per second (answers newer than "
                f"{jlc_live.MAX_AGE.seconds // 3600} hours are reused). This is JLCPCB's public parts "
                "page data, not an official API; if it is unavailable nothing else is affected."):
            return

        def done(result):
            self.changed = True
            self.status.setText(result.summary)
        self.run("Asking JLCPCB", lambda progress: jlc_live.refresh(
            self.config_dir, codes, progress=progress, cancel=self.cancel.is_set), done)

    # ---- assignment -----------------------------------------------------------------------
    def set_pending(self, refs, code: str):
        current = {c.reference: lcsc_code(c) for c in self.base.components}
        for ref in refs:
            if current.get(ref) == code:
                self.pending.pop(ref, None)
            else:
                self.pending[ref] = code
        self.reload()

    def assign_selected(self, *_):
        row = self.table.currentRow()
        if not 0 <= row < len(self.lines):
            self.status.setText("Select a line first.")
            return
        line = self.lines[row]
        if self.catalogue is None:
            self.status.setText("Download the catalogue to search for parts.")
            return
        dialog = SearchDialog(self, self.catalogue, line)
        if dialog.exec() and dialog.code:
            self.set_pending(line.refs, dialog.code)
            self.status.setText(f"{dialog.code} pending for {_ref_list(line.refs, 6)}. "
                                "Nothing is written until you click Write LCSC to board.")

    def use_suggestions(self):
        count = 0
        for line in list(self.lines):
            if line.suggestion and not line.lcsc:
                self.pending.update({ref: line.suggestion for ref in line.refs})
                count += 1
        self.reload()
        self.status.setText(f"{count} line(s) filled with Basic/Preferred suggestions (pending).")

    def write_board(self):
        safety = getattr(self.window_, "safety", None)
        if safety is None or not self.pending or not _confirm(
                self, "Write LCSC numbers to the board",
                f"Set the LCSC field of {len(self.pending)} footprint(s) in the open PCB?\n\n"
                "A backup is made first and KiCad records it as one Undo step. The schematic is "
                "not changed: KiCad 10 cannot edit it from a plugin. Afterwards run Tools > "
                "Update Schematic from PCB with 'Other fields' ticked."):
            return
        pending = dict(self.pending)

        def done(result):
            self.base, self.pending, self.changed = jlc_parts.with_assignments(self.base, pending), {}, True
            self.status.setText(result.summary)
        self.run("Writing LCSC fields", lambda _: jlc_assign.assign_lcsc(safety, pending), done)

    def export_csv(self):
        name = Path(self.base.path or "design").stem
        path, _ = QFileDialog.getSaveFileName(self, "Export assignment CSV", f"{name}-lcsc.csv", "CSV (*.csv)")
        if not path:
            return
        rows = [(ref, line.value, line.footprint, line.lcsc)
                for line in self.lines if line.lcsc for ref in line.refs]
        try:
            jlc_assign.export_assignments(Path(path), rows)
        except OSError as exc:
            self.status.setText(f"Stopped: could not write the CSV ({exc.strerror}).")
            return
        self.status.setText(f"Wrote {len(rows)} assignment(s) to {path}. Includes pending ones.")

    # ---- fabrication files ----------------------------------------------------------------
    def export_files(self):
        safety = getattr(self.window_, "safety", None)
        if safety is None:
            return
        if self.pending and not _confirm(self, "Pending part numbers",
                                         f"{len(self.pending)} pending part number(s) are not on the "
                                         "board yet and will be missing from the BOM. Export anyway?"):
            return
        folder = QFileDialog.getExistingDirectory(self, "Folder for the JLCPCB files",
                                                  str(safety.path.parent))
        if not folder:
            return

        def operation(_):
            cli = KiCadCli(self.window_.settings.cli)
            return jlc_export.export_fabrication(
                cli, safety.path, Path(folder), board_text=safety.board.get_as_string(),
                rotation_folders=(safety.path.parent, self.config_dir))
        self.run("Exporting BOM, CPL and Gerbers", operation,
                 lambda result: self.status.setText(result.summary))

