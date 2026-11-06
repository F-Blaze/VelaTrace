"""VelaStyle: Fusion with antialiased, token-coloured controls, so edges stay crisp at any scale.

Clean-room Qt painting; the looks (border glow, specular button, glide select, spring check,
rubber segment, warm tooltip) are only inspired by React Bits, no code from it. Motion goes through
ui_effects, so set_motion(False) snaps every state."""
from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import (QBrush, QColor, QCursor, QLinearGradient, QPainter, QPainterPath,
                           QPalette, QPen, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QListWidget, QProxyStyle,
                               QPushButton, QScrollBar, QStyle, QStyleOptionViewItem,
                               QStyledItemDelegate, QTextEdit, QWidget)

from .ui_effects import ACCENT, motion_enabled, progress, track_check, track_hover, untrack_check, untrack_hover

DEFAULTS = dict(fg="#EEF4FA", muted="#9DB7CC", solid="#101826", edge="rgba(255,255,255,36)", focus="#2DD4BF",
                card="rgba(255,255,255,18)", card_hover="rgba(255,255,255,28)", field="rgba(255,255,255,14)",
                accent_soft="rgba(45,212,191,56)", shadow="rgba(0,0,0,90)")
DANGER = QColor("#D45A67")
PILL = "#2563EB"  # rubber switch thumb: deep blue, white text on it stays >= 4.5:1
_OUT, _BACK = QEasingCurve(QEasingCurve.Type.OutCubic), QEasingCurve(QEasingCurve.Type.OutBack)
S = QStyle.StateFlag
_current = None
_system_dark = False


def qcolor(text):
    """QColor from a hex name or the 'rgba(r,g,b,a)' form (alpha 0-255) the tokens use."""
    if text.startswith("rgba("):
        return QColor(*(int(part) for part in text[5:-1].split(",")))
    return QColor(text)


def colors():
    return _current.c if _current else {key: qcolor(value) for key, value in DEFAULTS.items()}


def system_dark():
    """Whether the OS theme was dark before VelaStyle replaced the application palette."""
    return _system_dark


def install():
    """Install VelaStyle app-wide once; returns it."""
    global _system_dark
    app = QApplication.instance()
    if not isinstance(app.style(), VelaStyle):
        _system_dark = app.palette().window().color().lightness() < 128
        app.setStyle(VelaStyle())
    return app.style()


def _path(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect).adjusted(.5, .5, -.5, -.5), radius, radius)  # half pixel: crisp 1 px edge
    return path


def _fade(color, k):
    color = QColor(color)
    color.setAlphaF(color.alphaF() * k)
    return color


def _mix(a, b, t):
    return QColor.fromRgbF(*(x + (y - x) * t for x, y in zip(a.getRgbF(), b.getRgbF())))


def _tint(button, colour, disabled=None):
    """Label colour for a button role, via its palette (Qt stylesheets would repaint the whole box).
    The disabled group keeps the application's muted colour."""
    palette = QPalette()
    if colour is not None:
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette.setColor(group, QPalette.ColorRole.ButtonText, QColor(colour))
    if disabled is not None:
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(disabled))
    button.setPalette(palette)


def _chevron(painter, rect, color, up=False, size=3.5):
    centre = QRectF(rect).center()
    sign = -1 if up else 1
    painter.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolyline([QPointF(centre.x() - size, centre.y() - sign * size / 2),
                          QPointF(centre.x(), centre.y() + sign * size / 2),
                          QPointF(centre.x() + size, centre.y() - sign * size / 2)])


def _specular(painter, rect, path, hover, down, cursor=None, colour=ACCENT):
    """Accent gradient (smooth, no hard stop), a 1 px top highlight and, when hovered, a light spot."""
    top = QColor(colour)
    gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
    gradient.setColorAt(0, top)
    gradient.setColorAt(1, top.darker(130))
    painter.fillPath(path, QBrush(gradient))
    if hover > 0:
        spot = QColor(255, 255, 255, int(34 * hover))
        painter.save()
        painter.setClipPath(path)
        if cursor is None:
            painter.fillPath(path, spot)
        else:
            light = QRadialGradient(QPointF(cursor), 70)
            light.setColorAt(0, spot)
            light.setColorAt(1, QColor(255, 255, 255, 0))
            painter.fillRect(rect, QBrush(light))
        painter.restore()
    if down:
        painter.fillPath(path, QColor(0, 0, 0, 45))
    painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
    painter.drawLine(QPointF(rect.left() + 9, rect.top() + 1.5), QPointF(rect.right() - 8, rect.top() + 1.5))
    painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)


