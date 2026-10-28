"""Small, self-written motion polish for the VelaTrace window.

One switch (set_motion) turns every animation off. Static decorations stay.
No effect runs a timer while its widget is hidden or idle."""
from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QPropertyAnimation, QRectF, Qt, QTimer,
                            QVariantAnimation, QPauseAnimation, QSequentialAnimationGroup)
from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter, QPen, QPolygonF, QBrush,
                           QRadialGradient)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget

ACCENT = "#8B5CF6"  # ui.py imports it from here
_motion = True


def motion_enabled():
    return _motion


def set_motion(on):
    global _motion
    _motion = bool(on)


def _mix(a, b, t):
    """Blend colour a toward b by t in 0..1, alpha included."""
    return QColor.fromRgbF(*(x + (y - x) * t for x, y in zip(a.getRgbF(), b.getRgbF())))


def paint_dot_grid(painter, rect, color, pitch=12):
    """Faint static dots, 1.2 px, one per `pitch` pixels."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawPoints(QPolygonF([QPointF(x, y)
                                  for x in range(rect.left() + pitch // 2, rect.right(), pitch)
                                  for y in range(rect.top() + pitch // 2, rect.bottom(), pitch)]))
    painter.restore()


class StepStrip(QWidget):
    """Route · Preview · KiCad DRC · Approve. set_step(n): n is the current step (1-4);
    earlier steps show a tick, later ones are muted outlines; 0 means nothing started."""
    NAMES = ("Route", "Preview", "KiCad DRC", "Approve")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.step = 0
        self._pos = 0.0  # animated step position; equals self.step when settled
        self._tokens = {}
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(150)
        self._fade.valueChanged.connect(self._moved)
        self.setAccessibleName("Routing steps")

    def set_tokens(self, tokens):
        self._tokens = tokens
        self.update()

    def set_step(self, n):
        n = max(0, min(4, n))
        if n == self.step:
            return
        self.step = n
        self.setAccessibleDescription(f"Step {n}: {self.NAMES[n - 1]}" if n else "Not started")
        self._fade.stop()
        if motion_enabled() and self.isVisible():
            self._fade.setStartValue(self._pos)
            self._fade.setEndValue(float(n))
            self._fade.start()
        else:
            self._pos = float(n)
            self.update()

    def _moved(self, value):
        self._pos = float(value)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        muted = QColor(self._tokens.get("muted", "#B7B1CF"))
        fg = QColor(self._tokens.get("fg", "#F5F3FF"))
        accent = QColor(ACCENT)
        faint = QColor(muted)
        faint.setAlpha(90)
        seg, radius, cy = self.width() / 4, 9, self.height() / 2
        metrics = painter.fontMetrics()
        for i, name in enumerate(self.NAMES):
            n = i + 1
            cx = i * seg + radius + 1
            now = max(0.0, 1 - abs(n - self._pos))
            done = min(1.0, max(0.0, self._pos - n))
            edge = _mix(muted, accent, max(now, done))
            fill = QColor(accent)
            fill.setAlphaF(now)
            painter.setPen(QPen(edge, 1.5))
            painter.setBrush(fill)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)
            if done > .5:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolyline(QPolygonF([QPointF(cx - 4, cy), QPointF(cx - 1, cy + 3),
                                                QPointF(cx + 4, cy - 3)]))
            else:
                painter.setPen(QColor("white") if now > .5 else muted)
                painter.drawText(QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius),
                                 Qt.AlignmentFlag.AlignCenter, str(n))
            text_x = cx + radius + 6
            text = metrics.elidedText(name, Qt.TextElideMode.ElideRight, max(0, int(seg - (text_x - i * seg) - 10)))
            painter.setPen(_mix(muted, fg, now))
            painter.drawText(QPointF(text_x, cy + metrics.ascent() / 2 - 1), text)
            start, end = text_x + metrics.horizontalAdvance(text) + 6, (i + 1) * seg - 5
            if i < 3 and end - start > 6:
                painter.setPen(QPen(_mix(faint, accent, done), 1.5))
                painter.drawLine(QPointF(start, cy), QPointF(end, cy))


def reveal(widgets):
    """Cards fade from 0.4 to 1.0 in 160 ms, 20 ms apart (max 120). Opacity only: a y-offset
    would fight the layout. Each animation is a child of its card, so clearing the card ends it."""
    if not _motion:
        return
    for i, widget in enumerate(widgets[:24]):  # cards further down are off screen
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(.4)
        widget.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", widget)
        fade.setDuration(160)
        fade.setStartValue(.4)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        run = QSequentialAnimationGroup(widget)
        run.addAnimation(QPauseAnimation(min(20 * i, 120)))
        run.addAnimation(fade)
        run.finished.connect(lambda w=widget: w.setGraphicsEffect(None))
        run.start(QSequentialAnimationGroup.DeletionPolicy.DeleteWhenStopped)


class _Overlay(QWidget):
    """Mouse-transparent layer that follows a scroll area's viewport."""
    def __init__(self, area):
        super().__init__(area.viewport())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._view = area.viewport()
        self._view.installEventFilter(self)
        self.tokens = {}
        self.setGeometry(self._view.rect())
        self.raise_()

    def set_tokens(self, tokens):
        self.tokens = tokens
        self.update()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(self._view.rect())
            self.raise_()
        return False


