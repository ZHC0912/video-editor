"""Poster frames for the media bin.

Every row in the bin gets an image the same size, so the text columns line up:
a decoded frame where there is one, a neutral plate while it is being made, and
an audio glyph for files with no picture at all. A broken-image icon is never
correct here; an audio file has no poster frame, it is not a failure.

The frame itself comes from the shared thumbnail cache, the same one the
timeline's filmstrips use.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF

from core.model import MediaInfo
from ui import theme

__all__ = [
    "POSTER_SIZE",
    "POSTER_FRACTION",
    "poster_tick",
    "placeholder_pixmap",
    "audio_glyph_pixmap",
    "fit_poster",
]

#: The box every poster is drawn into. 16:9 at the thumbnail cache's 64px.
POSTER_SIZE = QSize(114, 64)

#: How far into a source the poster frame is taken. Not zero: a great many
#: files open on black, a slate, or a fade up, and frame 0 would show a row of
#: identical black rectangles.
POSTER_FRACTION = 0.10


def poster_tick(info: MediaInfo) -> int:
    """The source position to take a poster frame from."""
    return max(0, int(info.duration_ticks * POSTER_FRACTION))


def _plate(size: QSize) -> QPixmap:
    pixmap = QPixmap(size)
    pixmap.fill(QColor(0, 0, 0, 0))
    return pixmap


def placeholder_pixmap(size: QSize = POSTER_SIZE) -> QPixmap:
    """A neutral plate shown until the real frame arrives."""
    pixmap = _plate(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = QRectF(0.5, 0.5, size.width() - 1, size.height() - 1)
    painter.setPen(QPen(QColor(theme.BORDER), 1))
    painter.setBrush(QColor(theme.ELEVATED))
    painter.drawRoundedRect(rect, 3, 3)
    painter.end()
    return pixmap


def audio_glyph_pixmap(size: QSize = POSTER_SIZE) -> QPixmap:
    """A small symmetrical waveform, for sources with no picture."""
    pixmap = _plate(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    rect = QRectF(0.5, 0.5, size.width() - 1, size.height() - 1)
    painter.setPen(QPen(QColor(theme.BORDER), 1))
    painter.setBrush(QColor(theme.CLIP_AUDIO))
    painter.drawRoundedRect(rect, 3, 3)

    centre = size.height() / 2
    painter.setPen(QPen(QColor(255, 255, 255, 170), 1))
    bars = 13
    step = size.width() / (bars + 1)
    # A fixed, obviously synthetic shape. It is a glyph, not a reading of the
    # file, and it should not be mistaken for one.
    heights = (0.18, 0.42, 0.72, 0.94, 0.62, 0.30, 0.85, 0.30, 0.62, 0.94, 0.72, 0.42, 0.18)
    for index in range(bars):
        x = step * (index + 1)
        half = heights[index] * (size.height() / 2 - 6)
        painter.drawLine(QPointF(x, centre - half), QPointF(x, centre + half))
    painter.end()
    return pixmap


def fit_poster(frame: QPixmap, size: QSize = POSTER_SIZE) -> QPixmap:
    """Centre a decoded frame in the poster box without distorting it.

    Portrait footage keeps its shape and is simply narrower than the box, which
    is why the box is a fixed size and the frame is drawn inside it rather than
    stretched to fill.
    """
    pixmap = _plate(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    scaled = frame.scaled(
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = (size.width() - scaled.width()) / 2
    y = (size.height() - scaled.height()) / 2
    painter.drawPixmap(QPointF(x, y), scaled)
    painter.end()
    return pixmap