class GlideDelegate(QStyledItemDelegate):
    """Combo list rows; one highlight glides between rows (120 ms, snaps when motion is off)."""
    def __init__(self, combo):
        super().__init__(combo)
        self.combo, self.y = combo, None
        self.glide = QVariantAnimation(self)
        self.glide.setDuration(120)
        self.glide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.glide.valueChanged.connect(self._step)
        combo.highlighted.connect(self._to)
        combo.view().viewport().installEventFilter(self)

    def _top(self, row):
        return float(self.combo.view().visualRect(self.combo.model().index(row, 0)).top())

    def _step(self, value):
        self.y = float(value)
        self.combo.view().viewport().update()

    def _to(self, row):
        self.glide.stop()
        if self.y is not None and motion_enabled():
            self.glide.setStartValue(self.y)
            self.glide.setEndValue(self._top(row))
            self.glide.start()
        else:
            self._step(self._top(row))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Hide:
            self.glide.stop()
            self.y = None
        return False

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width() + 16, max(size.height(), 28))

    def paint(self, painter, option, index):
        view = self.combo.view()
        top = self.y if self.y is not None else self._top(max(view.currentIndex().row(), 0))
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        light = QPainterPath()
        light.addRoundedRect(QRectF(4, top + 1, view.viewport().width() - 8, option.rect.height() - 2), 7, 7)
        painter.fillPath(light, colors()["accent_soft"])
        painter.restore()
        row = QStyleOptionViewItem(option)
        row.state &= ~(S.State_Selected | S.State_MouseOver)
        row.rect = row.rect.adjusted(8, 0, -8, 0)
        super().paint(painter, row, index)