class EdgeFade(_Overlay):
    """24 px fade into the window colour where more content is above or below."""
    SIZE = 24

    def __init__(self, area):
        super().__init__(area)
        self._bar = area.verticalScrollBar()
        self._bar.valueChanged.connect(self._refresh)
        self._bar.rangeChanged.connect(self._refresh)

    def _refresh(self, *_):
        self.update()

    def paintEvent(self, event):
        bar = self._bar
        solid = QColor(self.tokens.get("solid", "#1C1930"))
        solid.setAlpha(200)
        clear = QColor(solid)
        clear.setAlpha(0)
        painter = QPainter(self)
        for top, show in ((True, bar.value() > bar.minimum()), (False, bar.value() < bar.maximum())):
            if show:
                y = 0 if top else self.height() - self.SIZE
                gradient = QLinearGradient(0, y, 0, y + self.SIZE)
                gradient.setColorAt(0, solid if top else clear)
                gradient.setColorAt(1, clear if top else solid)
                painter.fillRect(0, y, self.width(), self.SIZE, QBrush(gradient))


class EmptyState(_Overlay):
    """Dot grid fading out toward the edges, with one muted line in the middle."""
    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        image = QImage(self.size() * ratio, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        dots = QPainter(image)
        dot = QColor(self.tokens.get("fg", "#F5F3FF"))
        dot.setAlpha(90)
        paint_dot_grid(dots, self.rect(), dot)
        mask = QRadialGradient(QPointF(self.rect().center()), max(self.width(), self.height()) / 2)
        mask.setColorAt(0, QColor(0, 0, 0, 255))
        mask.setColorAt(1, QColor(0, 0, 0, 0))
        dots.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        dots.fillRect(self.rect(), QBrush(mask))
        dots.end()
        painter = QPainter(self)
        painter.drawImage(0, 0, image)
        painter.setPen(QColor(self.tokens.get("muted", "#B7B1CF")))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Findings will appear here after Check design.")


class ShimmerLabel(QLabel):
    """Status text with a soft highlight sweeping across it while start()ed. The timer runs
    only while started, visible and motion is on; a stylesheet colour (errors) or a stopped
    label paints as a plain QLabel."""
    def __init__(self, text=""):
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self._started = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._started = True
        self._sync()

    def stop(self):
        self._started = False
        self._sync()
        self.update()

    def _sync(self):
        run = self._started and self.isVisible() and _motion
        if run and not self._timer.isActive():
            self._timer.start()
        elif not run:
            self._timer.stop()

    def _tick(self):
        if not _motion:
            return self._sync()
        self._phase = (self._phase + 33 / 2400) % 1.0
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event):
        if not self._timer.isActive() or self.styleSheet():
            return super().paintEvent(event)
        base = self.palette().windowText().color()
        # A same-hue lighten/darken is invisible on near-white / near-black text, so the band leans accent.
        band = _mix(base, QColor(ACCENT), .5)
        area = self.contentsRect()
        width = max(60.0, area.width() * .3)
        left = area.left() - width + self._phase * (area.width() + width)
        gradient = QLinearGradient(left, 0, left + width, 0)
        gradient.setColorAt(0, base)
        gradient.setColorAt(.5, band)
        gradient.setColorAt(1, base)
        painter = QPainter(self)
        painter.setPen(QPen(QBrush(gradient), 1))
        painter.setFont(self.font())
        painter.drawText(area, int(self.alignment()) | Qt.TextFlag.TextWordWrap, self.text())


