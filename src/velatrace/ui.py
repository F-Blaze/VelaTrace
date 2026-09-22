"""Single external IPC companion window. All board writes use BoardSafety.

Qt widgets are confined to the main thread. A single serialized worker owns each
blocking service operation; it reports plain data through queued signals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import uuid
from queue import Queue

from PySide6.QtCore import Qt, QThread, Signal, Slot, QStandardPaths, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QFontDatabase, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

from .audit import AuditSession, AuditStage
from .candidate import SafeCandidateValidator, trusted_via_catalog
from .constraints import Constraint, ConstraintStore, Scope, propose_constraint
from .dsn import ExportTicket, accept_export
from .errors import ValidationError
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
    tokenizer: str = ""
    verified_model: str = ""
    cap: int = 40
    output_cap: int = 4096

    def provider_config(self):
        return ProviderConfig(self.name, self.endpoint, self.model, self.key,
                              self.protocol, self.search_model or None, self.search)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent):
        super().__init__(parent)
        self.setWindowTitle("VelaTrace setup")
        self.resize(650, 680)
        layout = QVBoxLayout(self)
        layout.addWidget(label("Local tools and your provider", muted=False))
        layout.addWidget(label("No backend or telemetry. Only your configured endpoint receives remote API requests. Keys stay in memory. Tool paths and provider settings apply to this launch.", muted=True))
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
        scroll.setWidget(content)
        layout.addWidget(scroll)
        layout.addWidget(label("Gemini uses exact countTokens. Groq/OpenAI-compatible models require a local tokenizer whose chat template you have verified against that exact provider/model. Built-in search must be supported by your selected provider/model.", muted=True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        values = {name: field.text().strip() for name, field in self.fields.items()}
        return Settings(**values, protocol=self.protocol.currentText(), search=self.search.isChecked(),
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


class RouteCanvas(QWidget):
    def __init__(self):
        super().__init__()
        self.plan = None
        self.setMinimumHeight(230)

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


class MainWindow(QMainWindow):
    def __init__(self, *, demo=False, config_dir=None):
        super().__init__()
        self.setWindowTitle("VelaTrace · KiCad companion")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.resize(630, 900)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(max(screen.left(), screen.right() - self.width()), screen.top() + 30)
        self.demo = demo
        self.config_dir = Path(config_dir or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
        self.constraints = ConstraintStore(self.config_dir / "constraints.json")
        self.settings = Settings(jar=os.environ.get("VELATRACE_FREEROUTING_JAR", ""),
                                 java=os.environ.get("VELATRACE_JAVA", "java"),
                                 cli=os.environ.get("VELATRACE_KICAD_CLI", "kicad-cli"),
                                 key=os.environ.get("VELATRACE_API_KEY", ""),
                                 model=os.environ.get("VELATRACE_MODEL", ""))
        self.audit = self.pricing = self.routing = self.router = None
        self.reader = self.safety = self.validator = self.writer = None
        self.ticket = self.snapshot = None
        self.worker = None
        self.executor = Worker(self)
        self.executor.usage.connect(self.update_usage)
        self.executor.result.connect(self.work_result)
        self.executor.error.connect(self.show_error)
        self.executor.idle.connect(self.work_finished)
        self.executor.start()
        self.ready = False
        self.preview_shown = False
        self.mode = Mode.AUDIT
        self.consent = ConsentStore(self.config_dir / "privacy-consent.json")
        self.cards = {}
        self.build_ui()
        self.apply_theme("KiCad")
        if demo:
            self.load_demo()
        else:
            QTimer.singleShot(0, self.configure)

    def build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = label("VelaTrace")
        title.setStyleSheet("font-size:25px; font-weight:700")
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
        self.command.setPlaceholderText("/autoroute or /autoroute_exit")
        self.command.returnPressed.connect(self.run_command)
        options.addWidget(self.command)
        self.theme = QComboBox()
        self.theme.addItems(["KiCad", "System", "Light", "Dark"])
        self.theme.currentTextChanged.connect(self.apply_theme)
        options.addWidget(self.theme)
        layout.addLayout(options)
        self.banner = label("No backend. No telemetry. Your key, your provider.", muted=True)
        layout.addWidget(self.banner)
        self.pages = QStackedWidget()
        self.audit_page = QWidget()
        audit_layout = QVBoxLayout(self.audit_page)
        audit_layout.setContentsMargins(0, 0, 0, 0)
        audit_layout.addWidget(label("What is this board meant to do?"))
        self.description = QTextEdit()
        self.description.setAcceptRichText(False)
        self.description.setPlaceholderText("Required: purpose, supply, interfaces and operating conditions…")
        self.description.setMaximumHeight(95)
        audit_layout.addWidget(self.description)
        source = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItems(["Open PCB through IPC", "Saved schematic", "Exported XML netlist"])
        source.addWidget(self.source, 1)
        self.load_button = QPushButton("Read design")
        self.load_button.clicked.connect(self.load_design)
        source.addWidget(self.load_button)
        self.restart_button = QPushButton("New audit")
        self.restart_button.clicked.connect(self.restart_audit)
        source.addWidget(self.restart_button)
        audit_layout.addLayout(source)
        self.audit_step = label("1 · Describe your project, then read connectivity.", muted=True)
        audit_layout.addWidget(self.audit_step)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.card_container = QWidget()
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.addStretch()
        self.scroll.setWidget(self.card_container)
        audit_layout.addWidget(self.scroll, 1)
        self.totals = label("All verdicts are suggestions. Components are never removed automatically.", muted=True)
        audit_layout.addWidget(self.totals)
        buttons = QHBoxLayout()
        self.next_button = QPushButton("Infer component functions")
        self.next_button.setObjectName("primary")
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
        route_layout.addWidget(label("Freerouting computes paths. KiCad validates the candidate before approval. Save an unrouted board and project first; keep the current stackup.", muted=True))
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
        self.route_button = QPushButton("3 · Confirm constraints and route")
        self.route_button.setObjectName("primary")
        self.route_button.clicked.connect(self.route)
        route_layout.addWidget(self.route_button)
        self.canvas = RouteCanvas()
        route_layout.addWidget(self.canvas, 1)
        self.route_summary = label("No routing result.")
        route_layout.addWidget(self.route_summary)
        route_layout.addWidget(label("Panel traces are dashed violet on each layer. Board previews use enabled User.9; KiCad controls its color. Set User.9 to violet for a matching preview.", muted=True))
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason for rejection — required before retry")
        route_layout.addWidget(self.reason)
        row = QHBoxLayout()
        self.reject_button = QPushButton("Reject")
        self.reject_button.clicked.connect(self.reject_route)
        self.approve_button = QPushButton("Approve route")
        self.approve_button.clicked.connect(self.approve_route)
        row.addWidget(self.reject_button)
        row.addWidget(self.approve_button)
        route_layout.addLayout(row)
        self.pages.addWidget(self.route_page)
        layout.addWidget(self.pages, 1)
        self.status = label("Setup required.")
        self.usage_label = label("Tokens: idle", muted=True)
        layout.addWidget(self.status)
        layout.addWidget(self.usage_label)
        self.setCentralWidget(central)
        self.refresh_actions()

    def apply_theme(self, choice):
        if choice == "KiCad":
            choice = (saved_kicad_theme(*self.reader.version[:2]) if self.reader else None) or "System"
        dark = choice == "Dark" or (choice == "System" and QApplication.palette().window().color().lightness() < 128)
        bg, card, fg, muted, border = (("#171820", "#242530", "#F4F3FA", "#ACADBD", "#393A48") if dark else
                                      ("#F5F4F8", "#FFFFFF", "#252331", "#686575", "#E1DFE9"))
        palette = self.palette()
        for role, value in ((QPalette.ColorRole.Window, bg), (QPalette.ColorRole.Base, card),
                            (QPalette.ColorRole.Text, fg), (QPalette.ColorRole.WindowText, fg)):
            palette.setColor(role, QColor(value))
        self.setPalette(palette)
        self.setStyleSheet(f"""
            QMainWindow, QDialog, QWidget {{background:{bg}; color:{fg}; font-family:'Segoe UI'; font-size:12px}}
            QLabel#muted {{color:{muted}}} QLabel#mode {{color:{ACCENT}; font-weight:700}}
            QFrame#card {{background:{card}; border:1px solid {border}; border-radius:9px}}
            QFrame#card QLabel {{background:transparent}}
            QPushButton {{border:1px solid {border}; border-radius:6px; padding:8px 10px; background:{card}}}
            QPushButton:hover {{border-color:{ACCENT}}} QPushButton:disabled {{color:{muted}}}
            QPushButton#primary {{background:{ACCENT}; color:white; border:0; font-weight:600}}
            QPushButton#textButton {{border:0; color:{ACCENT}; padding:4px 0px; background:transparent}}
            QLineEdit,QTextEdit,QComboBox,QSpinBox,QDoubleSpinBox,QListWidget {{background:{card}; border:1px solid {border}; border-radius:5px; padding:6px}}
            QScrollArea {{border:0}} QCheckBox {{spacing:7px}}
        """)

    def refresh_badge(self):
        self.badge.setText(f"● {len(self.constraints.items)} constraints")
        self.badge.setStyleSheet(f"color:{ACCENT}")

    def show_error(self, message):
        self.status.setText("Stopped: " + message)
        self.status.setStyleSheet("color:#D45A67")

    def refresh_actions(self):
        busy = self.worker is not None
        self.pages.setEnabled(not busy and (self.ready or self.demo))
        self.command.setEnabled(not busy and not self.demo)
        self.setup_button.setEnabled(not busy and not self.demo)
        self.badge.setEnabled(not busy and not self.demo)
        stage = self.audit.stage if self.audit else None
        self.next_button.setEnabled(not self.demo and stage in {AuditStage.FUNCTIONS, AuditStage.REVIEW_FUNCTIONS, AuditStage.CONFIRMED_FUNCTIONS, AuditStage.CLASSIFICATION_ESTIMATE})
        self.price_button.setEnabled(not self.demo and stage == AuditStage.CLASSIFIED)
        self.annotations.setEnabled(not self.demo and stage == AuditStage.CLASSIFIED and self.safety is not None)
        self.import_button.setEnabled(self.ticket is not None)
        self.route_button.setEnabled(self.routing is not None and self.routing.input is not None)
        self.approve_button.setEnabled(self.preview_shown and self.routing is not None and self.routing.stage == RoutingStage.PREVIEW and self.routing.report is not None and self.routing.report.drc_violations == 0)
        self.reject_button.setEnabled(self.routing is not None and self.routing.stage in {RoutingStage.PREVIEW, RoutingStage.SHORTFALL, RoutingStage.NEEDS_REASON})

    def run_work(self, title, operation, success=None):
        if self.worker is not None:
            return
        self.status.setStyleSheet("")
        self.status.setText(title + " — keep KiCad unchanged until this finishes.")
        self.worker = self.executor
        self._success = success
        self.refresh_actions()
        self.executor.jobs.put(operation)

    @Slot(object)
    def work_result(self, value):
        self.status.setText("Ready.")
        try:
            if self._success:
                self._success(value)
        except Exception as exc:
            self.show_error(str(exc))

    @Slot()
    def work_finished(self):
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
        settings = dialog.value()
        def operation(_):
            if self.safety:
                self.safety.clear_preview()
            router = Freerouting(Path(settings.jar), settings.java, work_directory=self.config_dir / "router-work")
            router.check_startup()
            return router
        def success(router):
            self.settings, self.router, self.ready = settings, router, True
            self.audit = self.pricing = self.routing = None
            self.reader = self.safety = self.validator = self.writer = None
            self.ticket = self.snapshot = None
            self.description.setReadOnly(False)
            self.render_cards()
            self.status.setText("Java and pinned Freerouting verified. Read a design to begin.")
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

    def load_design(self):
        description = self.description.toPlainText().strip()
        if not description:
            self.show_error("Enter the required project description first.")
            return
        choice = self.source.currentIndex()
        path = None
        if choice:
            selected, _ = QFileDialog.getOpenFileName(self, "Select saved connectivity source", "",
                "KiCad schematic (*.kicad_sch)" if choice == 1 else "KiCad XML netlist (*.xml *.net)")
            if not selected:
                return
            path = Path(selected)
            if not ask(self, "Saved design", "Confirm that this design is saved. Unsaved schematic changes are not included."):
                return
        def operation(_):
            if self.safety:
                self.safety.clear_preview()
            audit = self.make_audit()
            audit.set_description(description)
            reader = safety = None
            if choice == 0:
                reader = KiCadReader.connect()
                snapshot = reader.read_board()
                if snapshot.path:
                    safety = BoardSafety(reader.client.get_board(), snapshot.path)
            elif choice == 1:
                snapshot = KiCadCli(self.settings.cli).schematic_snapshot(path, saved_confirmed=True)
            else:
                snapshot = read_xml_netlist(path)
            audit.load_design(snapshot)
            return audit, reader, safety
        def success(value):
            self.audit, self.reader, self.safety = value
            self.apply_theme(self.theme.currentText())
            self.snapshot = self.audit.snapshot
            self.pricing = self.routing = None
            self.ticket = None
            self.description.setReadOnly(True)
            self.audit_step.setText(f"2 · {len(self.snapshot.components)} components with real connectivity. Infer functions, then correct them.")
            self.next_button.setText("Infer component functions")
            self.render_cards()
        self.run_work("Reading connectivity", operation, success)

    def restart_audit(self):
        def operation(_):
            if self.safety:
                self.safety.clear_preview()
        def done(_):
            self.audit = self.pricing = None
            self.description.setReadOnly(False)
            self.audit_step.setText("1 · Describe your project, then read connectivity.")
            self.next_button.setText("Infer component functions")
            self.totals.setText("All verdicts are suggestions. Components are never removed automatically.")
            self.render_cards()
        self.run_work("Starting a fresh audit", operation, done)

    def audit_next(self):
        try:
            if not self.audit:
                return
            if self.audit.stage == AuditStage.FUNCTIONS:
                if not self.authorize_provider():
                    return
                def done(_):
                    self.audit_step.setText("3 · Correct every function and electrical role, then confirm them.")
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
            self.audit_step.setText("4 · Functions confirmed. Classification requires this prompt's token confirmation.")
            text = (f"{estimate.input_tokens:,} exact input tokens from the actual prompt\n"
                    f"Output cap: {estimate.output_cap:,} per attempt\nAt most {estimate.calls_max} attempts (two retries).\n"
                    f"Method: {estimate.method}\nHard call cap remaining: {self.audit.provider.budget.remaining}\n\nStart classification?")
            if ask(self, "Classification token estimate", text):
                # Worker completion is delivered after result; queue the next job.
                QTimer.singleShot(0, lambda: self.start_classification(estimate.prompt_fingerprint))
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
            self.audit_step.setText(f"5 · Audit complete · {len(self.audit.flags)} flagged or borderline components.")
            self.render_cards()
            if self.safety:
                QTimer.singleShot(0, self.show_annotations)
        self.run_work("Classifying confirmed component functions", lambda usage: self.audit.classify(fingerprint, usage), done)

    def render_cards(self):
        if self.audit and self.audit.stage == AuditStage.CLASSIFIED:
            self.next_button.setText("Audit complete")
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards = {}
        if self.audit and self.audit.snapshot:
            flags = {item.reference: item for item in self.audit.flags}
            for component in self.audit.snapshot.components:
                reference = component.reference
                card = ComponentCard(component, self.audit.functions.get(reference),
                    self.audit.verdicts.get(reference), self.pricing.prices.get(reference) if self.pricing else None,
                    flags.get(reference), self.audit.stage == AuditStage.REVIEW_FUNCTIONS)
                self.cards[reference] = card
                self.card_layout.addWidget(card)
        else:
            self.card_layout.addWidget(label("Your connected components will appear as review cards here.", muted=True))
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
                self.totals.setText(f"Flagged cost: ${pricing.flagged_cost:.2f} · hypothetical savings: ${pricing.hypothetical_savings:.2f}. Estimates are illustrative; verify every suggestion.")
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
                      lambda _: self.status.setText("Annotations shown on User.9. Do not save temporary graphics; close or enter routing to clean them up."))

    def open_constraints(self):
        ConstraintsDialog(self).exec()
        self.refresh_actions()

    def invalidate_visible_preview(self):
        self.preview_shown = False
        self.canvas.plan = None
        self.canvas.update()
        self.route_summary.setText("Constraints changed. Confirm the full list and reroute; any old board preview will be removed before the next run.")
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
            safety = BoardSafety(reader.client.get_board(), snapshot.path)
            validator = SafeCandidateValidator(safety, KiCadCli(self.settings.cli))
            session = RoutingSession(self.constraints, self.router, validator)
            session.command("/autoroute")
            ticket = ExportTicket.begin(snapshot.path)
            return reader, snapshot, safety, validator, session, ticket
        def done(value):
            self.reader, self.snapshot, self.safety, self.validator, self.routing, self.ticket = value
            self.apply_theme(self.theme.currentText())
            self.writer = SafeBoardWriter(self.safety, self.validator)
            self.route_summary.setText("Now export Specctra DSN using KiCad File → Export, then select ‘Load fresh DSN’. Do not edit or save changes after this request.")
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
                      lambda dsn: self.route_summary.setText(f"Fresh DSN verified: {dsn.path.name}. Review all numeric constraints before routing."))

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
                self.safety.clear_preview()
                report = self.routing.run(trusted_via_catalog(self.routing.input))
                # Preview refusal must remain visible even if validated data exists.
                self.safety.show_preview(self.routing.input, self.routing.plan)
                return report
            def done(_):
                self.preview_shown = True
                self.route_summary.setText(self.routing.summary)
                self.canvas.plan = self.routing.plan
                self.canvas.update()
            self.run_work("Freerouting and actual KiCad candidate DRC", operation, done)
        except Exception as exc:
            self.show_error(str(exc))

    def reject_route(self):
        try:
            self.routing.reject(self.reason.text())
            self.preview_shown = False
            def done(_):
                self.canvas.plan = None
                self.canvas.update()
                self.route_summary.setText("Rejected: " + self.routing.rejection_reason + ". Adjust the numeric constraints explicitly, then confirm and retry.")
            self.run_work("Removing rejected preview", lambda _: self.safety.clear_preview(), done)
        except Exception as exc:
            self.show_error(str(exc))

    def approve_route(self):
        if not self.preview_shown:
            self.show_error("A successfully displayed board preview is required before approval.")
            return
        if not ask(self, "Apply validated routing", "Apply this route as one backed-up KiCad undoable commit? The PCB remains unsaved; inspect it in KiCad before saving."):
            return
        def done(_):
            self.preview_shown = False
            self.canvas.plan = None
            self.canvas.update()
            self.route_summary.setText("Applied to KiCad in one commit, using normal copper layer colors. Inspect and save in KiCad. Refresh before further routing, including after Undo.")
        self.run_work("Backing up and applying one routing commit", lambda _: self.routing.approve(self.writer), done)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            self.show_error("Wait for the current operation to finish before closing.")
            return
        if self.safety:
            event.ignore()
            def done(_):
                self.safety = None
                QTimer.singleShot(0, self.close_after_cleanup)
            self.run_work("Cleaning owned temporary graphics before closing", lambda _: self.safety.clear_preview(), done)
            return
        self.executor.jobs.put(None)
        self.executor.wait(2000)
        event.accept()

    def close_after_cleanup(self):
        if self.worker is not None:
            QTimer.singleShot(30, self.close_after_cleanup)
        else:
            self.close()

    def load_demo(self):
        self.description.setPlainText("Battery-powered environmental monitor with a status LED and two temperature sensors on a shared bus.")
        self.banner.setText("DEMO · synthetic data · no API, IPC or board writes")
        self.description.setReadOnly(True)
        self.settings.model = "demo"
        self.audit = self.make_audit()
        self.audit.set_description(self.description.toPlainText())
        pins = (Pin("1", "+3V3"), Pin("2", "GND"))
        components = (Component("U1", "MCU", "QFN:32", pins, position_mm=(20, 20)),
                      Component("C1", "10 µF", "Capacitor:0805", pins, position_mm=(22, 20)),
                      Component("D1", "Status LED", "LED:0603", pins, position_mm=(30, 20)),
                      Component("U3", "TMP102", "SOT:23-6", pins, position_mm=(35, 20)))
        self.audit.load_design(DesignSnapshot(components, "demo"))
        descriptions = ["Runs the sensing and reporting loop", "Supports the supply during transient loads",
                        "Shows device activity at a glance", "Measures the same local temperature as U2"]
        buckets = list(Bucket)
        for comp, text, bucket in zip(components, descriptions, buckets):
            self.audit.functions[comp.reference] = Function(text, text, .95)
            self.audit.verdicts[comp.reference] = Verdict(comp.reference, bucket, .95,
                "Review the schematic and intended operating conditions before making a design change.")
        self.audit.stage = AuditStage.CLASSIFIED
        self.pricing = PricingSession(self.audit)
        self.pricing.prices = {comp.reference: estimate_price(comp) for comp in components[2:]}
        self.audit_step.setText("AUDIT COMPLETE · 4 components · 2 illustrative suggestions")
        self.status.setText("Preview only. Real runs require your local tools and explicit confirmations.")
        self.usage_label.setText("Tokens: demo — no requests made")
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
    if screenshot:
        def render():
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(screenshot)):
                raise OSError("Could not save demo screenshot.")
            window.close()
            app.quit()
        QTimer.singleShot(200, render)
    return app.exec()
