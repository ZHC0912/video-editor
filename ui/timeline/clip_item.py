"""One clip on the timeline.

The item holds the clip's ID and a few primitive values copied at build time.
It never holds a Clip object: the model is rebuilt on every edit and a cached
pydantic instance would go stale silently. Anything authoritative is looked up
through the scene by ID.

Painting reads the thumbnail and waveform caches but never asks them for work.
Requests are made from :meth:`ClipItem.refresh_media`, which the scene calls
after layout. A paint that triggered a decode would decode forever.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsItem

from ui import theme

__all__ = ["ClipItem", "CORNER_RADIUS"]

CORNER_RADIUS = 3
_LABEL_MARGIN = 4
_MEDIA_TOP_INSET = 16
_THUMBNAIL_MIN_WIDTH = 24


class ClipItem(QGraphicsRectItem):
    """A rounded rectangle carrying a filmstrip or a waveform."""

    def __init__(
        self,
        clip_id: str,
        track_id: str,
        kind: str,
        label: str,
        src: Path,
        src_in: int,
        src_out: int,
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(parent)
        # Identity, and primitives needed to paint. No model object.
        self.clip_id = clip_id
        self.track_id = track_id
        self.kind = kind
        self.label = label
        self.src = Path(src)
        self.src_in = src_in
        self.src_out = src_out

        self._fill = QColor(
            theme.CLIP_VIDEO if kind == "video" else theme.CLIP_AUDIO
        )
        self._muted = False
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setToolTip(f"{label}\n{src}")

    # -- state ------------------------------------------------------------

    def set_muted(self, muted: bool) -> None:
        if muted != self._muted:
            self._muted = muted
            self.update()

    def source_tick_at(self, fraction: float) -> int:
        """The source position a fraction of the way across the clip."""
        span = self.src_out - self.src_in
        return self.src_in + int(span * max(0.0, min(1.0, fraction)))

    # -- media requests ---------------------------------------------------

    def refresh_media(self) -> None:
        """Ask the caches for what this clip needs at its current width.

        Called by the scene after every layout. Cheap when everything is
        already cached: the caches deduplicate both hits and in-flight jobs.
        """
        scene = self.scene()
        if scene is None:
            return
        width = self.rect().width()
        if width < 2:
            return

        if self.kind == "video":
            thumbnails = getattr(scene, "thumbnails", None)
            if thumbnails is None:
                return
            for fraction in self._thumbnail_fractions(width):
                thumbnails.request(self.src, self.source_tick_at(fraction))
        else:
            waveforms = getattr(scene, "waveforms", None)
            if waveforms is not None:
                waveforms.request(self.src)

    def _thumbnail_fractions(self, width: float) -> list[float]:
        """Evenly spaced positions across the clip, one per filmstrip cell."""
        slots = max(1, int(width // self._thumbnail_width()))
        slots = min(slots, 40)  # a very long clip does not need hundreds
        return [(i + 0.5) / slots for i in range(slots)]

    def _thumbnail_width(self) -> float:
        # 16:9 at the cache's fixed height, floored so a tall thin thumbnail
        # never makes the slot count explode.
        return max(_THUMBNAIL_MIN_WIDTH, self._media_height() * 16 / 9)

    def _media_height(self) -> float:
        return max(0.0, self.rect().height() - _MEDIA_TOP_INSET - 2)

    # -- painting ---------------------------------------------------------

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.rect()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        fill = QColor(self._fill)
        if self._muted:
            fill.setAlpha(110)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(rect, CORNER_RADIUS, CORNER_RADIUS)

        painter.save()
        path_rect = QRectF(rect)
        painter.setClipRect(path_rect)
        if self.kind == "video":
            self._paint_filmstrip(painter, rect)
        else:
            self._paint_waveform(painter, rect)
        painter.restore()

        border = QColor(theme.ACCENT) if self.isSelected() else QColor(0, 0, 0, 90)
        painter.setPen(QPen(border, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), CORNER_RADIUS, CORNER_RADIUS)

        self._paint_label(painter, rect)

    def _paint_label(self, painter: QPainter, rect: QRectF) -> None:
        available = rect.width() - 2 * _LABEL_MARGIN
        if available < 12:
            return
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = metrics.elidedText(self.label, Qt.TextElideMode.ElideMiddle, int(available))

        text_rect = QRectF(
            rect.left() + _LABEL_MARGIN,
            rect.top() + 1,
            available,
            _MEDIA_TOP_INSET - 2,
        )
        # A dark plate so the name stays readable over a bright thumbnail.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.drawRect(text_rect.adjusted(-2, 0, 2, 0))
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )

    def _paint_filmstrip(self, painter: QPainter, rect: QRectF) -> None:
        scene = self.scene()
        thumbnails = getattr(scene, "thumbnails", None)
        height = self._media_height()
        if thumbnails is None or height < 8:
            return

        top = rect.top() + _MEDIA_TOP_INSET
        fractions = self._thumbnail_fractions(rect.width())
        cell = rect.width() / len(fractions)

        for index, fraction in enumerate(fractions):
            pixmap = thumbnails.peek(self.src, self.source_tick_at(fraction))
            cell_rect = QRectF(rect.left() + index * cell, top, cell, height)
            if pixmap is None or pixmap.isNull():
                painter.fillRect(cell_rect, QColor(0, 0, 0, 40))
                continue
            scaled = pixmap.scaledToHeight(
                int(height), Qt.TransformationMode.SmoothTransformation
            )
            # Centre-crop each cell so the strip has no gaps or stretching.
            source_x = max(0.0, (scaled.width() - cell) / 2)
            painter.drawPixmap(
                cell_rect,
                scaled,
                QRectF(source_x, 0, min(cell, scaled.width()), scaled.height()),
            )

    def _paint_waveform(self, painter: QPainter, rect: QRectF) -> None:
        scene = self.scene()
        waveforms = getattr(scene, "waveforms", None)
        if waveforms is None:
            return
        columns = max(1, int(rect.width()))
        sliced = waveforms.slice_for(self.src, self.src_in, self.src_out, columns)
        if sliced is None:
            return

        lows, highs = sliced
        top = rect.top() + _MEDIA_TOP_INSET
        height = self._media_height()
        if height < 4:
            return
        centre = top + height / 2
        half = height / 2

        painter.setPen(QPen(QColor(255, 255, 255, 150), 1))
        for column in range(columns):
            x = rect.left() + column + 0.5
            # Mirrored about the vertical centre.
            y_high = centre - float(highs[column]) * half
            y_low = centre - float(lows[column]) * half
            if abs(y_low - y_high) < 1:
                y_high = centre - 0.5
                y_low = centre + 0.5
            painter.drawLine(int(x), int(y_high), int(x), int(y_low))
