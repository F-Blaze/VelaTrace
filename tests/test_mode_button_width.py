"""Offscreen regression for the Audit/Route segmented switch widths."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication

from velatrace.ui import glass_stylesheet, theme_tokens
from velatrace.ui_style import SegmentedSwitch, install


def test_mode_buttons_fit_labels_and_keep_width_in_both_themes():
    app = QApplication.instance() or QApplication([])
    install().set_tokens(theme_tokens(True))
    switch = SegmentedSwitch()
    buttons = [switch.add(text) for text in ("Audit", "Route")]
    try:
        switch.resize(240, 40)
        switch.show()
        for dark in (False, True):
            switch.setStyleSheet(glass_stylesheet(theme_tokens(dark)))
            app.processEvents()
            widths = [button.width() for button in buttons]
            for active in (0, 1, 0):
                for index, button in enumerate(buttons):
                    button.setChecked(index == active)
                app.processEvents()
                assert [button.width() for button in buttons] == widths
                for button in buttons:
                    assert button.width() >= max(QFontMetrics(button.font()).horizontalAdvance(button.text()) + 24, 64)
    finally:
        switch.close()
        switch.deleteLater()
        app.processEvents()
