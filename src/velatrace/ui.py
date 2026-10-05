"""Single external IPC companion window. All board writes use BoardSafety.

Qt widgets are confined to the main thread. A single serialized worker owns each
blocking service operation; it reports plain data through queued signals.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import os
import shutil
import tempfile
import threading
import time
import uuid
from queue import Queue

from PySide6.QtCore import Qt, QThread, Signal, Slot, QStandardPaths, QTimer
from PySide6.QtGui import QCursor, QColor, QPainter, QPen, QFontDatabase, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QToolTip,
    QTextEdit, QVBoxLayout, QWidget,
)

from .audit import AuditSession, AuditStage, require_connectivity
from .audit_rules import run_rules, summarize
from .bom import bom_findings
from .candidate import SafeCandidateValidator, canonical, project_context, trusted_via_catalog
from .constraints import Constraint, ConstraintStore, Scope, propose_constraint
from .dsn import ExportTicket, accept_export, export_live
from .errors import ExportUnavailable, RoutingCancelled, ValidationError
from .findings import Finding, Severity
from .flags import Bucket, Function, Verdict
from .freerouting import CANCELLED, Freerouting
from .ipc import KiCadReader
from .kicad_cli import KiCadCli
from .models import Component, DesignSnapshot, Pin
from .netlist import read_xml_netlist
from .report import render_report, save_report
from . import __version__
from .preflight import preflight
from .parts_db import cache_paths, download_catalogue, load_catalogue
from .pricing import PricingSession, estimate_price
from .privacy import ConsentStore, PROVIDER_NOTE, disclosure_text
from .provider import CallBudget, Provider, ProviderConfig, Usage
from .route_repair import repair_dangling
from .routing import Mode, RoutingSession, RoutingStage
from .sexpr import parse
from .tokens import LocalChatTokenizer
from .theme import saved_kicad_theme
from .write_safety import BoardSafety, SafeBoardWriter

ACCENT = "#8B5CF6"
BUCKET_COLORS = {"critical": "#F87171", "important": "#FBBF24",
                 "nice-to-have": "#60A5FA", "redundant": "#A78BFA"}
SEVERITY_COLORS = {Severity.ERROR: "#F87171", Severity.WARNING: "#FBBF24",
                   Severity.SAVING: "#34D399", Severity.INFO: "#60A5FA"}
SEVERITY_GROUPS = ((Severity.ERROR, "Errors"), (Severity.WARNING, "Warnings"),
                   (Severity.SAVING, "Savings"), (Severity.INFO, "Info"))
NO_REMOVAL = "Suggestions only. Components are never removed automatically."
AI_DEFAULT_DESCRIPTION = "Not provided; infer the purpose from the design."


def theme_tokens(dark: bool) -> dict:
    """Colours for the frosted-glass look: translucent cards over a painted gradient.

    Real Windows 11 Acrylic was measured and rejected: Windows removes it from
    stay-on-top windows (VelaTrace stays above KiCad), and dark Acrylic renders
    as near-opaque grey. Popups and dialogs stay solid for legibility."""
    if dark:
        tokens = dict(fg="#F5F3FF", muted="#B7B1CF", solid="#1C1930", rim="rgba(255,255,255,40)",
                      card="rgba(255,255,255,18)", card_hover="rgba(255,255,255,28)",
                      field="rgba(255,255,255,14)",
                      frost="qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #251F45, stop:1 #120F22)")
    else:
        tokens = dict(fg="#1E1933", muted="#5E5874", solid="#F6F3FF", rim="rgba(255,255,255,230)",
                      card="rgba(255,255,255,150)", card_hover="rgba(255,255,255,200)",
                      field="rgba(255,255,255,170)",
                      frost="qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #F7F4FF, stop:1 #E4DDFB)")
    return tokens


def glass_stylesheet(t: dict) -> str:
    gloss = ("qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #B79CFF, stop:0.48 #8B5CF6, "
             "stop:0.52 #7A48EE, stop:1 #6A36E0)")
    return f"""
        QMainWindow {{background:{t['solid']}}}
        QWidget {{background:transparent; color:{t['fg']}; font-family:'Segoe UI Variable Text','Segoe UI'; font-size:12px}}
        QWidget#glassRoot {{background:{t['frost']}}}
        QDialog, QMessageBox {{background:{t['frost']}}}
        QLabel#muted {{color:{t['muted']}}}
        QLabel#title {{font-size:20px; font-weight:700; color:{t['fg']}}}
        QLabel#error {{color:#D45A67}}
        QFrame#card {{background:{t['card']}; border:1px solid {t['rim']}; border-radius:14px}}
        QFrame#card:hover {{background:{t['card_hover']}}}
        QFrame#card QLabel {{background:transparent}}
        QPushButton {{background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {t['card_hover']}, stop:1 {t['card']});
                      border:1px solid {t['rim']}; border-radius:10px; padding:8px 12px}}
        QPushButton:hover {{border-color:{ACCENT}}}
        QPushButton:pressed {{background:{t['card']}}}
        QPushButton:disabled {{color:{t['muted']}}}
        QPushButton#primary {{background:{gloss}; color:white; border:1px solid rgba(255,255,255,90); font-weight:600}}
        QPushButton#primary:hover {{border-color:white}}
        QPushButton#primary:disabled {{background:{t['card']}; color:{t['muted']}; border:1px solid {t['rim']}}}
        QPushButton#danger:enabled {{color:#D45A67; border-color:#D45A67}}
        QPushButton#textButton {{border:0; color:{ACCENT}; padding:4px 0px; background:transparent}}
        QPushButton#textButton:disabled {{color:{t['muted']}}}
        QPushButton#seg {{padding:4px 14px; border-radius:9px}}
        QPushButton#seg:checked {{background:{gloss}; color:white; font-weight:600; border:1px solid rgba(255,255,255,90)}}
        QCheckBox::indicator {{width:14px; height:14px; border:1px solid {t['muted']}; border-radius:4px; background:{t['field']}}}
        QCheckBox::indicator:checked {{background:{ACCENT}; border-color:{ACCENT}}}
        QLineEdit,QTextEdit,QComboBox,QSpinBox,QDoubleSpinBox,QListWidget {{background:{t['field']};
                      border:1px solid {t['rim']}; border-radius:9px; padding:6px}}
        QLineEdit:focus,QTextEdit:focus,QComboBox:focus {{border-color:{ACCENT}}}
        QComboBox QAbstractItemView, QToolTip {{background:{t['solid']}; color:{t['fg']}; border:1px solid {ACCENT}}}
        QScrollArea {{border:0}} QCheckBox {{spacing:7px}}
        QScrollBar:vertical {{width:8px; background:transparent}}
        QScrollBar::handle:vertical {{background:{t['rim']}; border-radius:4px; min-height:30px}}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{height:0}}
        QLineEdit:disabled,QTextEdit:disabled,QComboBox:disabled,QSpinBox:disabled,QDoubleSpinBox:disabled,QListWidget:disabled {{color:{t['muted']}; background:transparent}}
        QCheckBox:disabled,QLabel:disabled {{color:{t['muted']}}}
    """


def label(text="", *, muted=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if muted:
        widget.setObjectName("muted")
    return widget


def tip(widget, text):
    """Set a widget's default tooltip; gate() restores it once the widget is enabled."""
    widget.setProperty("tip", text)
    widget.setToolTip(text)
    return widget


def gate(button, reason=""):
    """Enable a button, or disable it and say why in its tooltip."""
    button.setEnabled(not reason)
    button.setToolTip(reason or button.property("tip") or "")


def set_primary(button, on):
    """One highlighted (primary) action per step."""
    name = "primary" if on else ""
    if button.objectName() != name:
        button.setObjectName(name)
        button.style().unpolish(button)
        button.style().polish(button)


def ask(parent, title, text):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return box.exec() == QMessageBox.StandardButton.Ok


class Worker(QThread):
    result = Signal(object)
    error = Signal(str)
    usage = Signal(object)
    progress = Signal(str)
    idle = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.jobs = Queue()

    def run(self):
        while True:
            operation = self.jobs.get()
            if operation is None:
                return
            try:
                self.result.emit(operation(self.usage.emit))
            except Exception as exc:
                # Provider errors are sanitized by the provider boundary. Never log keys.
                self.error.emit(str(exc) or type(exc).__name__)
            finally:
                self.idle.emit()


