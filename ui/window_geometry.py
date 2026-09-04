"""Where the main window opens, and whether a saved position is still usable.

Pure geometry arithmetic, kept out of main_window.py so it can be tested
against invented screen layouts rather than whatever monitor happens to be
attached.

Two things go wrong with window geometry and both leave the window
unreachable: opening taller than the screen, which pushes the title bar above
the top edge, and restoring a position saved on a monitor that is no longer
plugged in. Neither can be recovered with the mouse, so both are handled here.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QRect, QSize

__all__ = [
    "PREFERRED_SIZE",
    "MINIMUM_SIZE",
    "MAX_SCREEN_FRACTION",
    "TITLE_BAR_ALLOWANCE",
    "default_geometry",
    "minimum_size",
    "is_reachable",
    "restored_geometry",
]

#: What the window opens at when there is room for it.
PREFERRED_SIZE = QSize(1400, 860)

#: Small enough for a 1600x900 laptop with a taskbar.
MINIMUM_SIZE = QSize(1024, 640)

#: Never claim the whole screen on first run.
MAX_SCREEN_FRACTION = 0.9

#: Vertical room reserved above the window so the title bar is always on
#: screen and therefore always draggable. Qt positions the client area; the
#: frame sits above it.
TITLE_BAR_ALLOWANCE = 32

#: A restored window must show at least this much of itself to count as
#: reachable: enough of the title bar to grab with a mouse.
_MIN_VISIBLE_WIDTH = 160
_MIN_VISIBLE_HEIGHT = TITLE_BAR_ALLOWANCE + 8


def _clamp(preferred: int, available: int, minimum: int) -> int:
    """Fit ``preferred`` into ``available``, honouring the minimum where it fits.

    The 90% rule yields to the minimum size, and the minimum size yields to the
    screen. On a display too small for the minimum, filling it is the least bad
    answer: a window bigger than the screen cannot be moved back.
    """
    size = min(preferred, int(available * MAX_SCREEN_FRACTION))
    return max(size, min(minimum, available))


def minimum_size(available: QRect) -> QSize:
    """The minimum size to enforce, never larger than the screen itself."""
    return QSize(
        min(MINIMUM_SIZE.width(), available.width()),
        min(MINIMUM_SIZE.height(), available.height()),
    )


def default_geometry(available: QRect) -> QRect:
    """A centred window that fits inside ``available``.

    ``available`` is expected to be a screen's availableGeometry, which already
    excludes the taskbar.
    """
    usable_height = max(1, available.height() - TITLE_BAR_ALLOWANCE)

    width = _clamp(PREFERRED_SIZE.width(), available.width(), MINIMUM_SIZE.width())
    height = _clamp(PREFERRED_SIZE.height(), usable_height, MINIMUM_SIZE.height())

    x = available.x() + (available.width() - width) // 2
    y = available.y() + TITLE_BAR_ALLOWANCE + (usable_height - height) // 2
    return QRect(x, y, width, height)


def is_reachable(geometry: QRect, screens: Sequence[QRect]) -> bool:
    """True when enough of ``geometry`` lands on a screen to grab with a mouse.

    Intersecting a screen by a single pixel is not enough. The top edge has to
    be at or below the screen's top edge, because that is where the title bar
    is, and a usable strip of it has to be visible.
    """
    if geometry.isEmpty():
        return False
    for screen in screens:
        if geometry.top() < screen.top():
            continue
        overlap = geometry.intersected(screen)
        if (
            overlap.width() >= _MIN_VISIBLE_WIDTH
            and overlap.height() >= _MIN_VISIBLE_HEIGHT
        ):
            return True
    return False


def restored_geometry(
    saved: QRect | None, screens: Sequence[QRect], available: QRect
) -> QRect:
    """Validate a geometry read back from settings.

    Phase 6 restores window placement from QSettings. A geometry saved on a
    monitor that has since been unplugged would put the window somewhere the
    user cannot reach, so it is discarded in favour of the centred default.
    """
    if saved is not None and is_reachable(saved, screens):
        return saved
    return default_geometry(available)
