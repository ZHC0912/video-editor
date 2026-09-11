"""The time ruler across the top of the timeline.

The marks are computed once into :attr:`RulerItem._marks` by :meth:`RulerItem.relayout`
and painted from that list. Nothing is computed inside ``paint``.

That split is deliberate. The ruler's content depends on ``pixels_per_second``,
which changes on zoom without the item's bounding rect changing at all, and an
item whose geometry has not changed is not repainted. The result was a ruler
carrying marks from two zoom levels at once. Both the zoom path and the rebuild
path now call :meth:`relayout`, which clears the old marks and asks for a
repaint every time, so the two cannot drift apart. The scene is rebuilt after
every command, so they must not.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

from core.timebase import TICKS_PER_SECOND
from ui import theme

__all__ = [
    "RulerItem",
    "RulerMark",
    "RULER_INTERVALS",
    "MIN_LABEL_SPACING",
    "choose_interval",
    "label_for_second",
    "uses_long_labels",
    "expected_label_count",
    "LONG_LABEL_THRESHOLD_SECONDS",
]

#: The only intervals the ruler will ever use, in seconds.
#:
#: The list has to reach far enough that the largest entry still clears
#: MIN_LABEL_SPACING at the lowest zoom, or there would be a band of zoom
#: levels with no usable interval at all. At the 0.05 px/s floor a 30 minute
#: interval is 90px, so 1800 is the largest one actually selected; 3600 is
#: headroom for a future lower floor and is unreachable inside the current
#: clamp.
RULER_INTERVALS = (1, 5, 10, 30, 60, 300, 600, 1800, 3600)

#: Minimum gap between labels, in pixels. An HH:MM:SS label is about 50px wide
#: at 8pt, so this leaves better than a label's width of clear space between
#: them and they can never touch.
MIN_LABEL_SPACING = 80

#: A ruler spanning longer than this shows HH:MM:SS rather than MM:SS.
LONG_LABEL_THRESHOLD_SECONDS = 3600

_MAJOR_TICK = 10
_MINOR_TICK = 5


@dataclass(frozen=True)
class RulerMark:
    """One tick on the ruler. Labelled marks carry text, minor ones do not."""

    x: float
    seconds: float
    label: str

    @property
    def is_labelled(self) -> bool:
        return bool(self.label)


def choose_interval(pixels_per_second: float) -> int:
    """The smallest interval whose labels are still far enough apart."""
    for interval in RULER_INTERVALS:
        if interval * pixels_per_second >= MIN_LABEL_SPACING:
            return interval
    return RULER_INTERVALS[-1]


def uses_long_labels(width: float, pixels_per_second: float) -> bool:
    """Whether a ruler of this size needs HH:MM:SS.

    Decided from the span the whole ruler covers, not from each label on its
    own, so every label on a given ruler is in the same format and the format
    does not change as the view is scrolled.
    """
    if pixels_per_second <= 0:
        return False
    return width / pixels_per_second > LONG_LABEL_THRESHOLD_SECONDS


def label_for_second(second: float, long_form: bool = False) -> str:
    """MM:SS, or HH:MM:SS on a ruler spanning more than an hour.

    Past an hour MM:SS would keep counting minutes (90:00 for an hour and a
    half), which is not a timecode anyone reads correctly.
    """
    total = int(second)
    if long_form:
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    minutes, seconds = divmod(total, 60)
    return f"{minutes:02d}:{seconds:02d}"


def expected_label_count(width: float, pixels_per_second: float) -> int:
    """How many labelled marks fit in ``width``. Used by the ruler and by tests."""
    if width < 0 or pixels_per_second <= 0:
        return 0
    spacing = choose_interval(pixels_per_second) * pixels_per_second
    return int(width // spacing) + 1


def compute_marks(width: float, pixels_per_second: float) -> list[RulerMark]:
    """Every mark on a ruler of this width at this zoom.

    A pure function, so the layout can be checked without a scene and the two
    call sites cannot produce different answers.
    """
    if width <= 0 or pixels_per_second <= 0:
        return []

    interval = choose_interval(pixels_per_second)
    long_form = uses_long_labels(width, pixels_per_second)
    marks: list[RulerMark] = []
    index = 0
    while True:
        second = index * interval
        x = second * pixels_per_second
        if x > width:
            break
        marks.append(
            RulerMark(x=x, seconds=second, label=label_for_second(second, long_form))
        )

        if interval > 1:
            midpoint = second + interval / 2
            mid_x = midpoint * pixels_per_second
            if mid_x <= width:
                marks.append(RulerMark(x=mid_x, seconds=midpoint, label=""))
        index += 1
    return marks


class RulerItem(QGraphicsItem):
    """Draws ticks and MM:SS labels along the top of the scene."""

    def __init__(self, height: float, parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self._height = height
        self._width = 0.0
        self._pixels_per_second = 0.0
        self._marks: list[RulerMark] = []
        self.setZValue(50)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    # -- the one layout function ------------------------------------------

    def relayout(self, width: float, pixels_per_second: float) -> None:
        """Recompute every mark. The only way the ruler ever changes.

        Called from TimelineScene._apply_geometry, which is on both the zoom
        path and the rebuild path. It always clears and always repaints, even
        when the width is unchanged: on an empty project the width never
        changes at all, and that is exactly the case where stale marks from the
        previous zoom level used to survive.
        """
        width = max(0.0, float(width))
        if width != self._width:
            self.prepareGeometryChange()
            self._width = width
        self._pixels_per_second = float(pixels_per_second)

        self._marks.clear()
        self._marks.extend(compute_marks(self._width, self._pixels_per_second))
        self.update()

    # -- inspection --------------------------------------------------------

    def marks(self) -> list[RulerMark]:
        return list(self._marks)

    def labels(self) -> list[RulerMark]:
        return [mark for mark in self._marks if mark.is_labelled]

    def interval_seconds(self) -> int:
        return choose_interval(self._pixels_per_second)

    def tick_interval_ticks(self) -> int:
        """The current interval expressed in timebase ticks."""
        return self.interval_seconds() * TICKS_PER_SECOND

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._width, self._height)

    # -- painting ----------------------------------------------------------

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.fillRect(self.boundingRect(), QColor(theme.ELEVATED))
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        baseline = int(self._height) - 1
        painter.drawLine(0, baseline, int(self._width), baseline)

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)

        exposed = option.exposedRect if option is not None else self.boundingRect()
        # A label is drawn to the right of its tick, so allow for its width
        # when deciding what the exposed rect covers.
        left = exposed.left() - 60
        right = exposed.right() + 60

        for mark in self._marks:
            if mark.x < left or mark.x > right:
                continue
            x = int(mark.x)
            if mark.is_labelled:
                painter.setPen(QPen(QColor(theme.MUTED), 1))
                painter.drawLine(x, baseline - _MAJOR_TICK, x, baseline)
                painter.setPen(QColor(theme.TEXT))
                painter.drawText(x + 3, baseline - _MAJOR_TICK - 3, mark.label)
            else:
                painter.setPen(QPen(QColor(theme.BORDER), 1))
                painter.drawLine(x, baseline - _MINOR_TICK, x, baseline)
