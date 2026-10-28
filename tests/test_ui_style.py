"""Offscreen tests for VelaStyle, the hover/check helpers and the segmented switch."""
import gc
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

try:
    from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QRectF, Qt
    from PySide6.QtGui import QKeyEvent, QShortcut
    from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton
    from velatrace.ui import MainWindow, glass_stylesheet, theme_tokens
    from velatrace.ui_effects import progress, set_motion, track_hover
    from velatrace.ui_style import SegmentedSwitch, VelaStyle
except ImportError:
    QApplication = None


def luminance(color):
    return .299 * color.red() + .587 * color.green() + .114 * color.blue()


@unittest.skipIf(QApplication is None, "Install the pinned Qt UI dependency")
class UiStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_motion(True)
        self.temp = tempfile.TemporaryDirectory()
        self.window = MainWindow(demo=True, config_dir=Path(self.temp.name))

    def tearDown(self):
        set_motion(True)
        self.window.safety = None
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.window = None
        gc.collect()
        self.temp.cleanup()

    def test_window_installs_vela_style(self):
        self.assertIsInstance(QApplication.style(), VelaStyle)

    def test_stylesheet_no_longer_draws_controls(self):
        for dark in (False, True):
            sheet = glass_stylesheet(theme_tokens(dark))
            for name in ("QPushButton", "QComboBox", "QLineEdit", "QCheckBox", "QScrollBar", "QSpinBox"):
                self.assertNotIn(name, sheet)

    def test_tokens_have_edge_focus_and_soft_accent(self):
        for dark in (False, True):
            self.assertTrue({"edge", "focus", "accent_soft", "shadow"} <= set(theme_tokens(dark)))

    def test_segmented_switch_routes_clicks_shortcuts_and_arrow_keys(self):
        window = self.window
        window.demo = False  # the demo gates the mode buttons
        window.refresh_actions()
        window.show()
        self.app.processEvents()
        from velatrace.routing import Mode
        window.run_command = Mock()
        switch, route = window.mode_switch, window.mode_buttons[Mode.ROUTING]
        route.click()
        window.run_command.assert_called_with("/autoroute")
        self.assertTrue(route.isChecked())
        self.assertEqual(switch._glide.endValue(), QRectF(route.geometry()))
        window.mode_buttons[Mode.AUDIT].click()
        window.run_command.reset_mock()
        next(s for s in window.findChildren(QShortcut) if s.key().toString() == "Ctrl+2").activated.emit()
        window.run_command.assert_called_with("/autoroute")
        window.mode_buttons[Mode.AUDIT].setChecked(True)
        window.run_command.reset_mock()
        switch.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        window.run_command.assert_called_with("/autoroute")
        self.assertEqual(switch.accessibleName(), "Mode")
        self.assertIn("Ctrl+2", route.toolTip())

    def test_motion_off_pill_and_hover_snap(self):
        set_motion(False)
        switch = self.window.mode_switch
        self.window.show()
        self.app.processEvents()
        from velatrace.routing import Mode
        self.window.set_mode_buttons(Mode.ROUTING)
        self.assertNotEqual(switch._glide.state(), QAbstractAnimation.State.Running)
        self.assertEqual(switch.pill, QRectF(self.window.mode_buttons[Mode.ROUTING].geometry()))

    def test_hover_helper_snaps_when_motion_is_off(self):
        button = QPushButton("x")
        button.show()
        hover = track_hover(button)
        set_motion(False)
        QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
        self.assertEqual(progress(button), 1.0)
        self.assertNotEqual(hover.ease.anim.state(), QAbstractAnimation.State.Running)
        QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
        self.assertEqual(progress(button), 0.0)
        set_motion(True)
        QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
        self.assertEqual(hover.ease.anim.state(), QAbstractAnimation.State.Running)
        button.close()

    def test_checkbox_tick_snaps_when_motion_is_off(self):
        box = QCheckBox("x")
        box.show()
        self.app.processEvents()
        set_motion(False)
        box.setChecked(True)
        self.assertEqual(progress(box, "vtCheck"), 1.0)
        box.close()

    def test_both_themes_render_and_light_buttons_have_a_visible_edge(self):
        window = self.window
        window.show()
        for choice in ("Light", "Dark"):
            window.theme.setCurrentText(choice)
            self.app.processEvents()
            image = window.grab().toImage()
            self.assertFalse(image.isNull())
            if choice == "Light":
                button = window.restart_button
                left = button.mapTo(window, QPoint(0, button.height() // 2))
                edge = image.pixelColor(left.x(), left.y())
                outside = image.pixelColor(left.x() - 4, left.y())
                self.assertGreaterEqual(abs(luminance(edge) - luminance(outside)), 12)


if __name__ == "__main__":
    unittest.main()