class VelaStyle(QProxyStyle):
    def __init__(self):
        super().__init__("Fusion")
        global _current
        _current = self
        self.set_tokens(DEFAULTS)

    def set_tokens(self, tokens):
        self.c = {key: qcolor(value) for key, value in tokens.items() if key in DEFAULTS}
        for widget in QApplication.allWidgets():
            if isinstance(widget, QPushButton) and widget.objectName() == "textButton":
                self.tint(widget)
        for widget in QApplication.topLevelWidgets():
            widget.update()

    def tint(self, button):
        """Label colour by role: white on primary and on the checked pill, accent links, red danger."""
        name = button.objectName()
        if name == "seg" and button.isChecked():
            return _tint(button, "white", "#EAF1FF")  # stays ≥4.5:1 on the pill even while locked
        _tint(button, {"primary": "white", "textButton": self._link(), "danger": DANGER}.get(name))

    def _link(self):
        """Accent for text buttons, shifted per theme so it keeps 4.5:1 on the glass."""
        return "#5EEAD4" if self.c["fg"].lightness() > 128 else "#0F766E"

    def polish(self, obj):
        super().polish(obj)
        if isinstance(obj, QPushButton):
            self.tint(obj)
            if obj.objectName() not in ("seg", "textButton"):
                track_hover(obj, follow=obj.objectName() == "primary")
        elif isinstance(obj, (QComboBox, QScrollBar)):
            track_hover(obj)
            if isinstance(obj, QComboBox) and obj.findChild(GlideDelegate) is None and not obj.isEditable():
                obj.setItemDelegate(GlideDelegate(obj))
        elif isinstance(obj, QCheckBox):
            track_check(obj)

    def unpolish(self, obj):
        if isinstance(obj, (QPushButton, QComboBox, QScrollBar)):
            untrack_hover(obj)
        elif isinstance(obj, QCheckBox):
            untrack_check(obj)
        super().unpolish(obj)

    def pixelMetric(self, metric, option=None, widget=None):
        fixed = {QStyle.PixelMetric.PM_ScrollBarExtent: 10, QStyle.PixelMetric.PM_ScrollBarSliderMin: 30,
                 QStyle.PixelMetric.PM_IndicatorWidth: 14, QStyle.PixelMetric.PM_IndicatorHeight: 14,
                 QStyle.PixelMetric.PM_CheckBoxLabelSpacing: 7}
        return fixed.get(metric, super().pixelMetric(metric, option, widget))

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0  # plain list below the field, drawn by GlideDelegate
        return super().styleHint(hint, option, widget, returnData)

    def sizeFromContents(self, kind, option, size, widget=None):
        size = super().sizeFromContents(kind, option, size, widget)
        T = QStyle.ContentsType
        if kind == T.CT_PushButton:
            if widget is not None and widget.objectName() == "seg":
                return QSize(max(size.width() + 12, 64), 26)
            return QSize(size.width() + 10, max(size.height() + 8, 32))
        if kind in (T.CT_ComboBox, T.CT_LineEdit, T.CT_SpinBox):
            return QSize(size.width() + (12 if kind == T.CT_ComboBox else 0), max(size.height(), 30))
        return size

    def subControlRect(self, control, option, sub, widget=None):
        C, SC = QStyle.ComplexControl, QStyle.SubControl
        rect = option.rect
        if control == C.CC_ComboBox and sub == SC.SC_ComboBoxArrow:
            return QRect(rect.right() - 23, rect.top(), 24, rect.height())
        if control == C.CC_ComboBox and sub == SC.SC_ComboBoxEditField:
            return rect.adjusted(9, 2, -26, -2)
        if control == C.CC_ScrollBar:
            if sub in (SC.SC_ScrollBarAddLine, SC.SC_ScrollBarSubLine):
                return QRect()
            horizontal = option.orientation == Qt.Orientation.Horizontal
            length = rect.width() if horizontal else rect.height()
            span = max(option.maximum - option.minimum, 0)
            size = min(length, max(self.pixelMetric(QStyle.PixelMetric.PM_ScrollBarSliderMin),
                                   int(length * option.pageStep / (span + option.pageStep or 1))))
            at = QStyle.sliderPositionFromValue(option.minimum, option.maximum, option.sliderPosition,
                                                length - size, option.upsideDown)
            cut = {SC.SC_ScrollBarSlider: (at, size), SC.SC_ScrollBarSubPage: (0, at),
                   SC.SC_ScrollBarAddPage: (at + size, length - at - size), SC.SC_ScrollBarGroove: (0, length)}
            if sub in cut:
                start, extent = cut[sub]
                return QRect(start, 0, extent, rect.height()) if horizontal else QRect(0, start, rect.width(), extent)
        return super().subControlRect(control, option, sub, widget)

    def drawPrimitive(self, element, option, painter, widget=None):
        P, c = QStyle.PrimitiveElement, self.c
        if element == P.PE_PanelButtonCommand:
            return self._button(option, painter, widget)
        if element == P.PE_PanelLineEdit:
            if option.lineWidth:
                self._field(option, painter, bool(option.state & S.State_HasFocus))
            return None
        if element == P.PE_FrameLineEdit:
            return None
        if element == P.PE_Frame and isinstance(widget, (QTextEdit, QListWidget)):
            return self._field(option, painter, bool(option.state & S.State_HasFocus))
        if element == P.PE_IndicatorCheckBox:
            return self._check(option, painter, widget)
        if element == P.PE_PanelTipLabel:  # warm tooltip: solid, rounded, accent-tinted edge
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.fillPath(_path(option.rect, 7), c["solid"])
            painter.setPen(QPen(_mix(c["edge"], c["focus"], .6), 1))
            painter.drawPath(_path(option.rect, 7))
            painter.restore()
            return None
        if element == P.PE_FrameFocusRect:
            if option.state & S.State_KeyboardFocusChange:
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(c["focus"], 1.5))
                painter.drawPath(_path(option.rect.adjusted(-1, -1, 1, 1), 6))
                painter.restore()
            return None
        if element == P.PE_FrameMenu or (element == P.PE_Frame and isinstance(widget, QWidget)
                                         and widget.parent() is not None and isinstance(widget.parent(), QComboBox)):
            painter.save()
            painter.setPen(QPen(c["edge"], 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(option.rect).adjusted(.5, .5, -.5, -.5))
            painter.restore()
            return None
        return super().drawPrimitive(element, option, painter, widget)

    def drawComplexControl(self, control, option, painter, widget=None):
        C, c = QStyle.ComplexControl, self.c
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            if control == C.CC_ComboBox:
                self._field(option, painter, bool(option.state & (S.State_HasFocus | S.State_On)),
                            progress(widget) or 0.0)
                arrow = self.subControlRect(control, option, QStyle.SubControl.SC_ComboBoxArrow, widget)
                _chevron(painter, arrow, c["fg"] if option.state & S.State_Enabled else c["muted"],
                         bool(widget is not None and widget.property("vtOpen")))  # flips while its menu is open
                return None
            if control == C.CC_SpinBox:
                self._field(option, painter, bool(option.state & S.State_HasFocus))
                for sub, up in ((QStyle.SubControl.SC_SpinBoxUp, True), (QStyle.SubControl.SC_SpinBoxDown, False)):
                    _chevron(painter, self.subControlRect(control, option, sub, widget),
                             c["fg"] if option.state & S.State_Enabled else c["muted"], up, 3)
                return None
            if control == C.CC_ScrollBar:
                slider = self.subControlRect(control, option, QStyle.SubControl.SC_ScrollBarSlider, widget)
                thick = 6 + 2 * (progress(widget) or 0.0)
                horizontal = option.orientation == Qt.Orientation.Horizontal
                bar = (QRectF(slider.left(), slider.center().y() + .5 - thick / 2, slider.width(), thick) if horizontal
                       else QRectF(slider.center().x() + .5 - thick / 2, slider.top(), thick, slider.height()))
                if option.maximum > option.minimum:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(_fade(c["muted"], .4 + .3 * (progress(widget) or 0.0)))
                    painter.drawRoundedRect(bar, thick / 2, thick / 2)
                return None
        finally:
            painter.restore()
        return super().drawComplexControl(control, option, painter, widget)

    def _field(self, option, painter, focus, hover=0.0):
        """Rounded field with a crisp 1 px edge; focus adds an accent edge and a faint inner glow."""
        c, on = self.c, bool(option.state & S.State_Enabled)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = _path(option.rect, 9)
        painter.fillPath(path, _fade(c["field"], 1 if on else .5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if focus and on:
            painter.setPen(QPen(c["accent_soft"], 3))
            painter.drawPath(_path(option.rect.adjusted(1, 1, -1, -1), 8))
        painter.setPen(QPen(c["focus"] if focus and on else _mix(_fade(c["edge"], 1 if on else .5),
                                                                   c["focus"], .7 * hover if on else 0), 1))
        painter.drawPath(path)
        painter.restore()

    def _button(self, option, painter, widget):
        name = widget.objectName() if widget is not None else ""
        if name in ("seg", "textButton"):
            return
        c, state = self.c, option.state
        on, down = bool(state & S.State_Enabled), bool(state & (S.State_Sunken | S.State_On))
        hover = (progress(widget) or 0.0) if on else 0.0
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = _path(option.rect, 9)
        if name == "primary" and on:
            cursor = widget.mapFromGlobal(QCursor.pos()) if hover and motion_enabled() else None
            _specular(painter, QRectF(option.rect), path, hover, down, cursor)
        else:
            painter.fillPath(path, _fade(_mix(c["card"], c["card_hover"], hover), 1 if on else .8))
            if down and on:
                painter.fillPath(path, QColor(0, 0, 0, 40))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if hover:  # border glow: soft accent ring just inside the edge
                painter.setPen(QPen(_fade(c["focus"], .3 * hover), 2.5))
                painter.drawPath(_path(option.rect.adjusted(1, 1, -1, -1), 8))
            edge = DANGER if name == "danger" and on else _mix(_fade(c["edge"], 1 if on else .7), c["focus"], .8 * hover)
            painter.setPen(QPen(edge, 1))
            painter.drawPath(path)
        painter.restore()

    def _check(self, option, painter, widget):
        c = self.c
        t = progress(widget, "vtCheck")
        if t is None:
            t = 1.0 if option.state & S.State_On else 0.0
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = _path(option.rect, 4)
        painter.fillPath(path, _mix(c["field"], QColor(ACCENT), t))
        painter.setPen(QPen(_mix(c["muted"], QColor(ACCENT), t), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if t > 0:  # the tick draws itself: first stroke, then second
            x, y = option.rect.left(), option.rect.top()
            points = [QPointF(x + 3.5, y + 7.5), QPointF(x + 6, y + 10), QPointF(x + 10.5, y + 4.5)]
            reach = t * 2
            painter.setPen(QPen(QColor("white"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(points[0], points[0] + (points[1] - points[0]) * min(reach, 1))
            if reach > 1:
                painter.drawLine(points[1], points[1] + (points[2] - points[1]) * (reach - 1))
        painter.restore()


class SegmentedSwitch(QWidget):
    """Rubber segment: one rounded track; the accent thumb stretches across to the checked segment
    (leading edge first, trailing edge catching up) and squashes into place."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Mode")
        self.keyboard = False  # focus ring only after Tab, not after a click or window activation
        self.buttons = []
        self.pill = QRectF()
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(3, 3, 3, 3)
        self._row.setSpacing(0)
        self._from = self._to = QRectF()
        self._glide = QVariantAnimation(self, duration=420, startValue=0.0, endValue=1.0)
        self._glide.valueChanged.connect(self._rubber)

    def add(self, text):
        button = QPushButton(text)
        button.setObjectName("seg")
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.toggled.connect(lambda on: self._slide(button) if on else None)
        button.toggled.connect(lambda _: _current.tint(button))
        self._row.addWidget(button)
        self.buttons.append(button)
        return button

    def _target(self):
        checked = [b for b in self.buttons if b.isChecked()]
        return QRectF(checked[0].geometry()) if checked else QRectF()

    def _slide(self, button):
        self._glide.stop()
        target = QRectF(button.geometry())
        if motion_enabled() and self.isVisible() and not self.pill.isEmpty() and self.pill != target:
            self._from, self._to = QRectF(self.pill), target
            self._glide.start()
        else:
            self._move(target)

    def _rubber(self, t):
        a, b = self._from, self._to
        lead = _OUT.valueForProgress(min(t / .4, 1.0))
        trail = _BACK.valueForProgress(min(max((t - .2) / .6, 0.0), 1.0))
        rightward = b.left() > a.left()
        left = a.left() + (b.left() - a.left()) * (trail if rightward else lead)
        right = a.right() + (b.right() - a.right()) * (lead if rightward else trail)
        squash = 1.5 * math.sin(math.pi * min(max((t - .55) / .45, 0.0), 1.0))  # wider and shorter on landing
        self._move(QRectF(QPointF(left - squash, b.top() + squash), QPointF(right + squash, b.bottom() - squash)))

    def _move(self, rect):
        self.pill = QRectF(rect)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._glide.stop()
        self._move(self._target())

    def focusInEvent(self, event):
        self.keyboard = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def keyPressEvent(self, event):
        step = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}.get(event.key())
        current = next((i for i, b in enumerate(self.buttons) if b.isChecked()), 0)
        if step and 0 <= current + step < len(self.buttons) and self.buttons[current + step].isEnabled():
            return self.buttons[current + step].click()
        super().keyPressEvent(event)

    def paintEvent(self, event):
        c = colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = _path(self.rect(), 10)
        painter.fillPath(track, c["field"])
        painter.setPen(QPen(c["focus"] if self.hasFocus() and self.keyboard else c["edge"], 1))
        painter.drawPath(track)
        if not self.pill.isEmpty():
            pill = QPainterPath()
            pill.addRoundedRect(self.pill, 7, 7)
            _specular(painter, self.pill, pill, 0, False, colour=PILL)


class SpotlightCard(QFrame):
    """Glass card painted here (crisp antialiased edge) with a soft accent light under the cursor
    while hovered, drawn below the text. Fill and edge ease with hover."""
    def __init__(self, parent=None):
        super().__init__(parent)
        track_hover(self, follow=True)

    def paintEvent(self, event):
        c, hover = colors(), progress(self) or 0.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = _path(self.rect(), 14)
        painter.fillPath(path, _mix(c["card"], c["card_hover"], hover))
        if hover > 0 and motion_enabled():
            light = QRadialGradient(QPointF(self.mapFromGlobal(QCursor.pos())), 150)
            light.setColorAt(0, _fade(c["focus"], .2 * hover))
            light.setColorAt(1, _fade(c["focus"], 0))
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(self.rect(), QBrush(light))
            painter.restore()
        painter.setPen(QPen(_mix(c["edge"], c["focus"], .5 * hover), 1))
        painter.drawPath(path)
