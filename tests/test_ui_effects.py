"""Offscreen tests for the motion polish; one setting must turn every effect off."""
import gc
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path

try:
    from PySide6.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QLabel
    from PySide6.QtCore import QAbstractAnimation
    from velatrace import ui_effects
    from velatrace.ui import MainWindow, Settings
    from velatrace.ui_effects import ShimmerLabel, StepStrip, reveal, set_motion
except ImportError:
    QApplication = None


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(.005)


@unittest.skipIf(QApplication is None, "Install the pinned Qt UI dependency")
class UiEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_motion(True)
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        set_motion(True)
        gc.collect()
        self.temp.cleanup()

    def cards(self, count=3):
        host = QFrame()
        host.show()
        cards = [QLabel(f"card {i}", host) for i in range(count)]
        for card in cards:
            card.show()
        return host, cards

    def test_motion_off_reveal_is_inert(self):
        set_motion(False)
        host, cards = self.cards()
        reveal(cards)
        self.assertTrue(all(card.graphicsEffect() is None for card in cards))
        self.assertEqual(host.findChildren(QAbstractAnimation), [])

    def test_motion_on_reveal_finishes_and_removes_effect(self):
        host, cards = self.cards()
        reveal(cards)
        self.assertTrue(all(card.isVisible() for card in cards))
        self.assertTrue(all(isinstance(card.graphicsEffect(), QGraphicsOpacityEffect) for card in cards))
        pump(self.app, .4)
        self.assertTrue(all(card.graphicsEffect() is None for card in cards))

    def test_shimmer_timer_follows_start_stop_hide_and_motion(self):
        label = ShimmerLabel("Working")
        label.show()
        label.start()
        self.assertTrue(label._timer.isActive())
        label.hide()
        self.assertFalse(label._timer.isActive())
        label.show()
        self.assertTrue(label._timer.isActive())
        label.stop()
        self.assertFalse(label._timer.isActive())
        set_motion(False)
        label.start()
        self.assertFalse(label._timer.isActive())

    def test_shimmer_paints_and_honours_stylesheet_colour(self):
        label = ShimmerLabel("Working")
        label.show()
        label.start()
        before = label.grab().toImage()
        label._phase = .5
        self.assertNotEqual(label.grab().toImage(), before)
        label.setStyleSheet("color:#D45A67")
        plain = label.grab().toImage()
        label._phase = .1
        self.assertEqual(label.grab().toImage(), plain)

    def test_step_strip_snaps_without_motion_and_fades_with_it(self):
        strip = StepStrip()
        strip.show()
        set_motion(False)
        strip.set_step(3)
        self.assertEqual(strip._pos, 3.0)
        self.assertNotEqual(strip._fade.state(), QAbstractAnimation.State.Running)
        set_motion(True)
        strip.set_step(4)
        self.assertEqual(strip._fade.state(), QAbstractAnimation.State.Running)
        pump(self.app, .3)
        self.assertEqual(strip._pos, 4.0)

    def test_window_steps_follow_route_state(self):
        from velatrace.routing import RoutingStage
        from test_ui import UiTests
        helper = UiTests("test_approve_button_applies_once_without_regenerating_preview")
        helper.temp, helper.window = self.temp, MainWindow(demo=True, config_dir=Path(self.temp.name))
        window = helper.window
        try:
            self.assertEqual(window.step_strip.step, 1)
            session, *_ = helper.ready_route_preview()
            self.assertEqual(window.step_strip.step, 4)
            session.report = type(session.report)(session.report.plan_digest, 2, 0,
                                                  board_digest=session.report.board_digest)
            window.refresh_actions()
            self.assertEqual(window.step_strip.step, 3)
            session.stage = RoutingStage.VALIDATING
            window._drc_pending = 1
            window.refresh_actions()
            self.assertEqual(window.step_strip.step, 3)
            window.preview_shown = False
            window.refresh_actions()
            self.assertEqual(window.step_strip.step, 2)
            window._drc_pending = 0
            session.stage = RoutingStage.REJECTED
            window.refresh_actions()
            self.assertEqual(window.step_strip.step, 1)
        finally:
            window.safety = None
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_motion_setting_round_trips(self):
        path = Path(self.temp.name) / "settings.json"
        Settings(motion=False).save(path)
        loaded = Settings()
        loaded.load(path)
        self.assertFalse(loaded.motion)

    def test_demo_window_renders_in_both_themes_and_empty_state_hides(self):
        window = MainWindow(demo=True, config_dir=Path(self.temp.name))
        try:
            window.show()
            self.assertTrue(window.empty.isHidden())
            self.assertTrue(window.edge_fade.testAttribute(
                ui_effects.Qt.WidgetAttribute.WA_TransparentForMouseEvents))
            for choice in ("Light", "Dark"):
                window.theme.setCurrentText(choice)
                self.app.processEvents()
                self.assertFalse(window.grab().isNull())
                window.pages.setCurrentIndex(1)
                self.assertFalse(window.grab().isNull())
                window.pages.setCurrentIndex(0)
            window.audit = window.audit_snapshot = None
            window.render_cards()
            self.assertFalse(window.empty.isHidden())
            self.assertFalse(window.grab().isNull())
        finally:
            window.safety = None
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