@dataclass
class Settings:
    jar: str = ""
    java: str = "java"
    cli: str = "kicad-cli"
    name: str = "Gemini"
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    protocol: str = "gemini"
    model: str = ""
    key: str = field(default="", repr=False)
    search_model: str = ""
    search: bool = False
    warm_router: bool = False
    tokenizer: str = ""
    verified_model: str = ""
    cap: int = 40
    output_cap: int = 4096

    def provider_config(self):
        return ProviderConfig(self.name, self.endpoint, self.model, self.key,
                              self.protocol, self.search_model or None, self.search)

    def save(self, path: Path):
        """Remember paths and provider choices between launches. Never the API key."""
        values = {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "key"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, indent=2), encoding="utf-8")

    def load(self, path: Path):
        """Apply saved values of the right type; a missing or damaged file changes nothing."""
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        for name, value in saved.items():
            if name != "key" and name in self.__dataclass_fields__ and type(value) is type(getattr(self, name)):
                setattr(self, name, value)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent):
        super().__init__(parent)
        self.setWindowTitle("VelaTrace setup")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        self.fields = {}

        def section(title, expanded, hint=""):
            toggle = QPushButton()
            toggle.setObjectName("textButton")
            toggle.setCheckable(True)
            toggle.setToolTip(hint)
            body = QWidget()
            form = QFormLayout(body)
            form.setContentsMargins(14, 0, 0, 8)
            def flip(on):
                body.setVisible(on)
                toggle.setText(("− " if on else "+ ") + title)
                QTimer.singleShot(0, self.adjustSize)
            toggle.toggled.connect(flip)
            toggle.setChecked(expanded)
            flip(expanded)
            layout.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(body)
            return form

        def text(form, attr, title, hint="", browse=None):
            field = QLineEdit(getattr(settings, attr))
            field.setMinimumWidth(320)
            field.setToolTip(hint)
            self.fields[attr] = field
            if browse is None:
                form.addRow(title, field)
                return field
            row = QHBoxLayout()
            row.addWidget(field, 1)
            pick = QPushButton("Browse…")
            def choose():
                chosen, _ = QFileDialog.getOpenFileName(self, title, field.text(), browse)
                if chosen:
                    field.setText(chosen)
            pick.clicked.connect(choose)
            row.addWidget(pick)
            form.addRow(title, row)
            return field

        ready = getattr(parent, "ready", False)
        tools = section("Tools", not (settings.jar and ready),
                        f"Needed for routing only. Saved in {parent.config_dir}")
        text(tools, "jar", "Freerouting 2.1.0 JAR", browse="Freerouting JAR (*.jar)")
        text(tools, "java", "Java 21", browse="All files (*)")
        text(tools, "cli", "kicad-cli", "Must match the running KiCad version exactly.", browse="All files (*)")
        self.warm_router = QCheckBox("Keep Freerouting running (faster)")
        self.warm_router.setToolTip("Keeps one Freerouting Java process between routes. Every route runs "
                                    "VelaTrace's MIT launcher and the GPLv3 Freerouting JAR in one Java process "
                                    "(needed to stop a stalled router and keep its partial route); "
                                    "off starts a fresh process per route.")
        self.warm_router.setChecked(settings.warm_router)
        tools.addRow(self.warm_router)

        ai = section("AI (optional)", True, "Only for Explain with AI and pricing. "
                     "No backend or telemetry: only your endpoint receives requests.")
        self.protocol = QComboBox()
        self.protocol.addItems(["gemini", "openai"])
        self.protocol.setCurrentText(settings.protocol)
        self.protocol.setToolTip("Gemini uses exact countTokens. Groq/OpenAI-compatible models need a "
                                 "verified local tokenizer (Advanced).")
        ai.addRow("Protocol", self.protocol)
        text(ai, "name", "Provider", "Groq or Gemini offer free tiers, subject to current quotas and terms. "
             + PROVIDER_NOTE)
        text(ai, "endpoint", "API base URL", "HTTPS base URL. Only this endpoint receives requests.")
        text(ai, "model", "Model ID", "Exact, currently available model ID.")
        key = text(ai, "key", "API key", "Kept in memory for this launch only; never saved.")
        key.setEchoMode(QLineEdit.EchoMode.Password)
        self.search = QCheckBox("Built-in web search")
        self.search.setChecked(settings.search)
        def update_search(protocol):
            available = protocol != "gemini"
            self.search.setEnabled(available)
            self.search.setToolTip("Must be supported by the selected provider and model." if available else
                                   "Unavailable for Gemini until grounded-result and Search Suggestion "
                                   "display is supported. Pricing uses local estimates.")
            if not available:
                self.search.setChecked(False)
        self.protocol.currentTextChanged.connect(update_search)
        update_search(self.protocol.currentText())
        ai.addRow(self.search)

        advanced = section("Advanced", False)
        text(advanced, "search_model", "Search model ID", "Optional; defaults to the model ID.")
        text(advanced, "tokenizer", "Tokenizer folder", "Local tokenizer for the OpenAI protocol.")
        text(advanced, "verified_model", "Tokenizer model ID",
             "The model whose chat template you verified this tokenizer against.")
        self.cap = QSpinBox()
        self.cap.setRange(1, 1000)
        self.cap.setValue(settings.cap)
        self.cap.setToolTip("Hard limit; requests stop when it is reached.")
        advanced.addRow("API calls per audit", self.cap)
        self.output_cap = QSpinBox()
        self.output_cap.setRange(1, 65536)
        self.output_cap.setValue(settings.output_cap)
        advanced.addRow("Output tokens per analysis", self.output_cap)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        values = {name: field.text().strip() for name, field in self.fields.items()}
        return Settings(**values, protocol=self.protocol.currentText(), search=self.search.isChecked(), warm_router=self.warm_router.isChecked(),
                        cap=self.cap.value(), output_cap=self.output_cap.value())


class ComponentCard(QFrame):
    """Plain text throughout, including model-supplied text and source fields."""
    def __init__(self, component, function=None, verdict=None, price=None, flag=None,
                 editable=False, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        outer = QHBoxLayout(self)
        bucket = verdict.bucket.value if verdict else "function review"
        bar = QFrame()
        bar.setFixedWidth(4)
        bar.setStyleSheet(f"background:{BUCKET_COLORS.get(bucket, ACCENT)}; border-radius:2px")
        outer.addWidget(bar)
        body = QVBoxLayout()
        row = QHBoxLayout()
        part = label(f"{component.reference} · {component.value}")
        font = part.font()
        font.setBold(True)
        font.setPointSize(12)
        part.setFont(font)
        row.addWidget(part, 1)
        self.price_label = label(f"${price.amount:.2f}" + (" est." if price.estimated else "") if price else "")
        self.price_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self.price_label)
        body.addLayout(row)
        body.addWidget(label(bucket.upper(), muted=True))
        self.function_edit = QLineEdit(function.text if function else "")
        self.function_edit.setPlaceholderText("Function")
        self.role_edit = QLineEdit(function.role if function else "")
        if editable:
            body.addWidget(self.function_edit)
            self.role_edit.setPlaceholderText("Electrical role")
            body.addWidget(self.role_edit)
        elif function:
            body.addWidget(label(function.text, muted=True))
        if flag:
            body.addWidget(label(flag.reason))
        self.details = label("\n\n".join(text for text in [
            verdict.suggestion if verdict else "",
            price.note if price else "", price.source if price and price.source else "",
            f"Footprint: {component.footprint}"] if text))
        self.details.setVisible(False)
        expand = QPushButton("Suggestion +" if verdict else "Details +")
        expand.setObjectName("textButton")
        expand.setCheckable(True)
        expand.toggled.connect(self.details.setVisible)
        self.expand = expand
        self.editable = editable
        self.setCursor(Qt.CursorShape.ArrowCursor if editable else Qt.CursorShape.PointingHandCursor)
        body.addWidget(expand, alignment=Qt.AlignmentFlag.AlignLeft)
        body.addWidget(self.details)
        outer.addLayout(body, 1)

    def mouseReleaseEvent(self, event):
        if not self.editable and event.button() == Qt.MouseButton.LeftButton:
            self.expand.setChecked(not self.expand.isChecked())
        super().mouseReleaseEvent(event)


class FindingCard(QFrame):
    """One rule finding: the title up front; evidence, fix and cost when expanded.

    `locate` is the hook for zooming KiCad to the involved parts; no button is
    shown until that exists. Design text stays plain text."""
    def __init__(self, finding: Finding, locate=None, parent=None):
        super().__init__(parent)
        self.finding = finding
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        outer = QHBoxLayout(self)
        bar = QFrame()
        bar.setFixedWidth(4)
        bar.setStyleSheet(f"background:{SEVERITY_COLORS[finding.severity]}; border-radius:2px")
        outer.addWidget(bar)
        body = QVBoxLayout()
        row = QHBoxLayout()
        self.title = label(finding.title)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        row.addWidget(self.title, 1)
        cost = ""
        if finding.cost_delta is not None:
            sign = "−" if finding.cost_delta < 0 else "+"
            amount = f"{sign}${abs(finding.cost_delta):.2f}"
            cost = f"Cost: {amount}/board (illustrative)"
            row.addWidget(label(amount, muted=True))
        self.chevron = label("+", muted=True)
        row.addWidget(self.chevron)
        body.addLayout(row)
        self.details = label("\n\n".join(text for text in (
            finding.evidence, "Fix: " + finding.fix if finding.fix else "", cost) if text), muted=True)
        self.details.setVisible(False)
        body.addWidget(self.details)
        if locate is not None and finding.refs:
            button = QPushButton("Show in KiCad")
            button.setObjectName("textButton")
            button.clicked.connect(lambda: locate(finding.refs))
            body.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        outer.addLayout(body, 1)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.details.setVisible(self.details.isHidden())
            self.chevron.setText("+" if self.details.isHidden() else "−")
        super().mouseReleaseEvent(event)


