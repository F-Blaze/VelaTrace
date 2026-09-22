"""Offscreen UI gates and worker-boundary tests; no provider or KiCad writes."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from velatrace.ui import MainWindow, ComponentCard, RouteCanvas
except ImportError:
    QApplication = None

from velatrace.audit import AuditStage
from velatrace.flags import Function
from velatrace.models import Component
from velatrace.ses import RoutePlan, Track


@unittest.skipIf(QApplication is None, "Install the pinned Qt UI dependency")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = MainWindow(demo=True, config_dir=Path(self.temp.name))

    def tearDown(self):
        self.window.safety = None
        self.wait_idle()
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while self.window.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertIsNone(self.window.worker)
        self.app.processEvents()

    def test_model_and_board_text_never_becomes_html(self):
        card = ComponentCard(Component("<b>R1</b>", "<img src=x>", "test", ()),
                             Function("<a href=evil>click</a>", "role", 1))
        labels = card.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)
        self.assertTrue(all(item.textFormat() == Qt.TextFormat.PlainText for item in labels))
        self.assertTrue(any("<a href=evil>" in item.text() for item in labels))

    def test_all_service_jobs_share_one_worker_and_results_return_to_ui_thread(self):
        threads, callbacks = [], []
        for _ in range(2):
            self.window.run_work("test", lambda usage: threads.append(threading.get_ident()),
                                 lambda value: callbacks.append(threading.get_ident()))
            self.wait_idle()
        self.assertEqual(threads[0], threads[1])
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertEqual(callbacks, [threading.get_ident()] * 2)

    def test_refused_privacy_notice_never_starts_inference(self):
        self.window.audit.stage = AuditStage.FUNCTIONS
        self.window.settings.model = "test-model"
        with patch("velatrace.ui.ask", return_value=False), patch.object(self.window.audit, "infer_functions") as infer:
            self.window.audit_next()
            infer.assert_not_called()
            self.assertIsNone(self.window.worker)

    def test_function_edits_cannot_bypass_confirmation(self):
        self.window.audit.stage = AuditStage.REVIEW_FUNCTIONS
        self.window.render_cards()
        self.window.cards["U1"].function_edit.setText("Corrected MCU role")
        with patch("velatrace.ui.ask", return_value=False), patch.object(self.window.audit, "estimate_classification") as count:
            self.window.audit_next()
        self.assertEqual(self.window.audit.functions["U1"].text, "Corrected MCU role")
        self.assertEqual(self.window.audit.stage, AuditStage.REVIEW_FUNCTIONS)
        count.assert_not_called()

    def test_pending_prompt_prevents_routing(self):
        self.window.route_prompt.setText("keep traces away from headers")
        self.window.routing = SimpleNamespace()
        with patch("velatrace.ui.ask") as ask:
            self.window.route()
        ask.assert_not_called()
        self.assertIn("pending routing prompt", self.window.status.text())
        self.window.routing = None

    def test_cleanup_failure_keeps_window_and_safety_handle(self):
        class Safety:
            def clear_preview(self):
                raise RuntimeError("Backup refused")
        self.window.safety = Safety()
        self.window.show()
        self.window.close()
        self.wait_idle()
        self.assertTrue(self.window.isVisible())
        self.assertIsNotNone(self.window.safety)
        self.assertIn("Backup refused", self.window.status.text())

    def test_multilayer_canvas_renders_actual_polylines(self):
        canvas = RouteCanvas()
        canvas.resize(600, 240)
        canvas.plan = RoutePlan("board", (
            Track("VCC", "F.Cu", .25, ((1, 1), (5, 2), (8, 2))),
            Track("GND", "B.Cu", .25, ((1, 2), (7, 8))),), ())
        canvas.show()
        self.app.processEvents()
        self.assertFalse(canvas.grab().isNull())
        canvas.close()


if __name__ == "__main__":
    unittest.main()
