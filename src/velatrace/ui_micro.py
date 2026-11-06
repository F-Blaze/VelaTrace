"""Glide Select and Lattice Loader: clean-room Qt rebuilds of two React Bits micro looks, no code
from it. Motion goes through ui_effects, so set_motion(False) snaps both."""
from __future__ import annotations

import time

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QWidget

from .ui_effects import motion_enabled
from .ui_style import DANGER, _fade, colors

TAG = Qt.ItemDataRole.UserRole + 7
ROW, PAD, SHADOW, RADIUS = 32, 4, 10, 10


def _round(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


class GlideSelect(QComboBox):
    """Select chip whose menu pops out of its corner; one pill glides between rows, the picked row
    rests on it at 60 % with a check. Keeps the QComboBox API (signals, setCurrentText)."""
    def __init__(self, items=(), parent=None):
        super().__init__(parent)
        for item in items:
            text, tag = (item, "") if isinstance(item, str) else item
            self.addItem(text)
            self.setItemData(self.count() - 1, tag, TAG)
        self.menu = None
        self._closed = 0.0

    def showPopup(self):
        # A click on the chip while open first closes the popup (Qt.Popup), then lands here: stay closed.
        if self.count() and time.monotonic() - self._closed > .25:
            self.menu = _Menu(self)
            self.menu.pop()

    def hidePopup(self):
        if self.menu is not None:
            self.menu.leave()

    def _menu_closed(self):
        self._closed = time.monotonic()
        self.menu = None
        self.setProperty("vtOpen", False)
        self.update()


class _Menu(QWidget):
    def __init__(self, combo):
        super().__init__(combo, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setFont(combo.font())
        self.combo, self.scale, self.up = combo, 1.0, False
        self.row = combo.currentIndex()
        self.pill = self._top(self.row)
        self.pop_anim = QVariantAnimation(self)
        self.pop_anim.valueChanged.connect(self._grow)
        self.pop_anim.finished.connect(lambda: self.close() if self.scale == 0 else None)
        self.glide = QVariantAnimation(self)
        self.glide.setDuration(220)
        self.glide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.glide.valueChanged.connect(self._slide)

    def _top(self, row):
        return float(SHADOW + PAD + max(row, 0) * ROW)

    def _row_at(self, pos):
        row = int((pos.y() - SHADOW - PAD) // ROW)
        return row if 0 <= row < self.combo.count() and SHADOW <= pos.x() < self.width() - SHADOW else -1

    def _enabled(self, row):
        item = self.combo.model().item(row) if hasattr(self.combo.model(), "item") else None
        return item is None or item.isEnabled()

    def pop(self):
        combo = self.combo
        width = max(combo.width(), 176) + 2 * SHADOW
        height = combo.count() * ROW + 2 * PAD + 2 * SHADOW
        below = combo.mapToGlobal(QPoint(-SHADOW, combo.height() + 4 - SHADOW))
        screen = (combo.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        self.up = below.y() + height > screen.bottom() and combo.mapToGlobal(QPoint()).y() - height >= screen.top()
        at = combo.mapToGlobal(QPoint(-SHADOW, -height - 4 + SHADOW)) if self.up else below
        self.setGeometry(QRect(at.x(), at.y(), width, height))
        combo.setProperty("vtOpen", True)
        combo.update()
        self._animate(0.0, 1.0, 180, QEasingCurve.Type.OutBack)
        self.show()
        self.setFocus()

    def leave(self):
        if motion_enabled() and self.isVisible():
            self._animate(self.scale, 0.0, 120, QEasingCurve.Type.InCubic)  # leaves in two thirds of the pop
        else:
            self.close()

    def _animate(self, start, end, ms, curve):
        self.pop_anim.stop()
        if not motion_enabled():
            return self._grow(end)
        self.pop_anim.setDuration(ms)
        self.pop_anim.setEasingCurve(curve)
        self.pop_anim.setStartValue(start)
        self.pop_anim.setEndValue(end)
        self.pop_anim.start()

    def _grow(self, value):
        self.scale = float(value)
        self.update()

    def _slide(self, value):
        self.pill = float(value)
        self.update()

    def _hover(self, row):
        if row < 0 or row == self.row or not self._enabled(row):
            return
        self.row = row
        self.glide.stop()
        if motion_enabled():
            self.glide.setStartValue(self.pill)
            self.glide.setEndValue(self._top(row))
            self.glide.start()
        else:
            self._slide(self._top(row))

    def _pick(self, row):
        if row >= 0 and self._enabled(row):
            changed = row != self.combo.currentIndex()
            self.combo.setCurrentIndex(row)
            self.combo.activated.emit(row)
            self.combo.textActivated.emit(self.combo.itemText(row))
            if changed:
                self.combo.update()
        self.leave()

    def mouseMoveEvent(self, event):
        self._hover(self._row_at(event.position().toPoint()))

    def mouseReleaseEvent(self, event):
        row = self._row_at(event.position().toPoint())
        if row >= 0:
            self._pick(row)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = -1 if key == Qt.Key.Key_Up else 1
            row = self.row + step
            while 0 <= row < self.combo.count() and not self._enabled(row):
                row += step
            self._hover(row if 0 <= row < self.combo.count() else -1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._pick(self.row)
        elif key == Qt.Key.Key_Escape:
            self.leave()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        self.pop_anim.stop()
        self.glide.stop()
        self.combo._menu_closed()
        super().hideEvent(event)

    def paintEvent(self, event):
        c = colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        panel = QRectF(self.rect()).adjusted(SHADOW + .5, SHADOW + .5, -SHADOW - .5, -SHADOW - .5)
        # Grows out of the chip's corner: scale about it and fade in together.
        corner = panel.bottomLeft() if self.up else panel.topLeft()
        k = max(self.scale, 0.0)
        painter.translate(corner)
        painter.scale(.9 + .1 * k, .9 + .1 * k)
        painter.translate(-corner)
        painter.setOpacity(min(k, 1.0))
        for spread in range(1, 5):  # soft shadow from a few widening translucent rings
            painter.fillPath(_round(panel.adjusted(-spread, -spread + 3, spread, spread + 3), RADIUS + spread),
                             _fade(c["shadow"], .16 / spread))
        painter.fillPath(_round(panel, RADIUS), c["solid"])
        painter.setPen(QPen(c["edge"], 1))
        painter.drawPath(_round(panel, RADIUS))
        row_rect = lambda top: QRectF(panel.left() + PAD, top, panel.width() - 2 * PAD, ROW)  # noqa: E731
        current = self.combo.currentIndex()
        if current >= 0:
            painter.fillPath(_round(row_rect(self._top(current)), RADIUS - 4), _fade(c["accent_soft"], .6))
        painter.fillPath(_round(row_rect(self.pill), RADIUS - 4), c["accent_soft"])
        metrics = painter.fontMetrics()
        for row in range(self.combo.count()):
            rect = row_rect(self._top(row)).adjusted(10, 0, -10, 0)
            on = self._enabled(row)
            painter.setPen(c["fg"] if on else c["muted"])
            painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.combo.itemText(row))
            tag = self.combo.itemData(row, TAG) or ""
            if tag:
                painter.setPen(c["muted"])
                painter.drawText(rect.adjusted(0, 0, -20, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                                 metrics.elidedText(tag, Qt.TextElideMode.ElideRight, int(rect.width() / 2)))
            if row == current:
                y, x = rect.center().y(), rect.right() - 7
                painter.setPen(QPen(c["fg"], 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                    Qt.PenJoinStyle.RoundJoin))
                painter.drawPolyline([QPointF(x - 4, y), QPointF(x - 1.5, y + 2.5), QPointF(x + 3.5, y - 3)])


class LatticeLoader(QWidget):
    """3 x 3 dots: while working a bright wave orbits the ring; when done the lattice turns into a
    check (or a cross on error) for a few seconds. Repaints at ~30 fps only while working and shown."""
    RING = (0, 1, 2, 5, 8, 7, 6, 3)  # cells in orbit order; the centre stays dim
    MARKS = {"done": {3, 7, 5, 2}, "error": {0, 2, 4, 6, 8}}
    STEP, LIT, IDLE = .09, .35, .15  # seconds per cell, share of the loop a cell stays bright, dim alpha

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.state, self.started = "idle", 0.0
        self.tick = QTimer(self, interval=33, timeout=self.update)
        self.clear = QTimer(self, singleShot=True, interval=4000, timeout=self.hide)
        self.hide()

    def start(self):
        self.clear.stop()
        self.state, self.started = "working", time.monotonic()
        self.show()
        self._run()

    def finish(self, ok=True):
        if self.state != "working":
            return
        self.state = "done" if ok else "error"
        self.tick.stop()
        self.update()
        self.clear.start()

    def _run(self):
        if self.state == "working" and self.isVisible() and motion_enabled():
            self.tick.start()
        else:
            self.tick.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._run()

    def hideEvent(self, event):
        self.tick.stop()
        super().hideEvent(event)

    def _alpha(self, cell):
        if self.state in self.MARKS:
            return 1.0 if cell in self.MARKS[self.state] else self.IDLE
        if cell not in self.RING:
            return self.IDLE
        if not motion_enabled():
            return .6
        loop = len(self.RING)
        lag = ((time.monotonic() - self.started) / self.STEP - self.RING.index(cell)) % loop
        return self.IDLE + (1 - self.IDLE) * max(0.0, 1 - lag / (loop * self.LIT))

    def paintEvent(self, event):
        c = colors()
        ink = {"done": QColor("#4ADE80" if c["fg"].lightness() > 128 else "#15803D"), "error": DANGER}.get(
            self.state, c["fg"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        size, gap = 5.0, 1.5
        origin = (self.width() - 3 * size - 2 * gap) / 2
        for cell in range(9):
            painter.setBrush(_fade(ink, self._alpha(cell)))
            x, y = origin + (cell % 3) * (size + gap), origin + (cell // 3) * (size + gap)
            painter.drawEllipse(QRectF(x, y, size, size))
