"""Single external IPC companion window. All board writes use BoardSafety.

Qt widgets are confined to the main thread. A single serialized worker owns each
blocking service operation; it reports plain data through queued signals.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import os
import time
import uuid
from queue import Queue

from PySide6.QtCore import Qt, QThread, Signal, Slot, QStandardPaths, QTimer
from PySide6.QtGui import QCursor, QColor, QPainter, QPen, QFontDatabase, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QToolTip,
    QTextEdit, QVBoxLayout, QWidget,
)

from .audit import AuditSession, AuditStage, require_connectivity
from .audit_rules import run_rules, summarize
from .candidate import SafeCandidateValidator, trusted_via_catalog
from .constraints import Constraint, ConstraintStore, Scope, propose_constraint
from .dsn import ExportTicket, accept_export
from .errors import ValidationError
from .findings import Finding, Severity
from .flags import Bucket, Function, Verdict
from .freerouting import Freerouting
from .ipc import KiCadReader
from .kicad_cli import KiCadCli
from .models import Component, DesignSnapshot, Pin
from .netlist import read_xml_netlist
from .pricing import PricingSession, estimate_price
from .privacy import ConsentStore, PROVIDER_NOTE, disclosure_text
from .provider import CallBudget, Provider, ProviderConfig, Usage
from .routing import Mode, RoutingSession, RoutingStage
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
        QLabel#title {{font-size:26px; font-weight:700; color:{t['fg']}}}
        QLabel#mode {{color:white; font-weight:700; padding:2px 10px; border-radius:9px; background:{gloss}}}
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
        self.resize(650, 680)
        layout = QVBoxLayout(self)
        layout.addWidget(label("Local tools and your provider", muted=False))
        layout.addWidget(label("No backend or telemetry. Only your configured endpoint receives remote API requests. Tool paths and provider choices are saved locally; keys stay in memory for this launch.", muted=True))
        layout.addWidget(label("Groq or Gemini offer free tiers, subject to current quotas and terms. " + PROVIDER_NOTE, muted=True))
        layout.addWidget(label(f"Local config folder: {parent.config_dir}", muted=True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        self.fields = {}
        names = [("jar", "Freerouting 2.1.0 JAR"), ("java", "Java 21 executable"),
                 ("cli", "KiCad CLI executable"), ("name", "Provider name"),
                 ("endpoint", "HTTPS API base URL"), ("model", "Exact model ID"),
                 ("key", "API key (memory only)"), ("search_model", "Search model ID (optional)"),
                 ("tokenizer", "Local tokenizer folder (OpenAI protocol)"),
                 ("verified_model", "Verified tokenizer model ID")]
        for attr, title in names:
            field = QLineEdit(getattr(settings, attr))
            if attr == "key":
                field.setEchoMode(QLineEdit.EchoMode.Password)
            self.fields[attr] = field
            form.addRow(title, field)
        self.protocol = QComboBox()
        self.protocol.addItems(["gemini", "openai"])
        self.protocol.setCurrentText(settings.protocol)
        form.addRow("Protocol", self.protocol)
        self.search = QCheckBox("Enable provider built-in web search")
        self.search.setChecked(settings.search)
        def update_search(protocol):
            available = protocol != "gemini"
            self.search.setEnabled(available)
            if not available:
                self.search.setChecked(False)
        self.protocol.currentTextChanged.connect(update_search)
        update_search(self.protocol.currentText())
        form.addRow(self.search)
        form.addRow(label("Gemini web search is disabled until its required grounded-result and Search Suggestion display is supported. Gemini pricing uses local estimates; ordinary analysis remains available.", muted=True))
        self.cap = QSpinBox()
        self.cap.setRange(1, 1000)
        self.cap.setValue(settings.cap)
        form.addRow("Hard API calls per audit", self.cap)
        self.output_cap = QSpinBox()
        self.output_cap.setRange(1, 65536)
        self.output_cap.setValue(settings.output_cap)
        form.addRow("Output token cap per analysis", self.output_cap)
        self.warm_router = QCheckBox("Keep Freerouting running between routes (faster)")
        self.warm_router.setChecked(settings.warm_router)
        form.addRow(self.warm_router)
        form.addRow(label("Opt-in: runs VelaTrace's MIT launcher and the GPLv3 Freerouting JAR in one Java process. Off starts a fresh Freerouting per route.", muted=True))
        scroll.setWidget(content)
        layout.addWidget(scroll)
        layout.addWidget(label("Gemini uses exact countTokens. Groq/OpenAI-compatible models require a local tokenizer whose chat template you have verified against that exact provider/model. Built-in search must be supported by your selected provider/model.", muted=True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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
        self.price_label = label(f"${price.amount:.2f}" + (" est." if price.estimated else "") if price else "—")
        self.price_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self.price_label)
        body.addLayout(row)
        body.addWidget(label(bucket.upper(), muted=True))
        self.function_edit = QLineEdit(function.text if function else "Function not inferred yet")
        self.role_edit = QLineEdit(function.role if function else "")
        if editable:
            body.addWidget(self.function_edit)
            self.role_edit.setPlaceholderText("Specific electrical role")
            body.addWidget(self.role_edit)
        else:
            body.addWidget(label(function.text if function else "Awaiting function inference", muted=True))
        if flag:
            body.addWidget(label(flag.reason))
        self.details = label("\n\n".join(text for text in [
            verdict.suggestion if verdict else "Confirm or correct the function before classification.",
            price.note if price else "", price.source if price and price.source else "",
            f"Footprint: {component.footprint}"] if text))
        self.details.setVisible(False)
        expand = QPushButton("Show suggestion +" if verdict else "Details +")
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
        self.title = label(finding.title)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        body.addWidget(self.title)
        cost = ""
        if finding.cost_delta is not None:
            sign = "−" if finding.cost_delta < 0 else "+"
            cost = f"Cost: {sign}${abs(finding.cost_delta):.2f}/board (illustrative)"
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
            self.details.setVisible(not self.details.isVisible())
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
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Validated route preview appears here")
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
        self.setWindowTitle("Session and universal constraints")
        self.resize(650, 500)
        layout = QVBoxLayout(self)
        layout.addWidget(label("Session constraints stack across retries. Universal constraints are saved locally.", muted=True))
        self.items = QListWidget()
        layout.addWidget(self.items)
        form = QFormLayout()
        self.scope = QComboBox()
        self.scope.addItems(["session", "universal"])
        self.kind = QComboBox()
        self.kind.addItems(["clearance", "trace-width", "header-clearance"])
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
        add = QPushButton("Add numeric constraint")
        remove = QPushButton("Remove selected")
        add.clicked.connect(self.add)
        remove.clicked.connect(self.remove)
        row.addWidget(add)
        row.addWidget(remove)
        layout.addLayout(row)
        layout.addWidget(label("Current router supports all-net clearance and trace width. Header/per-net rules remain visible but stop routing until supported; they are never ignored.", muted=True))
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


READY_STEP = "Check the open PCB, a saved schematic or a netlist. Runs on this computer; no key needed."
EXPLAIN = "Explain with AI (optional)"


class MainWindow(QMainWindow):
    def __init__(self, *, demo=False, config_dir=None):
        super().__init__()
        self.setWindowTitle("VelaTrace · KiCad companion")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(630, min(900, screen.height() - 80))
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
        self.ready = False
        self.preview_shown = False
        self._cleanup_failed = False
        self.mode = Mode.AUDIT
        self.consent = ConsentStore(self.config_dir / "privacy-consent.json")
        self.cards = {}
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
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = label("VelaTrace")
        title.setObjectName("title")
        header.addWidget(title)
        self.mode_label = label("Audit")
        self.mode_label.setObjectName("mode")
        header.addWidget(self.mode_label)
        header.addStretch()
        self.badge = QPushButton()
        self.badge.clicked.connect(self.open_constraints)
        header.addWidget(self.badge)
        self.setup_button = QPushButton("Setup")
        self.setup_button.clicked.connect(self.configure)
        header.addWidget(self.setup_button)
        layout.addLayout(header)
        self.refresh_badge()
        options = QHBoxLayout()
        self.command = QLineEdit()
        self.command.setPlaceholderText("Type /autoroute or /autoroute_exit, then press Enter")
        self.command.returnPressed.connect(self.run_command)
        options.addWidget(self.command)
        self.theme = QComboBox()
        self.theme.addItems(["KiCad", "System", "Light", "Dark"])
        self.theme.currentTextChanged.connect(self.apply_theme)
        options.addWidget(self.theme)
        layout.addLayout(options)
        self.banner = label(muted=True)
        self.banner.hide()
        layout.addWidget(self.banner)
        self.setup_hint = label()
        self.setup_hint.setStyleSheet("color:#D45A67")
        layout.addWidget(self.setup_hint)
        self.pages = QStackedWidget()
        self.audit_page = QWidget()
        audit_layout = QVBoxLayout(self.audit_page)
        audit_layout.setContentsMargins(0, 0, 0, 0)
        source = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItems(["Open PCB through IPC", "Saved schematic", "Exported XML netlist"])
        source.addWidget(self.source, 1)
        self.load_button = QPushButton("Check design")
        self.load_button.setObjectName("primary")
        self.load_button.clicked.connect(self.load_design)
        source.addWidget(self.load_button)
        self.restart_button = QPushButton("New audit")
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
        self.card_layout.addStretch()
        self.scroll.setWidget(self.card_container)
        audit_layout.addWidget(self.scroll, 1)
        self.totals = label(NO_REMOVAL, muted=True)
        audit_layout.addWidget(self.totals)
        # Only the optional AI pass uses the description; it never blocks the checks.
        self.description = QTextEdit()
        self.description.setAcceptRichText(False)
        self.description.setPlaceholderText("Optional, for Explain with AI: what the board does…")
        self.description.setMaximumHeight(52)
        audit_layout.addWidget(self.description)
        buttons = QHBoxLayout()
        self.next_button = QPushButton(EXPLAIN)
        self.next_button.clicked.connect(self.audit_next)
        self.price_button = QPushButton("Price flagged parts")
        self.price_button.clicked.connect(self.price_parts)
        buttons.addWidget(self.next_button, 1)
        buttons.addWidget(self.price_button)
        audit_layout.addLayout(buttons)
        self.annotations = QPushButton("Show / refresh board annotations")
        self.annotations.clicked.connect(self.show_annotations)
        audit_layout.addWidget(self.annotations)
        self.pages.addWidget(self.audit_page)
        self.route_page = QWidget()
        route_layout = QVBoxLayout(self.route_page)
        route_layout.setContentsMargins(0, 0, 0, 0)
        route_layout.addWidget(label("Route placed footprints", muted=False))
        self.placed = QCheckBox("Every footprint is already placed; no autoplacement")
        route_layout.addWidget(self.placed)
        self.route_prompt = QLineEdit()
        self.route_prompt.setPlaceholderText("Optional: keep traces away from headers")
        route_layout.addWidget(self.route_prompt)
        proposal = QPushButton("Restate prompt as a numeric constraint")
        proposal.clicked.connect(self.propose)
        route_layout.addWidget(proposal)
        row = QHBoxLayout()
        self.export_button = QPushButton("1 · Request fresh DSN")
        self.export_button.clicked.connect(self.request_export)
        self.import_button = QPushButton("2 · Load fresh DSN")
        self.import_button.clicked.connect(self.load_dsn)
        row.addWidget(self.export_button)
        row.addWidget(self.import_button)
        route_layout.addLayout(row)
        self.route_button = QPushButton("3 · Generate routing preview")
        self.route_button.setObjectName("primary")
        self.route_button.clicked.connect(self.route)
        route_layout.addWidget(self.route_button)
        self.canvas = RouteCanvas()
        route_layout.addWidget(self.canvas, 1)
        self.route_summary = label()  # errors and blocking messages only; info goes to note()
        self.route_summary.hide()
        route_layout.addWidget(self.route_summary)
        route_layout.addWidget(label("Panel traces are dashed violet on each layer. Board previews use enabled User.9; KiCad controls its color. Set User.9 to violet for a matching preview.", muted=True))
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason for rejection — required before retry")
        route_layout.addWidget(self.reason)
        row = QHBoxLayout()
        self.reject_button = QPushButton("Reject")
        self.reject_button.clicked.connect(self.reject_route)
        self.approve_button = QPushButton("4 · Approve and apply copper")
        self.approve_button.setObjectName("primary")
        self.approve_button.clicked.connect(self.approve_route)
        # Deliberately not the primary action: enabled only when DRC findings block approval.
        self.approve_anyway_button = QPushButton("Approve anyway (DRC errors)…")
        self.approve_anyway_button.setObjectName("danger")
        self.approve_anyway_button.clicked.connect(self.approve_anyway_route)
        row.addWidget(self.reject_button)
        row.addWidget(self.approve_anyway_button)
        row.addWidget(self.approve_button)
        route_layout.addLayout(row)
        # Validation details can grow, and small displays must still expose the
        # approval controls without forcing the whole window off screen.
        self.route_scroll = QScrollArea()
        self.route_scroll.setWidgetResizable(True)
        self.route_scroll.setWidget(self.route_page)
        self.pages.addWidget(self.route_scroll)
        layout.addWidget(self.pages, 1)
        self.status = label("Setup required.")
        self.usage_label = label("Tokens: idle", muted=True)
        layout.addWidget(self.status)
        layout.addWidget(self.usage_label)
        self.setCentralWidget(central)
        self.render_cards()
        self.refresh_actions()

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
        self.badge.setText(f"● {len(self.constraints.items)} constraints")
        self.badge.setStyleSheet(f"color:{ACCENT}")

    def note(self, text):
        """Informational result: hover/click/screen-reader text on the preview, not visible copy."""
        self.route_summary.hide()
        self.route_summary.setText("")
        self.canvas.setToolTip(text)
        self.canvas.setAccessibleName("Routing preview")
        self.canvas.setAccessibleDescription(text)

    def blocking(self, text):
        self.route_summary.setText(text)
        self.route_summary.show()

    def show_error(self, message):
        self.status.setText("Stopped: " + message)
        self.status.setStyleSheet("color:#D45A67")

    def refresh_actions(self):
        busy = self.worker is not None
        # Audit needs only the AI provider (checked when used); routing needs the verified router.
        self.audit_page.setEnabled(not busy)
        self.route_page.setEnabled(not busy and (self.ready or self.demo))
        locked = not (self.ready or self.demo)
        self.setup_hint.setVisible(locked)
        self.setup_hint.setText(
            "Routing is locked until Setup verifies Java 21 and Freerouting 2.1.0"
            + (f": {self._setup_error}" if self._setup_error else ".")
            + " Click Setup and check those paths. The audit works without them.")
        self.command.setEnabled(not busy and not self.demo)
        self.setup_button.setEnabled(not busy and not self.demo)
        self.badge.setEnabled(not busy and not self.demo)
        stage = self.audit.stage if self.audit else None
        # Explain with AI is an optional second pass over findings already shown.
        self.next_button.setEnabled(not self.demo and self.audit_snapshot is not None and (
            stage is None or stage in {AuditStage.FUNCTIONS, AuditStage.REVIEW_FUNCTIONS,
                                       AuditStage.CONFIRMED_FUNCTIONS, AuditStage.CLASSIFICATION_ESTIMATE}))
        self.price_button.setEnabled(not self.demo and stage == AuditStage.CLASSIFIED)
        self.annotations.setEnabled(not self.demo and stage == AuditStage.CLASSIFIED and self.safety is not None)
        self.import_button.setEnabled(self.ticket is not None)
        self.route_button.setEnabled(self.routing is not None and self.routing.input is not None)
        report = self.routing.report if self.routing is not None else None
        validated = report is not None and self.preview_shown and self.routing.stage == RoutingStage.PREVIEW
        self.approve_button.setEnabled(validated and report.drc_violations == 0)
        self.approve_anyway_button.setEnabled(validated and bool(report.drc_violations))
        self.reject_button.setEnabled(self.routing is not None and self.routing.stage in {
            RoutingStage.PREVIEW, RoutingStage.SHORTFALL, RoutingStage.NEEDS_REASON, RoutingStage.VALIDATING})

    def run_work(self, title, operation, success=None, failure=None):
        if self.worker is not None:
            return
        self.status.setStyleSheet("")
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
        self._work_title = message
        self.update_work_status()

    @Slot()
    def update_work_status(self):
        if self._work_started is not None:
            elapsed = time.monotonic() - self._work_started
            self.status.setText(f"{self._work_title} · {elapsed:.0f}s — keep KiCad unchanged until this finishes.")

    @Slot()
    def work_finished(self):
        self.work_timer.stop()
        self._work_started = None
        self.worker = None
        self.refresh_actions()

    @Slot(object)
    def update_usage(self, usage: Usage):
        incoming = "pending" if usage.input_tokens is None else str(usage.input_tokens)
        outgoing = "pending" if usage.output_tokens is None else str(usage.output_tokens)
        calls = f" · calls {self.audit.provider.budget.used}/{self.audit.provider.budget.limit}" if self.audit else ""
        self.usage_label.setText(f"Current response: {incoming} input · {outgoing} output tokens · {usage.text_characters} characters received · {usage.source}{calls}")

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
                router = Freerouting(Path(settings.jar), settings.java, work_directory=self.config_dir / "router-work", warm=settings.warm_router)
                router.check_startup()
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
            self.status.setText("Java and pinned Freerouting verified.")
        self.ready = False
        self.run_work("Checking required local router and Java", operation, success)

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
        self.description.setReadOnly(False)
        self.next_button.setText(EXPLAIN)
        self.show_findings_summary()
        self.render_cards()

    def show_findings_summary(self):
        self.totals.setText((summarize(self.findings) + "\n" if self.audit_snapshot else "") + NO_REMOVAL)

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
                    safety = BoardSafety(reader.client.get_board(), snapshot.path)
            elif choice == 1:
                # Picking the saved file is the confirmation; the step line says so.
                snapshot = KiCadCli(self.settings.cli).schematic_snapshot(path, saved_confirmed=True)
            else:
                snapshot = read_xml_netlist(path)
            # Logos, fiducials and unconnected holes (often REF** or duplicate refs)
            # carry no connectivity; they must not block the checks.
            snapshot = replace(snapshot, components=tuple(c for c in snapshot.components if c.nets))
            require_connectivity(snapshot)
            return snapshot, run_rules(snapshot), reader, safety
        def success(value):
            self.audit_snapshot, self.findings, self.reader, self.safety = value
            self.apply_theme(self.theme.currentText())
            self.pricing = self.routing = None
            self.ticket = None
            count = len(self.audit_snapshot.components)
            saved = " From the saved file; unsaved edits are not included." if choice else ""
            self.audit_step.setText(f"{count} parts checked on this computer; nothing was sent.{saved}")
            self.reset_ai()
        self.run_work("Reading connectivity and checking the design", operation, success)

    def restart_audit(self):
        def operation(_):
            if self.safety:
                self.safety.clear_preview()
        def done(_):
            self.audit_snapshot, self.findings = None, []
            self.audit_step.setText(READY_STEP)
            self.reset_ai()
        self.run_work("Starting a fresh audit", operation, done)

    def start_explain(self):
        """Optional second pass: the model explains the findings and each part's role."""
        if self.audit_snapshot is None:
            return
        try:
            audit = self.make_audit()
        except ValidationError as exc:
            self.show_error(f"Explain with AI needs your own provider and model; open Setup ({exc}) "
                            "The checks above need neither.")
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
            if self.audit.stage == AuditStage.FUNCTIONS:
                if not self.authorize_provider():
                    return
                def done(_):
                    self.audit_step.setText("Correct each AI function and electrical role, then confirm them.")
                    self.next_button.setText("Confirm functions and estimate tokens")
                    self.render_cards()
                self.run_work("Inferring functions", lambda usage: self.audit.infer_functions(usage, self.settings.output_cap), done)
            elif self.audit.stage == AuditStage.REVIEW_FUNCTIONS:
                for reference, card in self.cards.items():
                    old = self.audit.functions[reference]
                    if (card.function_edit.text(), card.role_edit.text()) != (old.text, old.role):
                        self.audit.correct_function(reference, card.function_edit.text(), card.role_edit.text())
                if not ask(self, "Confirm component functions", "I have reviewed and corrected the functions and roles. Use these confirmed functions for classification?"):
                    return
                self.audit.confirm_functions()
                self.prepare_classification()
            else:
                self.prepare_classification()
        except Exception as exc:
            self.show_error(str(exc))

    def prepare_classification(self):
        if not self.authorize_provider():
            return
        def done(estimate):
            self.render_cards()
            self.next_button.setText("Review classification estimate")
            self.audit_step.setText("Functions confirmed. Classification needs this prompt's token confirmation.")
            text = (f"{estimate.input_tokens:,} exact input tokens from the actual prompt\n"
                    f"Output cap: {estimate.output_cap:,} per attempt\nAt most {estimate.calls_max} attempts (two retries).\n"
                    f"Method: {estimate.method}\nHard call cap remaining: {self.audit.provider.budget.remaining}\n\nStart classification?")
            if ask(self, "Classification token estimate", text):
                # Worker completion is delivered after result; queue the next job.
                QTimer.singleShot(0, lambda: self.start_classification(estimate.confirmation_fingerprint))
        self.run_work("Counting the actual classification prompt", lambda _: self.audit.estimate_classification(self.settings.output_cap), done)

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
            self.audit_step.setText(f"AI review complete · {len(self.audit.flags)} flagged or borderline components.")
            self.render_cards()
            if self.safety:
                QTimer.singleShot(0, self.show_annotations)
        self.run_work("Classifying confirmed component functions", lambda usage: self.audit.classify(fingerprint, usage), done)

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
                item.widget().deleteLater()
        self.cards = {}
        self.finding_cards = []

    def render_findings(self):
        if not self.findings:
            self.card_layout.addWidget(label("No issues found by the built-in checks.", muted=True))
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
        if classified:
            self.next_button.setText("AI review complete")
        self.price_button.setVisible(classified)
        self.annotations.setVisible(classified)
        self.clear_cards()
        if self.audit_snapshot is not None:
            self.render_findings()
        if self.audit and self.audit.snapshot and self.audit.stage != AuditStage.FUNCTIONS:
            self.card_layout.addWidget(label("AI review (optional)", muted=True))
            flags = {item.reference: item for item in self.audit.flags}
            for component in self.audit.snapshot.components:
                reference = component.reference
                card = ComponentCard(component, self.audit.functions.get(reference),
                    self.audit.verdicts.get(reference), self.pricing.prices.get(reference) if self.pricing else None,
                    flags.get(reference), self.audit.stage == AuditStage.REVIEW_FUNCTIONS)
                self.cards[reference] = card
                self.card_layout.addWidget(card)
        if self.audit_snapshot is None and not self.cards:
            self.card_layout.addWidget(label("Findings appear here, grouped as errors, warnings, savings and info.", muted=True))
        self.card_layout.addStretch()

    def price_parts(self):
        try:
            if not self.authorize_provider():
                return
            pricing = PricingSession(self.audit)
            estimate = pricing.prepare()
            text = (f"{estimate.flagged_count} flagged, ~{estimate.searches_low}–{estimate.searches_high} searches, "
                    f"~{estimate.visible_prompt_token_upper_estimate:,} visible prompt tokens.\n"
                    f"Output cap: {estimate.output_cap_each} per flagged item. Maximum HTTP calls: {estimate.max_http_calls}.\n\n"
                    f"{estimate.note}\n\nStart pricing?")
            if not ask(self, "Pricing estimate", text):
                return
            def done(_):
                self.pricing = pricing
                self.render_cards()
                self.totals.setText((summarize(self.findings) + "\n" if self.audit_snapshot else "")
                                    + f"AI pricing: flagged ${pricing.flagged_cost:.2f} · hypothetical savings ${pricing.hypothetical_savings:.2f}. Illustrative; verify every suggestion.")
                if pricing.failures:
                    self.show_error(pricing.failure_summary)
                    self.totals.setText(self.totals.text() + "\n" + pricing.failure_summary)
            self.run_work("Pricing only flagged components", lambda usage: pricing.run(estimate.fingerprint, usage), done)
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
        self.run_work("Showing guarded temporary annotations", lambda _: self.safety.show_annotations(rows),
                      lambda _: self.status.setText("Ready."))

    def open_constraints(self):
        ConstraintsDialog(self).exec()
        self.refresh_actions()

    def invalidate_visible_preview(self):
        self.preview_shown = False
        self.canvas.plan = None
        self.canvas.update()
        self.note("Constraints changed. Confirm the full list and reroute; any old board preview will be removed before the next run.")
        self.refresh_actions()

    def run_command(self):
        command = self.command.text().strip()
        try:
            if command not in {"/autoroute", "/autoroute_exit"}:
                raise ValidationError("Use /autoroute or /autoroute_exit.")
            target = Mode.ROUTING if command == "/autoroute" else Mode.AUDIT
            self.mode = target
            self.mode_label.setText(target.value)
            self.pages.setCurrentIndex(1 if target == Mode.ROUTING else 0)
            self.command.clear()
            self.preview_shown = False
            def operation(_):
                if self.safety:
                    self.safety.clear_preview()
                if self.routing:
                    self.routing.command(command)
            self.run_work("Changing mode and cleaning temporary graphics", operation)
        except Exception as exc:
            self.show_error(str(exc))

    def propose(self):
        try:
            item = propose_constraint(self.route_prompt.text())
            if not ask(self, "Numeric interpretation", item.description + "\n\nAdd this session constraint? Unsupported rules will stop routing."):
                return
            if self.routing:
                self.routing.add_constraint(item)
            else:
                self.constraints.add(item)
            self.invalidate_visible_preview()
            self.route_prompt.clear()
            self.refresh_badge()
        except Exception as exc:
            self.show_error(str(exc))

    def request_export(self):
        if not self.placed.isChecked():
            self.show_error("Confirm that all footprints are placed first.")
            return
        if not ask(self, "Prepare saved board", "Save the open, initially unrouted PCB and its project in KiCad before continuing. Temporary annotations must already be cleared. Confirm it is saved?"):
            return
        def operation(_):
            if self.safety:
                self.safety.clear_preview()
            reader = KiCadReader.connect()
            snapshot = reader.read_board()
            if not snapshot.path:
                raise ValidationError("Routing requires a saved PCB with an absolute path.")
            cli = KiCadCli(self.settings.cli)
            cli.require_editor_version(reader.version)
            safety = BoardSafety(reader.client.get_board(), snapshot.path)
            validator = SafeCandidateValidator(safety, cli)
            session = RoutingSession(self.constraints, self.router, validator)
            session.command("/autoroute")
            ticket = ExportTicket.begin(snapshot.path)
            return reader, snapshot, safety, validator, session, ticket
        def done(value):
            self.reader, self.snapshot, self.safety, self.validator, self.routing, self.ticket = value
            self.apply_theme(self.theme.currentText())
            self.writer = SafeBoardWriter(self.safety, self.validator)
            self.blocking("Now export Specctra DSN using KiCad File → Export, then select ‘Load fresh DSN’. Do not edit or save changes after this request.")
        self.run_work("Preparing a fresh DSN request", operation, done)

    def load_dsn(self):
        selected, _ = QFileDialog.getOpenFileName(self, "Fresh Specctra DSN", "", "Specctra DSN (*.dsn)")
        if not selected:
            return
        if not self.placed.isChecked() or not ask(self, "Confirm fresh export", "The PCB is saved and this DSN was exported after the fresh-DSN request. Every footprint remains placed. Confirm?"):
            return
        def operation(_):
            dsn = accept_export(self.ticket, Path(selected), self.snapshot, user_confirms_saved_and_exported=True)
            self.routing.set_input(dsn, all_footprints_placed=True)
            return dsn
        self.run_work("Checking DSN connectivity and placements", operation,
                      lambda dsn: self.note(f"Fresh DSN verified: {dsn.path.name}. Review all numeric constraints before routing."))

    def route(self):
        try:
            if self.route_prompt.text().strip():
                raise ValidationError("Restate or explicitly clear the pending routing prompt before routing; no constraint may be silently ignored.")
            if not self.placed.isChecked():
                raise ValidationError("Confirm that every footprint is placed.")
            if not ask(self, "Confirm every numeric constraint", self.routing.confirmation_text()):
                return
            self.routing.confirm_constraints(self.constraints.fingerprint)
            self.preview_shown = False
            def operation(_):
                # Every preview refusal that can be known before routing fails here, not after it.
                self.executor.progress.emit("Checking the User.9 preview layer")
                self.safety.prepare_preview()
                self.routing.progress = self.executor.progress.emit
                plan = self.routing.route(trusted_via_catalog(self.routing.input))
                generation = self.routing.generation
                # The preview is drawn before DRC; approval waits for the DRC result.
                self.executor.progress.emit("Showing routing preview")
                started = time.monotonic()
                snapshot = self.safety.show_preview(self.routing.input, plan)
                return plan, generation, snapshot, time.monotonic() - started
            def done(value):
                plan, generation, snapshot, preview_seconds = value
                self.preview_shown = True
                self._route_timing = f"Routing {self.routing.timings['router']:.1f}s; preview {preview_seconds:.1f}s"
                self.note(
                    f"{plan.trace_count} traces; {len(plan.vias)} vias; layers: {', '.join(plan.layers_used)}. "
                    f"{self._route_timing}. Preview only: User.9 graphics do not change copper or the ratsnest. "
                    "DRC check follows; approval unlocks when it finishes.")
                self.canvas.plan = plan
                self.canvas.update()
                self.check_drc(self.routing, self.validator, plan, generation, snapshot)
            self.run_work("Freerouting, then the User.9 preview", operation, done)
        except Exception as exc:
            self.show_error(str(exc))

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
        self.status.setText("Checking DRC… Approve unlocks when it finishes; keep KiCad unchanged.")
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
            self.blocking(message + " It cannot be approved; generate a new routing preview.")
            self.run_work("Removing the unvalidated preview", lambda _: self.safety.clear_preview(),
                          lambda _: self.show_error(message))
        else:
            timing = f" {self._route_timing}; DRC {session.timings['validation']:.1f}s."
            next_step = ("Click ‘Approve and apply copper’ to install this route, or reject it. No save is required before approval."
                         if session.stage == RoutingStage.PREVIEW and report.drc_violations == 0
                         else "Approval is blocked by the validation result. Review it, then reject and revise the route"
                         + (", or use ‘Approve anyway’ to apply it despite the DRC errors after an explicit confirmation."
                            if session.stage == RoutingStage.PREVIEW else "."))
            text = (session.summary + timing +
                " Preview only: User.9 graphics do not change copper or the ratsnest. " + next_step)
            if session.stage == RoutingStage.PREVIEW and report.drc_violations == 0:
                self.note(text)
            else:
                self.blocking(text)  # approval blocked: the reason stays visible
            if self.worker is None:
                self.status.setText("DRC finished.")
        self.refresh_actions()

    def reject_route(self):
        try:
            self.routing.reject(self.reason.text())
            self.preview_shown = False
            def done(_):
                self.canvas.plan = None
                self.canvas.update()
                self.note("Rejected: " + self.routing.rejection_reason + ". Adjust the numeric constraints explicitly, then confirm and retry.")
            self.run_work("Removing rejected preview", lambda _: self.safety.clear_preview(), done)
        except Exception as exc:
            self.show_error(str(exc))

    def approve_route(self):
        if not self.preview_shown:
            self.show_error("A successfully displayed board preview is required before approval.")
            return
        if not ask(self, "Apply validated routing", "Apply this route as one backed-up KiCad undoable commit? The PCB remains unsaved; inspect it in KiCad before saving."):
            return
        self.apply_route(drc_override=False)

    def approve_anyway_route(self):
        """Explicit override of the DRC gate only; every other approval check still applies."""
        report = self.routing.report if self.routing else None
        if not self.preview_shown or report is None or not report.drc_violations:
            self.show_error("‘Approve anyway’ applies only to a displayed preview blocked by DRC findings.")
            return
        reasons = report.blocking_reasons or ("DRC issue details unavailable; review the full KiCad DRC report",)
        listed = "\n".join(f"• {reason}" for reason in reasons[:5])
        if len(reasons) > 5:
            listed += f"\n• … and {len(reasons) - 5} more"
        text = (f"KiCad DRC found {report.drc_violations} blocking issue(s) for this route:\n{listed}\n\n"
                "Approve anyway writes the routed copper to the board DESPITE these DRC errors. It is still one "
                "backed-up KiCad commit that a single Undo reverts, and the override and these issues are recorded "
                "in the backup journal. Fix them in KiCad before manufacturing.\n\nWrite the copper anyway?")
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
                "Temporary preview removed. Inspect and save in KiCad. Refresh before further routing, including after Undo.")
        def failed(message):
            self.blocking("Copper application was not confirmed. The panel retains the previous preview; "
                "inspect KiCad and the error before retrying, since the live outcome may be uncertain. " + message)
        self.run_work("Backing up and applying one routing commit", operation, done, failed)

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
            self.show_error("Wait for the current operation, including any DRC check, to finish before closing.")
            return
        if self.safety:
            event.ignore()
            # A failed cleanup (e.g. KiCad already closed) must not trap the window forever.
            if self._cleanup_failed and ask(self, "Close without cleanup",
                    "VelaTrace could not remove its temporary User.9 graphics. Delete any "
                    "remaining VelaTrace graphics in KiCad before saving. Close anyway?"):
                self.safety = None
                QTimer.singleShot(0, self.close_after_cleanup)
                return
            self._cleanup_failed = True  # Cleared only when cleanup succeeds.
            def done(_):
                self._cleanup_failed = False
                self.safety = None
                QTimer.singleShot(0, self.close_after_cleanup)
            self.run_work("Cleaning owned temporary graphics before closing", lambda _: self.safety.clear_preview(), done)
            return
        for worker in (self.executor, self.drc_executor):
            worker.jobs.put(None)
            worker.wait(2000)
        if self.router is not None:
            self.router.close()  # No java.exe outlives the window.
        event.accept()

    def close_after_cleanup(self):
        if self.worker is not None:
            QTimer.singleShot(30, self.close_after_cleanup)
        else:
            self.close()

    def load_demo(self):
        self.description.setPlainText("Battery-powered environmental monitor with a status LED and two temperature sensors on a shared bus.")
        self.banner.show()
        self.banner.setText("DEMO · synthetic data · no API, IPC or board writes")
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
        self.findings = run_rules(self.audit_snapshot)
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
        self.audit_step.setText(f"DEMO · {len(components)} parts checked on this computer; nothing was sent.")
        self.status.setText("Preview only. Real runs use your open board or saved files.")
        self.usage_label.setText("Tokens: demo — no requests made")
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
