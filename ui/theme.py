"""Dark theme.

One stylesheet, applied to the QApplication. Colours live here as constants so
that painting code (the timeline items in Phase 3) reads the same values the
stylesheet uses, rather than repeating hex strings.

The stylesheet is a string.Template, not an f-string: Qt style sheets are full
of literal braces and doubling every one of them would make this unreadable.
Placeholders are written $like_this.
"""

from __future__ import annotations

from string import Template

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

# Surfaces, darkest to lightest. The steps are deliberately visible so the
# timeline reads as its own surface sitting on the window.
BACKGROUND = "#1a1a1e"
PANEL = "#232329"
ELEVATED = "#2a2a32"
BORDER = "#34343c"

TEXT = "#e4e4e8"
MUTED = "#8a8a95"

ACCENT = "#4f8fff"
WARNING = "#d98c3a"
ERROR = "#d95a5a"

# Clip fills, used from Phase 3 onward.
CLIP_VIDEO = "#2d4a6b"
CLIP_AUDIO = "#2d5a4a"

UI_FAMILY = "Segoe UI"
UI_POINT_SIZE = 9

# Cascadia Mono ships with Windows Terminal but is not guaranteed. Qt walks
# this list in order, so a machine without it still gets a monospaced face.
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Courier New", "monospace"]
MONO_POINT_SIZE = 9

# Nothing in this UI is rounder than this.
RADIUS = 4


def ui_font(point_size: int = UI_POINT_SIZE, bold: bool = False) -> QFont:
    font = QFont(UI_FAMILY, point_size)
    font.setBold(bold)
    return font


def mono_font(point_size: int = MONO_POINT_SIZE, bold: bool = False) -> QFont:
    """The face for every timecode field in the application."""
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setPointSize(point_size)
    font.setBold(bold)
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


_STYLESHEET = Template(
    """
QWidget {
    background: $background;
    color: $text;
    font-family: "$ui_family";
    font-size: ${ui_size}pt;
}

QMainWindow, QDialog {
    background: $background;
}

QLabel {
    background: transparent;
}

QLabel[muted="true"] {
    color: $muted;
}

QLabel[timecode="true"] {
    color: $text;
    padding: 0px 4px;
}

/* ---- menus ---- */

QMenuBar {
    background: $panel;
    border-bottom: 1px solid $border;
    padding: 2px;
}

QMenuBar::item {
    background: transparent;
    padding: 4px 10px;
    border-radius: ${radius}px;
}

QMenuBar::item:selected {
    background: $elevated;
}

QMenu {
    background: $elevated;
    border: 1px solid $border;
    border-radius: ${radius}px;
    padding: 4px;
}

QMenu::item {
    padding: 5px 26px 5px 22px;
    border-radius: ${radius}px;
}

QMenu::item:selected {
    background: $accent;
    color: #ffffff;
}

QMenu::item:disabled {
    color: $muted;
}

QMenu::separator {
    height: 1px;
    background: $border;
    margin: 4px 8px;
}

/* ---- panels and framing ---- */

QFrame[surface="panel"] {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius}px;
}

QFrame[surface="timeline"] {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius}px;
}

QFrame[surface="stage"] {
    background: #000000;
    border: 1px solid $border;
    border-radius: ${radius}px;
}

QSplitter::handle {
    background: $background;
}

QSplitter::handle:horizontal {
    width: 4px;
}

QSplitter::handle:vertical {
    height: 4px;
}

QSplitter::handle:hover {
    background: $border;
}

/* ---- media bin ---- */

QListWidget {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius}px;
    padding: 2px;
    outline: none;
}

QListWidget::item {
    padding: 5px 6px;
    border-radius: ${radius}px;
    color: $text;
}

QListWidget::item:hover {
    background: $elevated;
}

QListWidget::item:selected {
    background: $accent;
    color: #ffffff;
}

/* ---- buttons ---- */

QPushButton, QToolButton {
    background: $elevated;
    color: $text;
    border: 1px solid $border;
    border-radius: ${radius}px;
    padding: 5px 12px;
}

QToolButton {
    padding: 4px;
}

QPushButton:hover, QToolButton:hover {
    background: $border;
}

QPushButton:pressed, QToolButton:pressed {
    background: $accent;
    color: #ffffff;
}

QPushButton:disabled, QToolButton:disabled {
    color: $muted;
    background: $panel;
}

QPushButton:default {
    border-color: $accent;
}

/* ---- sliders ---- */

QSlider::groove:horizontal {
    height: 4px;
    background: $elevated;
    border: 1px solid $border;
    border-radius: 2px;
}

QSlider::sub-page:horizontal {
    background: $accent;
    border: 1px solid $accent;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    background: $text;
    border: none;
    width: 10px;
    height: 10px;
    margin: -4px 0;
    border-radius: 4px;
}

QSlider::handle:horizontal:hover {
    background: #ffffff;
}

QSlider::handle:horizontal:disabled {
    background: $muted;
}

/* ---- progress ---- */

QProgressBar {
    background: $elevated;
    border: 1px solid $border;
    border-radius: ${radius}px;
    text-align: center;
    color: $text;
}

QProgressBar::chunk {
    background: $accent;
    border-radius: 3px;
}

/* ---- status bar ---- */

QStatusBar {
    background: $panel;
    border-top: 1px solid $border;
    color: $muted;
}

QStatusBar::item {
    border: none;
}

/* ---- scrollbars ---- */

QScrollBar:vertical {
    background: $background;
    width: 12px;
    margin: 0;
}

QScrollBar:horizontal {
    background: $background;
    height: 12px;
    margin: 0;
}

QScrollBar::handle {
    background: $elevated;
    border: 1px solid $border;
    border-radius: ${radius}px;
    min-height: 24px;
    min-width: 24px;
}

QScrollBar::handle:hover {
    background: $border;
}

QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
    width: 0;
}

QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}

/* ---- dialogs ---- */

QMessageBox {
    background: $panel;
}

QMessageBox QLabel {
    color: $text;
}

QTextEdit, QPlainTextEdit {
    background: $background;
    border: 1px solid $border;
    border-radius: ${radius}px;
    color: $text;
}

QLineEdit {
    background: $background;
    border: 1px solid $border;
    border-radius: ${radius}px;
    padding: 4px 6px;
    selection-background-color: $accent;
}

QToolTip {
    background: $elevated;
    color: $text;
    border: 1px solid $border;
    padding: 4px;
}
"""
)


def stylesheet() -> str:
    return _STYLESHEET.substitute(
        background=BACKGROUND,
        panel=PANEL,
        elevated=ELEVATED,
        border=BORDER,
        text=TEXT,
        muted=MUTED,
        accent=ACCENT,
        warning=WARNING,
        error=ERROR,
        ui_family=UI_FAMILY,
        ui_size=UI_POINT_SIZE,
        radius=RADIUS,
    )


def apply_theme(app: QApplication) -> None:
    """Set the application font and stylesheet. Call once, at startup."""
    app.setFont(ui_font())
    app.setStyleSheet(stylesheet())