class Ease(QObject):
    """Moves a float property of its parent widget toward a target in 120 ms, repainting each
    step. Motion off (or a hidden widget) snaps. Runs only between a transition's start and end."""
    def __init__(self, widget, name, duration=120):
        super().__init__(widget)
        self.setObjectName(name)
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(duration)
        self.anim.valueChanged.connect(self._set)

    def go(self, target):
        widget = self.parent()
        self.anim.stop()
        if _motion and widget.isVisible():
            self.anim.setStartValue(float(widget.property(self.objectName()) or 0.0))
            self.anim.setEndValue(float(target))
            self.anim.start()
        else:
            self._set(target)

    def _set(self, value):
        widget = self.parent()
        widget.setProperty(self.objectName(), float(value))
        widget.update()


def progress(widget, name="vtHover"):
    """The widget's current eased value, or None if it is not tracked."""
    value = widget.property(name) if widget is not None else None
    return float(value) if value is not None else None


class Hover(QObject):
    """Event filter easing `vtHover` to 1 on Enter and 0 on Leave. With follow=True it also
    repaints on mouse moves while hovered (mouse tracking is on only then), for cursor lights."""
    def __init__(self, widget, follow=False):
        super().__init__(widget)
        self.setObjectName("vtHoverFilter")
        self.follow = follow
        self.ease = Ease(widget, "vtHover")
        widget.setProperty("vtHover", 0.0)
        widget.installEventFilter(self)

    def _watch(self, on):
        widget = self.parent()
        for child in (widget, *widget.findChildren(QWidget)):
            child.setMouseTracking(on)
            if child is not widget:
                child.installEventFilter(self) if on else child.removeEventFilter(self)

    def eventFilter(self, obj, event):
        kind = event.type()
        if obj is self.parent() and kind in (QEvent.Type.Enter, QEvent.Type.Leave):
            on = kind == QEvent.Type.Enter
            self.ease.go(1.0 if on else 0.0)
            if self.follow and (_motion or not on):
                self._watch(on)
        elif kind == QEvent.Type.MouseMove and self.follow and _motion:
            self.parent().update()
        return False


def track_hover(widget, follow=False):
    hover = widget.findChild(Hover, "vtHoverFilter")
    if hover is None:
        hover = Hover(widget, follow)
    hover.follow = follow
    return hover


def untrack_hover(widget):
    hover = widget.findChild(Hover, "vtHoverFilter")
    if hover is not None:
        hover._watch(False)
        widget.removeEventFilter(hover)
        hover.ease.anim.stop()
        hover.setParent(None)
        hover.deleteLater()
        widget.setProperty("vtHover", None)


def track_check(widget):
    """Eases `vtCheck` with the checked state, for the self-drawing tick."""
    if widget.findChild(Ease, "vtCheck") is None:
        ease = Ease(widget, "vtCheck", 140)
        widget.setProperty("vtCheck", 1.0 if widget.isChecked() else 0.0)
        widget.toggled.connect(lambda on: ease.go(1.0 if on else 0.0))


def untrack_check(widget):
    ease = widget.findChild(Ease, "vtCheck")
    if ease is not None:
        ease.anim.stop()
        ease.setParent(None)
        ease.deleteLater()
        widget.setProperty("vtCheck", None)