class RouteCanvas(QWidget):
    def __init__(self):
        super().__init__()
        self.plan = None
        self.setMinimumHeight(230)

    def mousePressEvent(self, event):
        if self.toolTip():
            QToolTip.showText(QCursor.pos(), self.toolTip(), self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        if not self.plan:
            painter.setPen(self.palette().text().color())
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Route preview")
            return
        tracks = self.plan.tracks
        if not tracks:
            return
        layers = list(self.plan.layers_used)
        from PySide6.QtCore import QPointF
        for index, layer in enumerate(layers):
            selected = [track for track in tracks if track.layer == layer]
            if not selected:
                continue
            points = [point for track in selected for point in track.points_mm]
            min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
            min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
            width = self.width() / len(layers)
            scale = min((width - 36) / max(max_x - min_x, 1),
                        (self.height() - 60) / max(max_y - min_y, 1))
            painter.setPen(QColor(ACCENT))
            painter.drawText(int(index * width + 18), 25, layer)
            painter.setPen(QPen(QColor(ACCENT), 2, Qt.PenStyle.DashLine))
            def point(value):
                return QPointF(index * width + 18 + (value[0] - min_x) * scale,
                               45 + (max_y - value[1]) * scale)
            for track in selected:
                for start, end in zip(track.points_mm, track.points_mm[1:]):
                    painter.drawLine(point(start), point(end))


class ConstraintsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("Routing constraints")
        self.resize(480, 380)
        layout = QVBoxLayout(self)
        self.items = QListWidget()
        layout.addWidget(self.items)
        form = QFormLayout()
        self.scope = QComboBox()
        self.scope.addItems(["session", "universal"])
        self.scope.setToolTip("Session: this run only, stacking across retries. Universal: saved for every board.")
        self.kind = QComboBox()
        self.kind.addItems(["clearance", "trace-width", "header-clearance"])
        self.kind.setToolTip("The router enforces all-net clearance and trace width. Header and per-net rules "
                             "stay listed but stop routing until supported; they are never ignored.")
        self.minimum = QDoubleSpinBox()
        self.minimum.setRange(.001, 100)
        self.minimum.setDecimals(3)
        self.minimum.setValue(.25)
        self.minimum.setSuffix(" mm")
        self.target = QLineEdit("all nets")
        form.addRow("Scope", self.scope)
        form.addRow("Rule", self.kind)
        form.addRow("Minimum", self.minimum)
        form.addRow("Target", self.target)
        layout.addLayout(form)
        row = QHBoxLayout()
        add = QPushButton("Add")
        add.setObjectName("primary")
        remove = QPushButton("Remove")
        add.clicked.connect(self.add)
        remove.clicked.connect(self.remove)
        row.addWidget(add)
        row.addWidget(remove)
        layout.addLayout(row)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.refresh()

    def refresh(self):
        self.items.clear()
        for item in self.owner.constraints.items:
            self.items.addItem(item.description)
        self.owner.refresh_badge()

    def add(self):
        try:
            item = Constraint(uuid.uuid4().hex, Scope(self.scope.currentText()),
                              self.kind.currentText(), self.target.text(), self.minimum.value())
            if self.owner.routing:
                self.owner.routing.add_constraint(item)
            else:
                self.owner.constraints.add(item)
            self.owner.invalidate_visible_preview()
            self.refresh()
        except Exception as exc:
            self.owner.show_error(str(exc))

    def remove(self):
        row = self.items.currentRow()
        if row < 0:
            return
        try:
            identifier = self.owner.constraints.items[row].id
            if self.owner.routing:
                self.owner.routing.remove_constraint(identifier)
            else:
                self.owner.constraints.remove(identifier)
            self.owner.invalidate_visible_preview()
            self.refresh()
        except Exception as exc:
            self.owner.show_error(str(exc))


READY_STEP = "Pick a source, then Check design."
EXPLAIN = "Explain with AI"


class MainWindow(QMainWindow):
    def __init__(self, *, demo=False, config_dir=None):
        super().__init__()
        self.setWindowTitle("VelaTrace")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(600, min(860, screen.height() - 80))
        self._positioned = False
        self._dark = True
        self.demo = demo
        self.config_dir = Path(config_dir or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
        self.constraints = ConstraintStore(self.config_dir / "constraints.json")
        self.settings = Settings(jar=os.environ.get("VELATRACE_FREEROUTING_JAR", ""),
                                 java=os.environ.get("VELATRACE_JAVA", "java"),
                                 cli=os.environ.get("VELATRACE_KICAD_CLI", "kicad-cli"),
                                 key=os.environ.get("VELATRACE_API_KEY", ""),
                                 model=os.environ.get("VELATRACE_MODEL", ""))
        if not demo:
            self.settings.load(self.config_dir / "settings.json")
        self._setup_error = ""
        self.audit = self.pricing = self.routing = self.router = None
        self.reader = self.safety = self.validator = self.writer = None
        self.ticket = self.snapshot = None
        # The audit's own design read; self.snapshot belongs to routing.
        self.audit_snapshot: DesignSnapshot | None = None
        self.findings: list[Finding] = []
        self.worker = None
        self.executor = Worker(self)
        self.executor.usage.connect(self.update_usage)
        self.executor.result.connect(self.work_result)
        self.executor.error.connect(self.work_error)
        self.executor.progress.connect(self.work_progress)
        self.executor.idle.connect(self.work_finished)
        self._work_started = None
        self.work_timer = QTimer(self)
        self.work_timer.setInterval(1000)
        self.work_timer.timeout.connect(self.update_work_status)
        self.executor.start()
        # Candidate DRC runs here after the preview is drawn, so routing controls stay
        # usable; a reroute or rejection makes its result stale (session generation).
        self.drc_executor = Worker(self)
        self.drc_executor.result.connect(self.drc_finished)
        self.drc_executor.start()
        self._drc_pending = 0
        self._cancel = None  # threading.Event of the running Route board click
        self._dsn_folder = None  # private copy of the last exported board and DSN
        self._preflight_notes = []
        self.ready = False
        self.preview_shown = False
        self._cleanup_failed = False
        self.mode = Mode.AUDIT
        self.consent = ConsentStore(self.config_dir / "privacy-consent.json")
        self.cards = {}
        self._offered_pricing = None  # fingerprint of the pricing estimate shown beside the button
        self.build_ui()
        self.apply_theme("KiCad")
        if demo:
            self.load_demo()
        elif self.settings.jar:
            # Re-verify a remembered router quietly; no dialog stands between launch
            # and the audit, which needs no setup at all.
            QTimer.singleShot(0, lambda: self.apply_settings(self.settings, save=False))

    def build_ui(self):
        central = QWidget()
        central.setObjectName("glassRoot")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = label("VelaTrace")
        title.setObjectName("title")
        header.addWidget(title)
        header.addSpacing(8)
        self.mode_buttons = {}
        for mode, text, keys in ((Mode.AUDIT, "Audit", "Ctrl+1"), (Mode.ROUTING, "Route", "Ctrl+2")):
            button = tip(QPushButton(text), f"{text} mode ({keys})")
            button.setObjectName("seg")
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, target=mode: self.switch_mode(target))
            QShortcut(QKeySequence(keys), self, activated=button.click)
            header.addWidget(button)
            self.mode_buttons[mode] = button
        self.set_mode_buttons(Mode.AUDIT)
        header.addStretch()
        self.theme = QComboBox()
        self.theme.addItems(["KiCad", "System", "Light", "Dark"])
        self.theme.setToolTip("Theme")
        self.theme.currentTextChanged.connect(self.apply_theme)
        header.addWidget(self.theme)
        self.setup_button = tip(QPushButton("Setup"), "Router paths and AI provider")
        self.setup_button.clicked.connect(self.configure)
        header.addWidget(self.setup_button)
        layout.addLayout(header)
        self.banner = label(muted=True)
        self.banner.hide()
        layout.addWidget(self.banner)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(keys), self, activated=self.primary_action)
        self.pages = QStackedWidget()

        self.audit_page = QWidget()
        audit_layout = QVBoxLayout(self.audit_page)
        audit_layout.setContentsMargins(0, 0, 0, 0)
        source = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItems(["Open PCB", "Schematic file…", "Netlist file…"])
        self.source.setToolTip("Open PCB reads KiCad live. Files are read as saved; unsaved edits are not included.")
        source.addWidget(self.source, 1)
        self.load_button = tip(QPushButton("Check design"), "Run the built-in checks on this computer (Ctrl+Enter)")
        self.load_button.setObjectName("primary")
        self.load_button.clicked.connect(self.load_design)
        source.addWidget(self.load_button)
        self.restart_button = tip(QPushButton("Reset"), "Clear findings, annotations and the AI review")
        self.restart_button.clicked.connect(self.restart_audit)
        source.addWidget(self.restart_button)
        audit_layout.addLayout(source)
        self.audit_step = label(READY_STEP, muted=True)
        audit_layout.addWidget(self.audit_step)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.card_container = QWidget()
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSpacing(6)
        self.card_layout.addStretch()
        self.scroll.setWidget(self.card_container)
        audit_layout.addWidget(self.scroll, 1)
        self.totals = tip(label(muted=True), NO_REMOVAL)
        self.totals.hide()
        audit_layout.addWidget(self.totals)
        # Only the optional AI pass uses the description; it never blocks the checks.
        self.description = QTextEdit()
        self.description.setAcceptRichText(False)
        self.description.setPlaceholderText("What does the board do? (optional, for AI)")
        self.description.setMaximumHeight(52)
        self.description.hide()
        audit_layout.addWidget(self.description)
        buttons = QHBoxLayout()
        self.next_button = tip(QPushButton(EXPLAIN), "Optional. Sends the design and findings to your AI provider.")
        self.next_button.clicked.connect(self.audit_next)
        self.price_button = tip(QPushButton("Price flagged parts"), "Optional. Uses your AI provider.")
        self.price_button.clicked.connect(self.price_parts)
        buttons.addWidget(self.next_button, 1)
        buttons.addWidget(self.price_button, 1)
        audit_layout.addLayout(buttons)
        # The one cost line: what the next AI click sends. Clicking is the consent.
        self.cost_line = label(muted=True)
        self.cost_line.hide()
        audit_layout.addWidget(self.cost_line)
        extras = QHBoxLayout()
        cached = cache_paths(self.config_dir)[0].exists()
        self.parts_button = tip(QPushButton("Update JLCPCB parts" if cached else "Get JLCPCB parts list"),
                                "Free JLCPCB Basic-parts list (~0.8 MB) for savings checks. Downloaded once, "
                                "then used offline; no board data is sent.")
        self.parts_button.setObjectName("textButton")
        self.parts_button.clicked.connect(self.download_parts)
        extras.addWidget(self.parts_button)
        extras.addSpacing(16)
        self.report_button = tip(QPushButton("Save report"), "Single offline HTML file you can share")
        self.report_button.setObjectName("textButton")
        self.report_button.clicked.connect(self.export_report)
        extras.addWidget(self.report_button)
        extras.addSpacing(16)
        self.annotations = tip(QPushButton("Annotate board"), "Show each part's AI bucket on User.9 (temporary)")
        self.annotations.setObjectName("textButton")
        self.annotations.clicked.connect(self.show_annotations)
        extras.addWidget(self.annotations)
        extras.addStretch()
        audit_layout.addLayout(extras)
        self.pages.addWidget(self.audit_page)

        self.route_page = QWidget()
        route_layout = QVBoxLayout(self.route_page)
        route_layout.setContentsMargins(0, 0, 0, 0)
        rule = QHBoxLayout()
        self.route_prompt = QLineEdit()
        self.route_prompt.setPlaceholderText("Optional rule, e.g. keep traces away from headers")
        self.route_prompt.returnPressed.connect(self.propose)
        rule.addWidget(self.route_prompt, 1)
        proposal = tip(QPushButton("Add rule"), "Turn the text into a numeric constraint")
        proposal.clicked.connect(self.propose)
        rule.addWidget(proposal)
        route_layout.addLayout(rule)
        constraints = QHBoxLayout()
        self.constraint_line = label(muted=True)
        constraints.addWidget(self.constraint_line, 1)
        edit = tip(QPushButton("Edit…"), "Session and universal constraints")
        edit.setObjectName("textButton")
        edit.clicked.connect(self.open_constraints)
        constraints.addWidget(edit, alignment=Qt.AlignmentFlag.AlignTop)
        route_layout.addLayout(constraints)
        # One click reads the open board, checks it, exports its DSN and routes it.
        self.route_button = tip(QPushButton("Route board"),
                                "Reads the open board, checks it, exports the DSN and routes it "
                                "(Ctrl+Enter). The preview is dashed on User.9.")
        self.route_button.setObjectName("primary")
        self.route_button.clicked.connect(self.route)
        route_layout.addWidget(self.route_button)
        # Fallback only, shown when KiCad cannot export the DSN automatically.
        self.import_button = QPushButton("Load DSN exported from KiCad…")
        self.import_button.clicked.connect(self.load_dsn)
        route_layout.addWidget(self.import_button)
        self.canvas = RouteCanvas()
        route_layout.addWidget(self.canvas, 1)
        self.route_summary = label()  # errors and blocking messages only; info goes to note()
        self.route_summary.hide()
        route_layout.addWidget(self.route_summary)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Why reject? (required)")
        route_layout.addWidget(self.reason)
        row = QHBoxLayout()
        self.reject_button = tip(QPushButton("Reject"), "Remove the preview; a reason is required")
        self.reject_button.clicked.connect(self.reject_route)
        # Deliberately not the primary action: enabled only when DRC findings block approval.
        self.approve_anyway_button = tip(QPushButton("Approve anyway…"),
                                         "Write the copper despite DRC errors, after a confirmation")
        self.approve_anyway_button.setObjectName("danger")
        self.approve_anyway_button.clicked.connect(self.approve_anyway_route)
        self.approve_button = tip(QPushButton("4 · Approve and apply copper"),
                                  "One backed-up KiCad commit; Undo in KiCad reverts it. Save in KiCad afterwards.")
        self.approve_button.setObjectName("primary")
        self.approve_button.clicked.connect(self.approve_route)
        row.addWidget(self.reject_button)
        row.addWidget(self.approve_anyway_button)
        row.addWidget(self.approve_button)
        route_layout.addLayout(row)
        # Validation details can grow, and small displays must still expose the
        # approval controls without forcing the whole window off screen.
        self.route_scroll = QScrollArea()
        self.route_scroll.setWidgetResizable(True)
        self.route_scroll.setWidget(self.route_page)
        route_box = QWidget()
        route_box_layout = QVBoxLayout(route_box)
        route_box_layout.setContentsMargins(0, 0, 0, 0)
        # Outside route_page so it stays readable while routing is locked (disabled).
        self.setup_hint = label()
        self.setup_hint.setObjectName("error")
        route_box_layout.addWidget(self.setup_hint)
        route_box_layout.addWidget(self.route_scroll, 1)
        self.pages.addWidget(route_box)
        layout.addWidget(self.pages, 1)
        # Outside the route page, which is disabled while the worker runs.
        self.cancel_button = QPushButton("Cancel routing")
        self.cancel_button.clicked.connect(self.cancel_route)
        layout.addWidget(self.cancel_button)
        self.status = label("Ready.")
        self.usage_label = label(muted=True)
        self.usage_label.hide()
        layout.addWidget(self.status)
        layout.addWidget(self.usage_label)
        self.setCentralWidget(central)
        self.refresh_badge()
        self.render_cards()
        self.refresh_actions()

    def set_mode_buttons(self, mode):
        for target, button in self.mode_buttons.items():
            button.setChecked(target == mode)

    def switch_mode(self, target):
        if target == self.mode:
            self.set_mode_buttons(target)
            return
        self.run_command("/autoroute" if target == Mode.ROUTING else "/autoroute_exit")

    def primary_action(self):
        """Ctrl+Enter: the highlighted step on the visible page. Never copper approval."""
        buttons = (self.route_button,) if self.pages.currentIndex() else (self.next_button, self.load_button)
        for button in buttons:
            if button.objectName() == "primary" and button.isEnabled() and button.isVisible():
                button.click()
                return

    def show_cost(self, text, details=""):
        self.cost_line.setText(text)
        self.cost_line.setToolTip(details)
        self.cost_line.setVisible(bool(text))

    def apply_theme(self, choice):
        if choice == "KiCad":
            choice = (saved_kicad_theme(*self.reader.version[:2]) if self.reader else None) or "System"
        self._dark = choice == "Dark" or (choice == "System" and QApplication.palette().window().color().lightness() < 128)
        tokens = theme_tokens(self._dark)
        palette = self.palette()
        for role, value in ((QPalette.ColorRole.Window, tokens["solid"]), (QPalette.ColorRole.Base, tokens["solid"]),
                            (QPalette.ColorRole.Text, tokens["fg"]), (QPalette.ColorRole.WindowText, tokens["fg"])):
            palette.setColor(role, QColor(value))
        self.setPalette(palette)
        self.setStyleSheet(glass_stylesheet(tokens))

    def refresh_badge(self):
        """The constraint list shown beside Generate; clicking Generate confirms it."""
        items = [item.description for item in self.constraints.items]
        self.constraint_line.setText("Constraints: " + ("; ".join(items) if items else "board rules only"))

    def note(self, text):
        """Informational result: hover/click/screen-reader text on the preview, not visible copy."""
        self.route_summary.hide()
        self.route_summary.setText("")
        self.canvas.setToolTip(text)
        self.canvas.setAccessibleName("Routing preview")
        self.canvas.setAccessibleDescription(text)

    def blocking(self, text, details=""):
        self.route_summary.setText(text)
        self.route_summary.setToolTip(details)
        self.route_summary.show()

    def show_error(self, message, details=""):
        self.status.setText("Stopped: " + message)
        self.status.setToolTip(details)
        self.status.setStyleSheet("color:#D45A67")

    def refresh_actions(self):
        busy = self.worker is not None
        # Audit needs only the AI provider (checked when used); routing needs the verified router.
        self.audit_page.setEnabled(not busy)
        self.route_page.setEnabled(not busy and (self.ready or self.demo))
        locked = not (self.ready or self.demo)
        self.setup_hint.setVisible(locked)
        self.setup_hint.setText("Routing locked: " + (self._setup_error or "Java 21 and Freerouting 2.1.0 "
                                "are not verified.") + " Click Setup.")
        for button in (*self.mode_buttons.values(), self.setup_button):
            gate(button, "Not available in the demo." if self.demo else "Busy." if busy else "")
        stage = self.audit.stage if self.audit else None
        ai_step = stage in {AuditStage.REVIEW_FUNCTIONS, AuditStage.CONFIRMED_FUNCTIONS,
                            AuditStage.CLASSIFICATION_ESTIMATE}
        # Explain with AI is an optional second pass over findings already shown.
        gate(self.next_button, "Not available in the demo." if self.demo else
             "Check a design first." if self.audit_snapshot is None else
             "" if stage is None or stage == AuditStage.FUNCTIONS or ai_step else "AI review done.")
        gate(self.price_button, "Not available in the demo." if self.demo else
             "Finish the AI review first." if stage != AuditStage.CLASSIFIED else
             "No flagged parts." if not self.audit.flags else "")
        gate(self.report_button, "Check a design first." if self.audit_snapshot is None else "")
        gate(self.annotations, "Not available in the demo." if self.demo else
             "Finish the AI review first." if stage != AuditStage.CLASSIFIED else
             "Needs the open PCB." if self.safety is None else "")
        set_primary(self.next_button, ai_step)
        set_primary(self.load_button, not ai_step)
        self.import_button.setVisible(self.ticket is not None)
        session = self.routing
        gate(self.route_button, "" if self.ready or session is not None else "Click Setup first.")
        self.cancel_button.setVisible(
            (busy and self._cancel is not None and not self._cancel.is_set())
            or (self._drc_pending > 0 and session is not None and session.stage == RoutingStage.VALIDATING))
        report = session.report if session is not None else None
        validated = report is not None and self.preview_shown and session.stage == RoutingStage.PREVIEW
        approvable = validated and report.drc_violations == 0
        gate(self.approve_button, "" if approvable else "DRC found errors." if validated else
             "Waiting for DRC." if session is not None and session.stage == RoutingStage.VALIDATING
             else "Generate a preview first.")
        gate(self.approve_anyway_button, "" if validated and report.drc_violations else
             "Only when DRC finds errors.")
        gate(self.reject_button, "" if session is not None and session.stage in {
            RoutingStage.PREVIEW, RoutingStage.SHORTFALL, RoutingStage.NEEDS_REASON, RoutingStage.VALIDATING}
            else "No preview to reject.")
        set_primary(self.route_button, not approvable)

    def check_design(self, snapshot):
        """Built-in rules plus BOM savings; the cached parts list is used only if downloaded."""
        def bom(snap):
            return bom_findings(snap, load_catalogue(self.config_dir))
        return run_rules(snapshot, providers=[bom])

    def export_report(self):
        name = Path(self.audit_snapshot.path or "design").stem or "design"
        path, _ = QFileDialog.getSaveFileName(self, "Save report", f"{name}-velatrace.html", "HTML (*.html)")
        if not path:
            return
        try:
            save_report(path, render_report(self.audit_snapshot, list(self.findings), version=__version__))
        except OSError as exc:
            self.show_error(f"Could not save report: {exc}")
            return
        self.status.setText("Report saved.")

    def download_parts(self):
        def operation(_):
            return download_catalogue(self.config_dir)
        def success(_):
            self.parts_button.setText("Update JLCPCB parts")
            self.status.setText("Parts list updated.")
            if getattr(self, "audit_snapshot", None) is not None:
                self.findings = self.check_design(self.audit_snapshot)
                self.render_cards()
        self.run_work("Downloading parts list", operation, success)

    def run_work(self, title, operation, success=None, failure=None):
        if self.worker is not None:
            return
        self.status.setStyleSheet("")
        self.status.setToolTip("Keep KiCad unchanged until this finishes.")
        self._work_title = title
        self._work_started = time.monotonic()
        self.update_work_status()
        self.work_timer.start()
        self.worker = self.executor
        self._success = success
        self._failure = failure
        self.refresh_actions()
        self.executor.jobs.put(operation)

    @Slot(object)
    def work_result(self, value):
        self.work_timer.stop()
        self.status.setText("Ready.")
        try:
            if self._success:
                self._success(value)
        except Exception as exc:
            self.show_error(str(exc))

    @Slot(str)
    def work_error(self, message):
        self.work_timer.stop()
        self.show_error(message)
        if self._failure:
            self._failure(message)

    @Slot(str)
    def work_progress(self, message):
        # Elapsed time is per stage; "Routing · pass 2" continues the "Routing" stage.
        if message.split(" · ")[0] != self._work_title.split(" · ")[0] and self._work_started is not None:
            self._work_started = time.monotonic()
        self._work_title = message
        self.update_work_status()

    @Slot()
    def update_work_status(self):
        if self._work_started is not None:
            elapsed = time.monotonic() - self._work_started
            self.status.setText(f"{self._work_title} · {elapsed:.0f}s")

    @Slot()
    def work_finished(self):
        self.work_timer.stop()
        self._work_started = None
        self.worker = None
        if not self.status.styleSheet():
            self.status.setToolTip("")
        self.refresh_actions()

    @Slot(object)
    def update_usage(self, usage: Usage):
        incoming = "pending" if usage.input_tokens is None else str(usage.input_tokens)
        outgoing = "pending" if usage.output_tokens is None else str(usage.output_tokens)
        calls = f" · calls {self.audit.provider.budget.used}/{self.audit.provider.budget.limit}" if self.audit else ""
        self.usage_label.setText(f"Tokens: {incoming} in · {outgoing} out{calls}")
        self.usage_label.setToolTip(f"Current response · {usage.text_characters} characters received · {usage.source}")
        self.usage_label.show()

    def configure(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.apply_settings(dialog.value())

    def apply_settings(self, settings, *, save=True):
        self.settings = settings  # Reopening Setup after a failure shows what was typed.
        # An AI audit owns its provider and key. Retire it as soon as new settings
        # are accepted, even if independent router verification later fails.
        # Local rule findings stay: they never depended on the provider.
        # Keep the safety handle until cleanup succeeds so owned graphics remain
        # recoverable when KiCad is unavailable.
        self.reset_ai()
        if save:
            try:
                settings.save(self.config_dir / "settings.json")
            except OSError:
                pass  # Remembering settings is a convenience; verification still runs.
        self._setup_error = ""
        def operation(_):
            if self.safety:
                self.safety.clear_preview()
            try:
                router = Freerouting(Path(settings.jar), settings.java, work_directory=self.config_dir / "router-work",
                                     warm=settings.warm_router, forbidden=self._project_folders())
                router.check_startup()
                self._remember_tool("java", router.java)
            except Exception as exc:
                self._setup_error = str(exc) or type(exc).__name__
                raise
            return router
        def success(router):
            if self.router is not None:
                self.router.close()  # Stop the replaced router's warm JVM.
            self.settings, self.router, self.ready = settings, router, True
            self.audit = self.pricing = self.routing = None
            self.reader = self.safety = self.validator = self.writer = None
            self.ticket = self.snapshot = None
            self.description.setReadOnly(False)
            self.render_cards()
            self.status.setText("Router ready.")
        self.ready = False
        self.run_work("Checking router", operation, success)

    def authorize_provider(self):
        config = self.audit.provider.config if self.audit else self.settings.provider_config()
        if not self.consent.accepted(config):
            if not ask(self, "Before first analysis", disclosure_text(config)):
                return False
            self.consent.accept(config)
        return True

    def make_audit(self):
        config = self.settings.provider_config()
        tokenizer = None
        if self.settings.tokenizer:
            tokenizer = LocalChatTokenizer(Path(self.settings.tokenizer), config.model, self.settings.verified_model)
        provider = Provider(config, CallBudget(self.settings.cap), tokenizer,
                            disclosure_gate=self.consent.accepted)
        return AuditSession(provider)

    def reset_ai(self):
        """Drop the optional AI pass (and its provider/key); keep local findings."""
        self.audit = self.pricing = None
        self._offered_pricing = None
        self.description.setReadOnly(False)
        self.next_button.setText(EXPLAIN)
        self.show_cost("")
        self.show_findings_summary()
        self.render_cards()

    def show_findings_summary(self):
        self.totals.setText(summarize(self.findings) if self.audit_snapshot else "")
        self.totals.setVisible(self.audit_snapshot is not None)

    def load_design(self):
        """Read connectivity and run the local checks. No provider, key or consent."""
        choice = self.source.currentIndex()
        path = None
        if choice:
            selected, _ = QFileDialog.getOpenFileName(self, "Select saved connectivity source", "",
                "KiCad schematic (*.kicad_sch)" if choice == 1 else "KiCad XML netlist (*.xml *.net)")
            if not selected:
                return
            path = Path(selected)
        def operation(_):
            if self.safety:
                self.safety.clear_preview()
            reader = safety = None
            if choice == 0:
                reader = KiCadReader.connect()
                snapshot = reader.read_board()
                if snapshot.path:
                    safety = BoardSafety(reader.client.get_board(), snapshot.path, journal_dir=self.config_dir / "journals")
            elif choice == 1:
                # Picking the saved file is the confirmation; the step line says so.
                snapshot = KiCadCli(self.settings.cli, forbidden=(path.parent,)).schematic_snapshot(path, saved_confirmed=True)
            else:
                snapshot = read_xml_netlist(path)
            # Logos, fiducials and unconnected holes (often REF** or duplicate refs)
            # carry no connectivity; they must not block the checks.
            snapshot = replace(snapshot, components=tuple(c for c in snapshot.components if c.nets))
            require_connectivity(snapshot)
            return snapshot, self.check_design(snapshot), reader, safety
        def success(value):
            self.audit_snapshot, self.findings, self.reader, self.safety = value
            self.apply_theme(self.theme.currentText())
            self.pricing = self.routing = None
            self.ticket = None
            count = len(self.audit_snapshot.components)
            self.audit_step.setText(f"{count} parts checked" + (" · saved file" if choice else ""))
            self.audit_step.setToolTip("Checked on this computer; nothing was sent."
                                       + (" Unsaved edits are not included." if choice else ""))
            self.reset_ai()
        self.run_work("Checking design", operation, success)

    def restart_audit(self):
        def operation(_):
            if self.safety:
                self.safety.clear_preview()
        def done(_):
            self.audit_snapshot, self.findings = None, []
            self.audit_step.setText(READY_STEP)
            self.audit_step.setToolTip("")
            self.reset_ai()
        self.run_work("Resetting", operation, done)

    def start_explain(self):
        """Optional second pass: the model explains the findings and each part's role."""
        if self.audit_snapshot is None:
            return
        try:
            audit = self.make_audit()
        except ValidationError as exc:
            self.show_error("AI needs a provider and model. Open Setup.", str(exc))
            return
        self.audit = audit
        if not self.authorize_provider():
            self.audit = None
            return
        audit.set_description(self.description.toPlainText().strip() or AI_DEFAULT_DESCRIPTION)
        audit.load_design(self.audit_snapshot, tuple(self.findings))
        self.description.setReadOnly(True)
        self.audit_next()

    def audit_next(self):
        try:
            if not self.audit:
                self.start_explain()
                return
            stage = self.audit.stage
            if stage == AuditStage.FUNCTIONS:
                if not self.authorize_provider():
                    return
                def done(_):
                    self.audit_step.setText("Review each function and role, then confirm.")
                    self.next_button.setText("Confirm functions")
                    self.render_cards()
                self.run_work("Inferring functions", lambda usage: self.audit.infer_functions(usage, self.settings.output_cap), done)
            elif stage == AuditStage.REVIEW_FUNCTIONS:
                for reference, card in self.cards.items():
                    old = self.audit.functions[reference]
                    if (card.function_edit.text(), card.role_edit.text()) != (old.text, old.role):
                        self.audit.correct_function(reference, card.function_edit.text(), card.role_edit.text())
                self.audit.confirm_functions()  # Clicking "Confirm functions" is the confirmation.
                self.prepare_classification()
            elif stage == AuditStage.CLASSIFICATION_ESTIMATE and self.audit.estimate is not None:
                # The estimate is on screen beside this button; the click is the consent.
                self.start_classification(self.audit.estimate.confirmation_fingerprint)
            else:
                self.prepare_classification()
        except Exception as exc:
            self.show_error(str(exc))

    def prepare_classification(self):
        if not self.authorize_provider():
            return
        def done(estimate):
            self.render_cards()
            self.next_button.setText("Classify parts")
            self.audit_step.setText("Functions confirmed.")
            remaining = self.audit.provider.budget.remaining
            self.show_cost(f"Sends ≈{estimate.input_tokens:,} tokens · ≤{estimate.output_cap:,} out · "
                           f"≤{estimate.calls_max} calls ({remaining} left)",
                           f"Method: {estimate.method}. Up to two retries. The hard call cap applies.")
        self.run_work("Estimating tokens", lambda _: self.audit.estimate_classification(self.settings.output_cap), done)

    def start_classification(self, fingerprint):
        if self.worker is not None:
            QTimer.singleShot(30, lambda: self.start_classification(fingerprint))
            return
        try:
            if not self.authorize_provider():
                return
        except Exception as exc:
            self.show_error(str(exc))
            return
        def done(_):
            self.audit_step.setText(f"AI review done · {len(self.audit.flags)} flagged.")
            self.render_cards()
            self.offer_pricing()
            if self.safety:
                QTimer.singleShot(0, self.show_annotations)
        self.run_work("Classifying parts", lambda usage: self.audit.classify(fingerprint, usage), done)

    def offer_pricing(self):
        """Show the pricing estimate beside its button; clicking the button is the consent."""
        self._offered_pricing = None
        self.show_cost("")
        if not self.audit or self.audit.stage != AuditStage.CLASSIFIED or not self.audit.flags:
            return
        try:
            estimate = PricingSession(self.audit).prepare()
        except Exception as exc:
            self.show_error(str(exc))
            return
        self._offered_pricing = estimate.fingerprint
        remaining = self.audit.provider.budget.remaining
        self.show_cost(f"Pricing {estimate.flagged_count} flagged: ≤{estimate.max_http_calls} calls · "
                       f"~{estimate.visible_prompt_token_upper_estimate:,} tokens ({remaining} left)"
                       if estimate.max_http_calls else
                       f"Pricing {estimate.flagged_count} flagged: local estimates, no calls",
                       f"~{estimate.searches_low}–{estimate.searches_high} searches. Output cap "
                       f"{estimate.output_cap_each} each. {estimate.note}")

    def can_zoom(self):
        """True once zoom-to-part exists; until then finding cards show no button."""
        return False

    def zoom_to_part(self, refs):
        """Hook: select and zoom KiCad's PCB editor to these references (next feature)."""
        self.status.setText("Zoom to part is not available yet: " + ", ".join(refs))

    def clear_cards(self):
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().hide()  # deleteLater alone can leave one stale paint
                item.widget().deleteLater()
        self.cards = {}
        self.finding_cards = []

    def render_findings(self):
        if not self.findings:
            self.card_layout.addWidget(label("No issues found.", muted=True))
            return
        locate = self.zoom_to_part if self.can_zoom() else None
        for severity, heading in SEVERITY_GROUPS:
            rows = [item for item in self.findings if item.severity == severity]
            if not rows:
                continue
            header = label(f"{heading} · {len(rows)}")
            header.setStyleSheet(f"color:{SEVERITY_COLORS[severity]}; font-weight:600")
            self.card_layout.addWidget(header)
            for finding in rows:
                card = FindingCard(finding, locate)
                self.finding_cards.append(card)
                self.card_layout.addWidget(card)

    def render_cards(self):
        classified = bool(self.audit and self.audit.stage == AuditStage.CLASSIFIED)
        self.next_button.setVisible(not classified)
        self.price_button.setVisible(classified)
        self.annotations.setVisible(classified)
        # The description feeds only the AI pass; show it until that pass starts.
        self.description.setVisible(self.audit_snapshot is not None and self.audit is None)
        self.clear_cards()
        if self.audit_snapshot is not None:
            self.render_findings()
        if self.audit and self.audit.snapshot and self.audit.stage != AuditStage.FUNCTIONS:
            self.card_layout.addWidget(label("AI review", muted=True))
            flags = {item.reference: item for item in self.audit.flags}
            for component in self.audit.snapshot.components:
                reference = component.reference
                card = ComponentCard(component, self.audit.functions.get(reference),
                    self.audit.verdicts.get(reference), self.pricing.prices.get(reference) if self.pricing else None,
                    flags.get(reference), self.audit.stage == AuditStage.REVIEW_FUNCTIONS)
                self.cards[reference] = card
                self.card_layout.addWidget(card)
        self.card_layout.addStretch()

    def price_parts(self):
        try:
            if not self.authorize_provider():
                return
            pricing = PricingSession(self.audit)
            estimate = pricing.prepare()
            if estimate.fingerprint != self._offered_pricing:
                # What would run differs from the line on screen: show the new one, wait for a click.
                self.offer_pricing()
                self.status.setText("Pricing estimate updated; click again to start.")
                return
            def done(_):
                self.pricing = pricing
                self.render_cards()
                self.totals.setText((summarize(self.findings) + "\n" if self.audit_snapshot else "")
                                    + f"Flagged ${pricing.flagged_cost:.2f} · possible savings "
                                    f"${pricing.hypothetical_savings:.2f} (estimates)")
                self.totals.show()
                if pricing.failures:
                    self.show_error(pricing.failure_summary)
                    self.totals.setText(self.totals.text() + "\n" + pricing.failure_summary)
            self.run_work("Pricing flagged parts", lambda usage: pricing.run(estimate.fingerprint, usage), done)
        except Exception as exc:
            self.show_error(str(exc))

    def show_annotations(self):
        if self.worker is not None:
            QTimer.singleShot(30, self.show_annotations)
            return
        if not self.safety or not self.audit or not self.audit.verdicts:
            return
        rows = [(f"{comp.reference}: {self.audit.verdicts[comp.reference].bucket.value}",
                 comp.position_mm[0] + 2, comp.position_mm[1] + 2)
                for comp in self.audit.snapshot.components if comp.position_mm]
        self.run_work("Annotating board", lambda _: self.safety.show_annotations(rows),
                      lambda _: self.status.setText("Ready."))

    def open_constraints(self):
        ConstraintsDialog(self).exec()
        self.refresh_actions()

    def invalidate_visible_preview(self):
        self.preview_shown = False
        self.canvas.plan = None
        self.canvas.update()
        self.note("Constraints changed; reroute.")
        self.refresh_badge()
        self.refresh_actions()

    def run_command(self, command):
        try:
            if command not in {"/autoroute", "/autoroute_exit"}:
                raise ValidationError("Use /autoroute or /autoroute_exit.")
            target = Mode.ROUTING if command == "/autoroute" else Mode.AUDIT
            self.mode = target
            self.set_mode_buttons(target)
            self.pages.setCurrentIndex(1 if target == Mode.ROUTING else 0)
            self.preview_shown = False
            def operation(_):
                if self.safety:
                    self.safety.clear_preview()
                if self.routing:
                    self.routing.command(command)
            self.run_work("Switching mode", operation)
        except Exception as exc:
            self.set_mode_buttons(self.mode)
            self.show_error(str(exc))

    def propose(self):
        if not self.route_prompt.text().strip():
            return
        try:
            # The interpreted rule appears in the constraint line beside Generate.
            item = propose_constraint(self.route_prompt.text())
            if self.routing:
                self.routing.add_constraint(item)
            else:
                self.constraints.add(item)
            self.invalidate_visible_preview()
            self.route_prompt.clear()
            self.status.setStyleSheet("")
            self.status.setText("Rule added: " + item.description)
        except Exception as exc:
            self.show_error(str(exc))

    def _project_folders(self):
        return (self.snapshot.path.parent,) if self.snapshot is not None and self.snapshot.path else ()

    def _remember_tool(self, name, path):
        """Keep the verified absolute path, so later launches never search PATH again."""
        if self.demo or path is None or getattr(self.settings, name) == str(path):
            return
        setattr(self.settings, name, str(path))
        try:
            self.settings.save(self.config_dir / "settings.json")
        except OSError:
            pass  # A convenience; the next launch resolves and verifies the tool again.

    def prepare_board(self, cancel):
        """Worker thread: read the open board, pre-flight it and export its DSN with
        KiCad's own exporter. Nothing is saved. Returns (canonical snapshot of the
        exported board text, pre-flight notes); the preview must match that snapshot."""
        reader = KiCadReader.connect()
        snapshot = reader.read_board()
        if not snapshot.path:
            raise ValidationError("Save the board once in KiCad so VelaTrace knows its project folder.")
        board = reader.client.get_board()
        if self.routing is None or self.snapshot is None or self.snapshot.path != snapshot.path:
            cli = KiCadCli(self.settings.cli, forbidden=(snapshot.path.parent,))
            cli.require_editor_version(reader.version)
            self._remember_tool("cli", cli.executable)
            if self.safety is None or self.safety.path != snapshot.path.resolve():
                self.safety = BoardSafety(board, snapshot.path, journal_dir=self.config_dir / "journals")
            self.validator = SafeCandidateValidator(self.safety, cli)
            self.routing = RoutingSession(self.constraints, self.router, self.validator, repair_dangling)
            self.routing.command("/autoroute")
            self.writer = SafeBoardWriter(self.safety, self.validator)
        self.safety.board = board  # A fresh IPC handle; owned preview items carry over.
        self.reader, self.snapshot = reader, snapshot
        # Remove old previews first: the exported text must not contain them.
        self.safety.prepare_preview()
        text = board.get_as_string()
        root = parse(text, kicad=True)
        project, _ = project_context(snapshot.path)
        problems, self._preflight_notes = preflight(root, snapshot, project)
        if problems:
            raise ValidationError("\n".join(problems))
        if cancel.is_set():
            raise RoutingCancelled(CANCELLED)
        if self._dsn_folder:
            shutil.rmtree(self._dsn_folder, ignore_errors=True)
        (self.config_dir / "dsn-export").mkdir(parents=True, exist_ok=True)
        self._dsn_folder = tempfile.mkdtemp(prefix="board-", dir=self.config_dir / "dsn-export")
        try:
            dsn = export_live(self.validator.cli, text, snapshot, Path(self._dsn_folder), cancel)
        except ExportUnavailable as exc:
            self.ticket = ExportTicket.begin(snapshot.path)  # Starts the manual fallback.
            raise ExportUnavailable(f"Automatic DSN export is unavailable: {exc} Export it from KiCad "
                                    "(File > Export > Specctra DSN), then click ‘Load DSN exported from KiCad…’.") from None
        self.routing.set_input(dsn, all_footprints_placed=True)  # Pre-flight replaced the checkbox.
        return canonical(root), self._preflight_notes

    def route(self):
        self.ticket = None
        self.start_routing(self.prepare_board)

    def load_dsn(self):
        """Manual fallback: a DSN the user exported from KiCad after the failed attempt."""
        selected, _ = QFileDialog.getOpenFileName(self, "Specctra DSN exported from KiCad", "", "Specctra DSN (*.dsn)")
        if not selected:
            return
        if not ask(self, "Confirm fresh export", "This DSN was exported from KiCad after VelaTrace asked for it, "
                   "and the board was not edited since. Confirm?"):
            return
        ticket, snapshot = self.ticket, self.snapshot
        def prepare(_):
            self.safety.prepare_preview()
            dsn = accept_export(ticket, Path(selected), snapshot, user_confirms_saved_and_exported=True)
            self.routing.set_input(dsn, all_footprints_placed=True)
            return None, self._preflight_notes
        self.start_routing(prepare)

    def start_routing(self, prepare):
        """Route board: prepare (export), Freerouting, then the User.9 preview; DRC follows."""
        try:
            if self.route_prompt.text().strip():
                raise ValidationError("Add or clear the pending rule first.")
            if self.routing is not None and self.routing.stage == RoutingStage.NEEDS_REASON:
                raise ValidationError("Enter a reject reason and click Reject first.")
            # The full constraint list is shown beside this button; the click confirms it.
            fingerprint = self.constraints.fingerprint
            cancel = self._cancel = threading.Event()
            self.preview_shown = False
            def operation(_):
                emit = self.executor.progress.emit
                started = time.monotonic()
                expected, notes = prepare(cancel)
                exported = time.monotonic() - started
                session = self.routing
                session.confirm_constraints(fingerprint)
                session.progress = session.router.progress = emit
                session.router.cancel = cancel
                plan = session.route(trusted_via_catalog(session.input))
                generation = session.generation
                emit("Importing the route and drawing the preview")
                started = time.monotonic()
                # Bound to the exported board: an edit since the click refuses the preview.
                snapshot = self.safety.show_preview(session.input, plan, expected)
                if cancel.is_set():
                    session.cancel()
                    self.safety.clear_preview()
                    raise RoutingCancelled(CANCELLED)
                return plan, generation, snapshot, exported, time.monotonic() - started, notes
            def done(value):
                self._cancel = None
                plan, generation, snapshot, exported, preview_seconds, notes = value
                self.preview_shown = True
                self._route_timing = (f"Export {exported:.1f}s; Routing {self.routing.timings['router']:.1f}s; "
                                      f"preview {preview_seconds:.1f}s")
                self.note(
                    f"{plan.trace_count} traces · {len(plan.vias)} vias · {', '.join(plan.layers_used)}. "
                    f"{self._route_timing}. Preview only ({self.safety.preview_layer_name}). Checking DRC…" + "".join(" " + note for note in notes))
                self.canvas.plan = plan
                self.canvas.update()
                self.check_drc(self.routing, self.validator, plan, generation, snapshot)
                if cancel.is_set():  # Clicked after the last in-worker check.
                    QTimer.singleShot(0, self.cancel_route)
            def failed(message):
                self._cancel = None
                self.blocking(message)
            self.run_work("Exporting the board", operation, done, failed)
        except Exception as exc:
            self.show_error(str(exc))

    def cancel_route(self):
        """Stop the router (its process is killed) or discard a preview still in DRC."""
        if self.worker is not None and self._cancel is not None:
            self._cancel.set()
            self.status.setText("Cancelling…")
        elif self.routing is not None and self.routing.stage == RoutingStage.VALIDATING:
            self.routing.cancel()
            self.preview_shown = False
            self.canvas.plan = None
            self.canvas.update()
            self.run_work("Removing the cancelled preview", lambda _: self.safety.clear_preview(),
                          lambda _: self.note("Cancelled; the board is unchanged."))
        self.refresh_actions()

    def check_drc(self, session, validator, plan, generation, snapshot):
        """Candidate DRC off the Qt thread; drc_finished discards a stale result."""
        def operation(_):
            try:
                report = session.check(plan)
                if validator.evidence is None or validator.evidence[5] != snapshot:
                    raise ValidationError("The board changed after the preview was drawn. Generate a new routing preview.")
                return session, plan, generation, report, ""
            except Exception as exc:
                return session, plan, generation, None, str(exc) or type(exc).__name__
        self._drc_pending += 1
        self.status.setStyleSheet("")
        self.status.setText("Checking DRC…")
        self.status.setToolTip("Approve unlocks when it finishes. Keep KiCad unchanged.")
        self.drc_executor.jobs.put(operation)

    @Slot(object)
    def drc_finished(self, value):
        self._drc_pending -= 1
        session, plan, generation, report, error = value
        # A reroute, rejection, mode or input change bumps the generation: discard.
        if session is not self.routing or not session.accept(plan, report, generation):
            return
        if report is None:
            self.preview_shown = False
            self.canvas.plan = None
            self.canvas.update()
            message = "DRC could not validate this route: " + error
            self.show_error(message)
            self.blocking(message, "This route cannot be approved. Generate a new routing preview.")
            self.run_work("Clearing preview", lambda _: self.safety.clear_preview(),
                          lambda _: self.show_error(message))
        else:
            timing = f"{self._route_timing}; DRC {session.timings['validation']:.1f}s."
            if session.stage == RoutingStage.PREVIEW and report.drc_violations == 0:
                self.note(f"{session.summary} {timing} Preview only ({self.safety.preview_layer_name}). "
                          "Approve and apply copper, or reject.")
            else:  # approval blocked: the reason stays visible
                self.blocking(session.summary + (" Reject and reroute, or Approve anyway."
                                                 if session.stage == RoutingStage.PREVIEW else " Reject and reroute."),
                              timing + f" Preview only: {self.safety.preview_layer_name} graphics do not change copper.")
            if self.worker is None:
                self.status.setText("DRC finished.")
                self.status.setToolTip("")
        self.refresh_actions()

    def reject_route(self):
        try:
            self.routing.reject(self.reason.text())
            self.preview_shown = False
            def done(_):
                self.canvas.plan = None
                self.canvas.update()
                self.note("Rejected: " + self.routing.rejection_reason + ".")
            self.run_work("Clearing preview", lambda _: self.safety.clear_preview(), done)
        except Exception as exc:
            self.show_error(str(exc))

    def approve_route(self):
        if not self.preview_shown:
            self.show_error("Generate a preview first.")
            return
        # The click is the confirmation: one backed-up commit that a single KiCad Undo reverts.
        self.apply_route(drc_override=False)

    def approve_anyway_route(self):
        """Explicit override of the DRC gate only; every other approval check still applies."""
        report = self.routing.report if self.routing else None
        if not self.preview_shown or report is None or not report.drc_violations:
            self.show_error("Approve anyway applies only to a preview blocked by DRC errors.")
            return
        reasons = report.blocking_reasons or ("DRC issue details unavailable; review the full KiCad DRC report",)
        listed = "\n".join(f"• {reason}" for reason in reasons[:5])
        if len(reasons) > 5:
            listed += f"\n• … and {len(reasons) - 5} more"
        text = (f"KiCad DRC found {report.drc_violations} blocking issue(s):\n{listed}\n\n"
                "Write the copper DESPITE these errors? It is one backed-up commit that a single Undo "
                "reverts; the override is logged in the backup journal. Fix them before manufacturing.")
        if not ask(self, "Approve despite DRC errors", text):
            return
        self.apply_route(drc_override=True)

    def apply_route(self, *, drc_override):
        def operation(_):
            self.executor.progress.emit("Applying copper to KiCad")
            started = time.monotonic()
            self.routing.approve(self.writer, drc_override=drc_override)
            return time.monotonic() - started
        def done(elapsed):
            self.preview_shown = False
            self.canvas.plan = None
            self.canvas.update()
            plan = self.routing.plan
            override = (f" Applied despite {self.routing.report.drc_violations} DRC issue(s); the override is "
                        "recorded in the backup journal." if drc_override else "")
            self.note(f"Applied {plan.trace_count} copper track segments and {len(plan.vias)} vias "
                f"on {', '.join(plan.layers_used)} in {elapsed:.1f}s, as one KiCad commit.{override} "
                "Refresh before further routing, including after Undo.")
            self.status.setText("Copper applied. Press B to refill any copper pours, review, then save in KiCad (Undo reverts it).")
        def failed(message):
            self.blocking("Copper application was not confirmed; check KiCad before retrying. " + message,
                          "The panel keeps the previous preview; the live outcome may be uncertain.")
        self.run_work("Applying copper", operation, done, failed)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._positioned:
            # Content can make the window wider than requested; place it by its real frame.
            self._positioned = True
            screen = self.screen().availableGeometry()
            frame = self.frameGeometry()
            self.move(max(screen.left(), screen.right() - frame.width() + 1), screen.top() + 30)

    def closeEvent(self, event):
        if self.worker is not None or self._drc_pending:
            event.ignore()
            self.show_error("Wait for the current task to finish, then close.")
            return
        if self.safety and not self._cleanup_failed:
            event.ignore()
            def done(_):
                self.safety = None
                QTimer.singleShot(0, self.close_after_cleanup)
            def failed(message):
                # Never trap the window: the next close leaves without cleanup.
                self._cleanup_failed = True
                self.show_error(message + " Close again to exit, then delete leftover VelaTrace graphics in KiCad.")
            self.run_work("Cleaning up", lambda _: self.safety.clear_preview(), done, failed)
            return
        self.safety = None
        for worker in (self.executor, self.drc_executor):
            worker.jobs.put(None)
            worker.wait(2000)
        if self.router is not None:
            self.router.close()  # No java.exe outlives the window.
        if self._dsn_folder:
            shutil.rmtree(self._dsn_folder, ignore_errors=True)  # The exported board copy.
        event.accept()

    def close_after_cleanup(self):
        if self.worker is not None:
            QTimer.singleShot(30, self.close_after_cleanup)
        else:
            self.close()

    def load_demo(self):
        self.description.setPlainText("Battery-powered environmental monitor with a status LED and two temperature sensors on a shared bus.")
        self.banner.show()
        self.banner.setText("Demo · synthetic board, nothing is sent or written")
        self.settings.model = "demo"
        sensor = (Pin("1", "+3V3", "V+"), Pin("2", "GND", "GND"), Pin("3", "SDA", "SDA"), Pin("4", "SCL", "SCL"))
        components = (
            Component("U1", "RP2040", "Package_DFN_QFN:QFN-56", (
                Pin("1", "+3V3", "IOVDD"), Pin("2", "GND", "GND"), Pin("3", "SDA", "GPIO4"),
                Pin("4", "SCL", "GPIO5"), Pin("5", "STATUS", "GPIO25"), Pin("6", "+1V1", "DVDD")),
                position_mm=(20, 20)),
            Component("C1", "100nF", "Capacitor_SMD:C_0402", (Pin("1", "+3V3"), Pin("2", "GND")), position_mm=(22, 20)),
            Component("D1", "Green", "LED_SMD:LED_0603", (Pin("1", "GND", "K"), Pin("2", "STATUS", "A")), position_mm=(30, 20)),
            Component("R1", "4.7k", "Resistor_SMD:R_0402", (Pin("1", "+3V3"), Pin("2", "SDA")), position_mm=(26, 24)),
            Component("R2", "4.7k", "Resistor_SMD:R_0402", (Pin("1", "+3V3"), Pin("2", "SCL")), position_mm=(27, 24)),
            Component("R3", "4.7k", "Resistor_SMD:R_0402", (Pin("1", "+3V3"), Pin("2", "SDA")), position_mm=(36, 24)),
            Component("U2", "TMP102", "Package_TO_SOT_SMD:SOT-563", sensor, position_mm=(35, 20)),
            Component("U3", "TMP102", "Package_TO_SOT_SMD:SOT-563", sensor, position_mm=(40, 20)))
        self.audit_snapshot = DesignSnapshot(components, "demo")
        self.findings = self.check_design(self.audit_snapshot)
        self.audit = self.make_audit()
        self.audit.set_description(self.description.toPlainText())
        self.audit.load_design(self.audit_snapshot, tuple(self.findings))
        self.description.setReadOnly(True)
        descriptions = ["Runs the sensing and reporting loop", "Decouples the MCU supply",
                        "Shows device activity at a glance", "SDA pull-up", "SCL pull-up",
                        "Second SDA pull-up", "Measures board temperature", "Measures the same temperature as U2"]
        buckets = list(Bucket) * 2
        for comp, text, bucket in zip(components, descriptions, buckets):
            self.audit.functions[comp.reference] = Function(text, text, .95)
            self.audit.verdicts[comp.reference] = Verdict(comp.reference, bucket, .95,
                "Review the schematic and intended operating conditions before making a design change.")
        self.audit.stage = AuditStage.CLASSIFIED
        self.pricing = PricingSession(self.audit)
        self.pricing.prices = {comp.reference: estimate_price(comp) for comp in components[5:]}
        self.audit_step.setText(f"{len(components)} parts checked")
        self.show_findings_summary()
        self.render_cards()
        self.refresh_actions()
        self.pages.setEnabled(True)
        self.load_button.setEnabled(False)
        self.restart_button.setEnabled(False)


def launch(*, demo=False, screenshot: Path | None = None):
    if screenshot and not demo:
        raise ValidationError("Screenshot rendering is available only with explicit --demo.")
    app = QApplication.instance() or QApplication([])
    # The Windows offscreen Qt platform does not enumerate installed fonts.
    # Loading an existing system font also makes exported QA renders readable.
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
    app.setApplicationName("VelaTrace")
    app.setOrganizationName("F-Blaze")
    window = MainWindow(demo=demo)
    window.show()
    window.raise_()
    window.activateWindow()
    if screenshot:
        def render():
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(screenshot)):
                raise OSError("Could not save demo screenshot.")
            window.close()
            app.quit()
        QTimer.singleShot(200, render)
    return app.exec()
