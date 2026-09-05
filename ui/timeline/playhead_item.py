"""The playhead line."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QGraphicsItem

from ui import theme

__all__ = ["PlayheadItem", "PLAYHEAD_WIDTH"]

PLAYHEAD_WIDTH = 2
_HANDLE_HALF_WIDTH = 5
_HANDLE_HEIGHT = 8


class PlayheadItem(QGraphicsItem):
    """A vertical accent line spanning every lane, drawn above the clips.

    Positioned with setX, so moving it costs nothing but a repaint of the two
    columns it used to and now does occupy.
    """

    def __init__(self, parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self._height = 0.0
        # Above clips (0) and the ruler (50).
        self.setZValue(100)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_height(self, height: float) -> None:
        if height != self._height:
            self.prepareGeometryChange()
            self._height = height

    def boundingRect(self) -> QRectF:
        return QRectF(
            -_HANDLE_HALF_WIDTH,
            0,
            _HANDLE_HALF_WIDTH * 2,
            self._height,
        )

    def paint(self, painter: QPainter, option, widget=None) -> None:
        colour = QColor(theme.ACCENT)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.setPen(QPen(colour, PLAYHEAD_WIDTH))
        painter.drawLine(0, 0, 0, int(self._height))

        # A small grabbable head, which Phase 4 turns into a scrub handle.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(-_HANDLE_HALF_WIDTH, 0),
                    QPointF(_HANDLE_HALF_WIDTH, 0),
                    QPointF(0, _HANDLE_HEIGHT),
                ]
            )
        )
