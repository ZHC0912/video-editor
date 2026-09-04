"""Window placement.

Pure QRect arithmetic, so no QApplication is needed. The cases that matter are
the ones that leave a window unreachable: taller than the screen, so the title
bar sits above the top edge, and restored onto a monitor that is gone.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRect

from ui.window_geometry import (
    MAX_SCREEN_FRACTION,
    MINIMUM_SIZE,
    PREFERRED_SIZE,
    TITLE_BAR_ALLOWANCE,
    default_geometry,
    is_reachable,
    minimum_size,
    restored_geometry,
)

# Real layouts, as availableGeometry reports them: the taskbar is already gone.
DESKTOP_1440P = QRect(0, 0, 2560, 1400)
THINKPAD_1600x900 = QRect(0, 0, 1600, 860)
LAPTOP_1366x768 = QRect(0, 0, 1366, 728)
SMALL = QRect(0, 0, 1024, 600)
TINY = QRect(0, 0, 800, 560)
SECOND_MONITOR = QRect(2560, 0, 1920, 1040)

ALL_SCREENS = [
    DESKTOP_1440P,
    THINKPAD_1600x900,
    LAPTOP_1366x768,
    SMALL,
    TINY,
    QRect(0, 0, 1920, 1040),
]


class TestDefaultGeometry:
    @pytest.mark.parametrize("available", ALL_SCREENS, ids=lambda r: f"{r.width()}x{r.height()}")
    def test_never_larger_than_the_screen(self, available: QRect) -> None:
        # The reported defect: a window taller than the available area puts its
        # title bar above the top edge and it cannot be moved or closed.
        got = default_geometry(available)
        assert available.contains(got), f"{got} escapes {available}"

    @pytest.mark.parametrize("available", ALL_SCREENS, ids=lambda r: f"{r.width()}x{r.height()}")
    def test_title_bar_is_always_below_the_top_edge(self, available: QRect) -> None:
        got = default_geometry(available)
        assert got.top() >= available.top() + TITLE_BAR_ALLOWANCE

    @pytest.mark.parametrize("available", ALL_SCREENS, ids=lambda r: f"{r.width()}x{r.height()}")
    def test_centred_horizontally(self, available: QRect) -> None:
        got = default_geometry(available)
        left = got.left() - available.left()
        right = available.right() - got.right()
        assert abs(left - right) <= 1

    def test_uses_the_preferred_size_when_there_is_room(self) -> None:
        got = default_geometry(DESKTOP_1440P)
        assert got.size() == PREFERRED_SIZE
        assert got.center().x() == pytest.approx(DESKTOP_1440P.center().x(), abs=1)

    def test_thinkpad_gets_a_shorter_window(self) -> None:
        # 1600x900 with a taskbar. The preferred 860 tall does not fit once the
        # title bar allowance is taken off, so the height comes down.
        got = default_geometry(THINKPAD_1600x900)
        assert got.width() == 1400
        assert got.height() < PREFERRED_SIZE.height()
        assert THINKPAD_1600x900.contains(got)

    @pytest.mark.parametrize(
        "available", [DESKTOP_1440P, THINKPAD_1600x900, LAPTOP_1366x768]
    )
    def test_claims_at_most_ninety_percent(self, available: QRect) -> None:
        got = default_geometry(available)
        assert got.width() <= int(available.width() * MAX_SCREEN_FRACTION)
        assert got.height() <= int(available.height() * MAX_SCREEN_FRACTION)

    def test_the_minimum_size_wins_over_the_ninety_percent_rule(self) -> None:
        # On a screen where 90% is under the minimum, the minimum is used: a
        # cramped window is better than one that cannot show its controls.
        available = QRect(0, 0, 1100, 900)
        got = default_geometry(available)
        assert got.width() == MINIMUM_SIZE.width()
        assert available.contains(got)

    def test_the_screen_wins_over_the_minimum_size(self) -> None:
        # Below the minimum there is nothing sensible to do but fit. A window
        # wider than the display cannot be dragged back into view.
        got = default_geometry(TINY)
        assert got.width() <= TINY.width()
        assert got.height() <= TINY.height()
        assert TINY.contains(got)

    def test_offset_screen_origin_is_respected(self) -> None:
        # A second monitor to the right of the primary has a non-zero origin.
        got = default_geometry(SECOND_MONITOR)
        assert SECOND_MONITOR.contains(got)
        assert got.left() >= SECOND_MONITOR.left()


class TestMinimumSize:
    def test_is_the_constant_when_it_fits(self) -> None:
        assert minimum_size(DESKTOP_1440P) == MINIMUM_SIZE

    def test_never_exceeds_the_screen(self) -> None:
        # A minimum larger than the display would stop the window shrinking to
        # fit, which is the same trap by another route.
        got = minimum_size(TINY)
        assert got.width() <= TINY.width()
        assert got.height() <= TINY.height()


class TestIsReachable:
    SCREENS = [QRect(0, 0, 1920, 1040), SECOND_MONITOR]

    def test_fully_on_the_primary(self) -> None:
        assert is_reachable(QRect(100, 100, 1400, 860), self.SCREENS)

    def test_fully_on_a_secondary(self) -> None:
        assert is_reachable(QRect(2700, 100, 1400, 860), self.SCREENS)

    def test_partly_off_the_right_edge_is_still_reachable(self) -> None:
        # Half off screen is untidy but the title bar can still be grabbed.
        assert is_reachable(QRect(1500, 200, 1400, 860), self.SCREENS)

    def test_a_title_bar_above_the_top_edge_is_not(self) -> None:
        # Exactly the reported defect, restored from settings.
        assert not is_reachable(QRect(100, -400, 1400, 860), self.SCREENS)

    def test_a_disconnected_monitor_is_not(self) -> None:
        # Saved on a third screen at x=4480 that is no longer plugged in.
        assert not is_reachable(QRect(4600, 100, 1400, 860), self.SCREENS)

    def test_a_sliver_on_screen_is_not_enough(self) -> None:
        # Twenty pixels of window hanging off the right of a lone monitor.
        # (With the second monitor attached this same rect is fine, because it
        # spans onto it, which is why the screen list is the whole input.)
        only_primary = [QRect(0, 0, 1920, 1040)]
        assert not is_reachable(QRect(1900, 100, 1400, 860), only_primary)
        assert is_reachable(QRect(1900, 100, 1400, 860), self.SCREENS)

    def test_empty_geometry_is_not(self) -> None:
        assert not is_reachable(QRect(), self.SCREENS)

    def test_no_screens_at_all(self) -> None:
        assert not is_reachable(QRect(0, 0, 1400, 860), [])


class TestRestoredGeometry:
    SCREENS = [QRect(0, 0, 1920, 1040)]

    def test_a_good_saved_geometry_is_kept_exactly(self) -> None:
        saved = QRect(120, 80, 1280, 800)
        assert restored_geometry(saved, self.SCREENS, self.SCREENS[0]) == saved

    def test_nothing_saved_yields_the_default(self) -> None:
        got = restored_geometry(None, self.SCREENS, self.SCREENS[0])
        assert got == default_geometry(self.SCREENS[0])

    def test_a_geometry_on_a_vanished_monitor_is_discarded(self) -> None:
        saved = QRect(3000, 200, 1400, 860)
        got = restored_geometry(saved, self.SCREENS, self.SCREENS[0])
        assert got == default_geometry(self.SCREENS[0])
        assert self.SCREENS[0].contains(got)

    def test_a_geometry_above_the_top_edge_is_discarded(self) -> None:
        saved = QRect(100, -500, 1400, 860)
        got = restored_geometry(saved, self.SCREENS, self.SCREENS[0])
        assert got == default_geometry(self.SCREENS[0])
