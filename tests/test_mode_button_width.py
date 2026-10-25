"""Offscreen regression for the Audit/Route segmented control widths."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QWidget

from velatrace.ui import glass_stylesheet, theme_tokens


def test_mode_buttons_fit_bold_labels_in_both_themes():
    app = QApplication.instance() or QApplication([])
    container = QWidget()
    layout = QHBoxLayout(container)
    buttons = []
    for text in ("Audit", "Route"):
        button = QPushButton(text)
        button.setObjectName("seg")
        button.setCheckable(True)
        layout.addWidget(button)
        buttons.append(button)
    layout.addStretch()

    try:
        container.resize(420, 80)
        container.show()

        for dark in (False, True):
            container.setStyleSheet(glass_stylesheet(theme_tokens(dark)))
            app.processEvents()
            widths = [button.width() for button in buttons]

            for active in (0, 1, 0):
                for index, button in enumerate(buttons):
                    button.setChecked(index == active)
                app.processEvents()

                assert [button.width() for button in buttons] == widths
                for button in buttons:
                    bold_font = QFont(button.font())
                    bold_font.setWeight(QFont.Weight.DemiBold)
                    text_width = QFontMetrics(bold_font).horizontalAdvance(button.text())
                    # Stylesheet uses horizontal padding of 14px on each side.
                    assert button.width() >= text_width + 28
    finally:
        container.close()
        container.deleteLater()
        app.processEvents()
