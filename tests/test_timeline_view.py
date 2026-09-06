"""Timeline view and track header column.

The zoom anchor is the interesting part: the acceptance says the point under
the cursor stays still, and that is easy to get subtly wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.model import Clip, Project, Track  # noqa: E402
from core.timebase import TICKS_PER_SECOND as SEC  # noqa: E402
from ui.timeline.timeline_scene import (  # noqa: E402
    MAX_PIXELS_PER_SECOND,
    MIN_PIXELS_PER_SECOND,
)
from ui.timeline.timeline_view import (  # noqa: E402
    HEADER_WIDTH,
    ADD_VIDEO_LABEL,
    MULTIPLE_VIDEO_TRACK_TOOLTIP,
    VIDEO_LIMIT_LABEL,
    FitOutcome,
    TimelinePanel,
)

SRC = Path("C:/media/a.mp4")


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_project(video: int = 1, audio: int = 1, seconds: int = 30) -> Project:
    tracks: list[Track] = []
    for i in range(video):
        tracks.append(
            Track(
                id=f"v{i}",
                name=f"V{i + 1}",
                kind="video",
                clips=[
                    Clip(src=SRC, src_in=0, src_out=seconds * SEC, timeline_start=0)
                ],
            )
        )
    for i in range(audio):
        tracks.append(Track(id=f"a{i}", name=f"A{i + 1}", kind="audio"))
    return Project(name="p", tracks=tracks)


@pytest.fixture
def panel(qapp: QApplication) -> TimelinePanel:
    p = TimelinePanel()
    p.resize(900, 300)
    p.set_project(make_project())
    yield p
    p.deleteLater()


class TestZoomAnchor:
    @pytest.fixture
    def scrolled(self, qapp: QApplication) -> TimelinePanel:
        """A long project, scrolled into the middle.

        The anchor can only be held while the scrollbar has room to move in
        the direction the zoom needs. Parked at the very start there is
        nothing to the left to reveal, which is a property of the timeline
        beginning at zero rather than of the arithmetic; that boundary is
        pinned separately below.
        """
        panel = TimelinePanel()
        panel.resize(900, 300)
        panel.set_project(make_project(seconds=300))
        panel.view.resize(780, 260)
        panel.scene.set_pixels_per_second(40.0)
        panel.view.horizontalScrollBar().setValue(4000)
        return panel

    @pytest.mark.parametrize("x", [0, 50, 200, 640])
    @pytest.mark.parametrize("factor", [1.25, 1 / 1.25, 2.0, 0.5])
    def test_the_point_under_the_cursor_stays_put(
        self, scrolled: TimelinePanel, x: int, factor: float
    ) -> None:
        view = scrolled.view
        scene = scrolled.scene

        cursor = QPoint(x, 60)
        before_ticks = scene.x_to_ticks(view.mapToScene(cursor).x(), snap=False)
        view.zoom_at(cursor, factor)
        after_ticks = scene.x_to_ticks(view.mapToScene(cursor).x(), snap=False)

        # Within one pixel's worth of ticks: the scrollbar is an integer.
        tolerance = SEC / scene.pixels_per_second
        assert abs(after_ticks - before_ticks) <= tolerance

    def test_repeated_zooming_does_not_walk_sideways(
        self, scrolled: TimelinePanel
    ) -> None:
        # Zooming in and back out ten times must not drift. This is what the
        # unsnapped anchor is for: snapping it each notch would nudge the
        # timeline by up to half a frame every time.
        view = scrolled.view
        scene = scrolled.scene
        cursor = QPoint(300, 60)

        start = scene.x_to_ticks(view.mapToScene(cursor).x(), snap=False)
        for _ in range(10):
            view.zoom_at(cursor, 1.25)
        for _ in range(10):
            view.zoom_at(cursor, 1 / 1.25)
        end = scene.x_to_ticks(view.mapToScene(cursor).x(), snap=False)

        assert scene.pixels_per_second == pytest.approx(40.0, rel=1e-6)
        assert abs(end - start) <= SEC / scene.pixels_per_second * 3

    def test_at_the_very_start_zooming_out_cannot_hold_the_anchor(
        self, panel: TimelinePanel
    ) -> None:
        # Honest boundary: holding the anchor would need a negative scroll
        # position, and the timeline starts at zero. The view stays pinned to
        # the start instead, which is the right thing to do.
        view = panel.view
        view.horizontalScrollBar().setValue(0)
        panel.scene.set_pixels_per_second(40.0)
        view.zoom_at(QPoint(200, 60), 0.5)
        assert view.horizontalScrollBar().value() == 0

    def test_zoom_in_increases_pixels_per_second(self, panel: TimelinePanel) -> None:
        before = panel.scene.pixels_per_second
        panel.view.zoom_at(QPoint(100, 60), 1.25)
        assert panel.scene.pixels_per_second > before

    def test_zoom_is_clamped_at_both_ends(self, panel: TimelinePanel) -> None:
        for _ in range(60):
            panel.view.zoom_at(QPoint(10, 60), 1.25)
        assert panel.scene.pixels_per_second == MAX_PIXELS_PER_SECOND
        for _ in range(120):
            panel.view.zoom_at(QPoint(10, 60), 1 / 1.25)
        assert panel.scene.pixels_per_second == MIN_PIXELS_PER_SECOND

    def test_zooming_at_the_clamp_is_a_no_op(self, panel: TimelinePanel) -> None:
        panel.scene.set_pixels_per_second(MAX_PIXELS_PER_SECOND)
        before = panel.view.horizontalScrollBar().value()
        panel.view.zoom_at(QPoint(100, 60), 2.0)
        assert panel.view.horizontalScrollBar().value() == before


class TestFitLongSources:
    """An hour-plus source is a normal thing to edit."""

    LECTURE_SECONDS = 5509  # 1:31:49

    def long_panel(
        self, qapp: QApplication, seconds: int, width: int = 1200
    ) -> TimelinePanel:
        # Wide enough that the viewport, which is the panel less the 120px
        # header column, is the width the arithmetic below assumes.
        p = TimelinePanel()
        p.resize(width, 300)
        p.show()
        qapp.processEvents()
        p.set_project(make_project(seconds=seconds))
        qapp.processEvents()
        return p

    def test_a_lecture_length_source_fits(self, qapp: QApplication) -> None:
        p = self.long_panel(qapp, self.LECTURE_SECONDS)
        outcomes: list[tuple] = []
        p.fitted.connect(lambda *args: outcomes.append(args))

        p.view.fit_project()

        assert outcomes[-1][0] == FitOutcome.FITTED
        width = p.scene.ticks_to_x(p.scene.project().duration)
        assert width <= p.view.viewport().width() + 1

    def test_five_and_a_half_hours_fits_at_the_new_floor(
        self, qapp: QApplication
    ) -> None:
        # 19800s at 0.05 px/s is 990px, which is what the new floor was
        # chosen to allow. It needs a viewport of at least that.
        p = self.long_panel(qapp, 19800)
        assert p.view.viewport().width() >= 1006

        p.view.fit_project()
        assert p.scene.pixels_per_second >= MIN_PIXELS_PER_SECOND
        width = p.scene.ticks_to_x(p.scene.project().duration)
        assert width <= p.view.viewport().width() + 1
        assert width == pytest.approx(990, rel=0.3)

    def test_the_same_project_is_clamped_in_a_narrower_window(
        self, qapp: QApplication
    ) -> None:
        # The floor is absolute, not relative to the window: 5.5 hours needs
        # 990px and a small window simply does not have them.
        p = self.long_panel(qapp, 19800, width=700)
        outcomes: list[tuple] = []
        p.fitted.connect(lambda *args: outcomes.append(args))
        p.view.fit_project()
        assert outcomes[-1][0] == FitOutcome.CLAMPED

    def test_something_too_long_even_for_the_floor_shows_what_it_can(
        self, qapp: QApplication
    ) -> None:
        # Twenty hours. It cannot fit, so it must zoom out as far as it goes
        # and say so, not silently leave the view where it was.
        p = self.long_panel(qapp, 20 * 3600)
        before = p.scene.set_pixels_per_second(100.0)
        outcomes: list[tuple] = []
        p.fitted.connect(lambda *args: outcomes.append(args))

        p.view.fit_project()

        assert p.scene.pixels_per_second == MIN_PIXELS_PER_SECOND
        assert p.scene.pixels_per_second != before
        outcome, pps, visible = outcomes[-1]
        assert outcome == FitOutcome.CLAMPED
        assert pps == MIN_PIXELS_PER_SECOND
        assert 0.0 < visible < 1.0

    def test_the_empty_case_is_reported_rather_than_silent(
        self, qapp: QApplication
    ) -> None:
        p = TimelinePanel()
        p.resize(900, 300)
        p.show()
        qapp.processEvents()
        p.set_project(Project(name="empty"))
        outcomes: list[tuple] = []
        p.fitted.connect(lambda *args: outcomes.append(args))

        p.view.fit_project()
        assert outcomes[-1][0] == FitOutcome.EMPTY

    def test_a_fitted_project_reports_the_whole_thing_visible(
        self, qapp: QApplication
    ) -> None:
        p = self.long_panel(qapp, 120)
        outcomes: list[tuple] = []
        p.fitted.connect(lambda *args: outcomes.append(args))
        p.view.fit_project()
        assert outcomes[-1][0] == FitOutcome.FITTED
        assert outcomes[-1][2] == 1.0

    def test_a_very_short_project_still_counts_as_fitted(
        self, qapp: QApplication
    ) -> None:
        # Clamped at the top means it cannot fill the window, but all of it is
        # on screen, which is what fit means.
        p = self.long_panel(qapp, 1)
        outcomes: list[tuple] = []
        p.fitted.connect(lambda *args: outcomes.append(args))
        p.view.fit_project()
        assert p.scene.pixels_per_second == MAX_PIXELS_PER_SECOND
        assert outcomes[-1][0] == FitOutcome.FITTED

    def test_the_ruler_stays_legible_on_a_fitted_lecture(
        self, qapp: QApplication
    ) -> None:
        from ui.timeline.ruler_item import MIN_LABEL_SPACING, choose_interval

        p = self.long_panel(qapp, self.LECTURE_SECONDS)
        p.view.fit_project()
        pps = p.scene.pixels_per_second

        labels = p.scene._ruler.labels()
        assert len(labels) >= 3, "a fitted lecture needs more than a couple of marks"
        assert choose_interval(pps) * pps >= MIN_LABEL_SPACING
        # Past an hour, so every label is HH:MM:SS.
        assert all(label.label.count(":") == 2 for label in labels)


class TestFit:
    def test_fit_shows_the_whole_project(self, panel: TimelinePanel) -> None:
        panel.scene.set_pixels_per_second(400.0)
        panel.view.fit_project()
        width = panel.scene.ticks_to_x(panel.scene.project().duration)
        assert width <= panel.view.viewport().width() + 1

    def test_fit_scrolls_back_to_the_start(self, panel: TimelinePanel) -> None:
        panel.view.horizontalScrollBar().setValue(200)
        panel.view.fit_project()
        assert panel.view.horizontalScrollBar().value() == 0

    def test_fit_on_an_empty_project_does_nothing_bad(
        self, qapp: QApplication
    ) -> None:
        p = TimelinePanel()
        p.resize(900, 300)
        p.set_project(Project(name="empty"))
        before = p.scene.pixels_per_second
        assert p.view.fit_project() == before

    def test_fit_stays_within_the_clamp(self, panel: TimelinePanel) -> None:
        panel.set_project(make_project(seconds=1))
        pps = panel.view.fit_project()
        assert MIN_PIXELS_PER_SECOND <= pps <= MAX_PIXELS_PER_SECOND


class TestHeaderColumn:
    def test_fixed_width(self, panel: TimelinePanel) -> None:
        assert panel.headers.width() == HEADER_WIDTH

    def test_a_row_per_lane(self, panel: TimelinePanel) -> None:
        assert len(panel.headers._rows) == len(panel.scene.lanes())

    def test_rows_line_up_with_lanes(self, panel: TimelinePanel) -> None:
        panel.headers.relayout()
        for lane in panel.scene.lanes():
            row = panel.headers._rows[lane.track_id]
            assert row.y() == pytest.approx(lane.top, abs=1)
            assert row.height() == pytest.approx(lane.height, abs=1)

    def test_the_bottom_lane_row_is_not_clipped(self, qapp: QApplication) -> None:
        # The add-track buttons used to sit inside this column and take height
        # from it, so the last lane's mute toggle was cut off while the view
        # still showed that lane.
        p = TimelinePanel()
        p.resize(900, 220)
        p.show()  # the layout has to run before the geometry means anything
        qapp.processEvents()
        p.set_project(make_project(video=1, audio=1))
        qapp.processEvents()
        p.headers.relayout()

        lanes = p.scene.lanes()
        bottom = lanes[-1]
        row = p.headers._rows[bottom.track_id]
        assert row.y() + row.height() <= p.headers._lane_host.height(), (
            "the bottom lane's header row is clipped"
        )

    def test_the_lane_host_is_as_tall_as_the_column(
        self, panel: TimelinePanel
    ) -> None:
        panel.headers.relayout()
        assert panel.headers._lane_host.height() == panel.headers.height()

    def test_the_add_buttons_are_outside_the_lane_area(
        self, panel: TimelinePanel
    ) -> None:
        assert panel.headers.add_video_button.parent() is not panel.headers._lane_host

    def test_rows_follow_vertical_scroll(self, panel: TimelinePanel) -> None:
        lane = panel.scene.lanes()[0]
        before = panel.headers._rows[lane.track_id].y()
        panel.headers.set_scroll_offset(40.0)
        after = panel.headers._rows[lane.track_id].y()
        assert after == pytest.approx(before - 40, abs=1)

    def test_mute_toggle_emits(self, panel: TimelinePanel) -> None:
        seen: list[tuple[str, bool]] = []
        panel.mute_toggled.connect(lambda tid, muted: seen.append((tid, muted)))
        box = panel.headers.mute_box("a0")
        box.setChecked(True)
        assert seen == [("a0", True)]

    def test_mute_box_reflects_the_model(self, qapp: QApplication) -> None:
        project = make_project()
        project.tracks[1].muted = True
        p = TimelinePanel()
        p.set_project(project)
        assert p.headers.mute_box("a0").isChecked() is True


class TestAddTrackAffordance:
    def test_add_video_is_disabled_when_a_video_track_exists(
        self, panel: TimelinePanel
    ) -> None:
        assert panel.headers.add_video_button.isEnabled() is False

    def test_the_label_states_the_limit(self, panel: TimelinePanel) -> None:
        """A greyed-out "+ Video" reads as a broken button, so the label
        carries the reason and the tooltip carries the explanation."""
        assert panel.headers.add_video_button.text() == VIDEO_LIMIT_LABEL

    def test_the_tooltip_says_why(self, panel: TimelinePanel) -> None:
        assert panel.headers.add_video_button.toolTip() == MULTIPLE_VIDEO_TRACK_TOOLTIP

    def test_add_video_is_enabled_when_there_is_no_video_track(
        self, qapp: QApplication
    ) -> None:
        p = TimelinePanel()
        p.set_project(make_project(video=0, audio=2))
        assert p.headers.add_video_button.isEnabled() is True
        assert p.headers.add_video_button.text() == ADD_VIDEO_LABEL
        assert "compositing" not in p.headers.add_video_button.toolTip()

    def test_audio_tracks_are_unlimited(self, qapp: QApplication) -> None:
        p = TimelinePanel()
        p.set_project(make_project(video=1, audio=6))
        assert p.headers.add_audio_button.isEnabled() is True

    def test_the_state_updates_after_a_rebuild(self, qapp: QApplication) -> None:
        p = TimelinePanel()
        p.set_project(make_project(video=0, audio=1))
        assert p.headers.add_video_button.isEnabled() is True
        p.set_project(make_project(video=1, audio=1))
        assert p.headers.add_video_button.isEnabled() is False

    def test_the_button_emits_its_kind(self, qapp: QApplication) -> None:
        p = TimelinePanel()
        p.set_project(make_project(video=0, audio=1))
        seen: list[str] = []
        p.add_track_requested.connect(seen.append)
        p.headers.add_video_button.click()
        p.headers.add_audio_button.click()
        assert seen == ["video", "audio"]


class TestRulerFollowsTheViewport:
    def test_the_view_tells_the_scene_its_width(self, qapp: QApplication) -> None:
        p = TimelinePanel()
        p.resize(900, 300)
        p.show()
        qapp.processEvents()
        assert p.scene.viewport_width() == p.view.viewport().width()
        assert p.scene.viewport_width() > 0

    def test_resizing_the_window_resizes_the_ruler(
        self, qapp: QApplication
    ) -> None:
        p = TimelinePanel()
        p.resize(600, 300)
        p.show()
        qapp.processEvents()
        narrow = p.scene.sceneRect().width()

        p.resize(1400, 300)
        qapp.processEvents()
        assert p.scene.sceneRect().width() > narrow

    def test_an_empty_project_gets_a_full_width_ruler_in_the_real_view(
        self, qapp: QApplication
    ) -> None:
        p = TimelinePanel()
        p.resize(900, 300)
        p.show()
        qapp.processEvents()
        p.set_project(Project(name="empty"))
        qapp.processEvents()

        viewport = p.view.viewport().width()
        assert p.scene.sceneRect().width() >= viewport
        assert p.scene._ruler.boundingRect().width() >= viewport
        # And no horizontal scrollbar, because there is nothing to scroll to.
        assert p.view.horizontalScrollBar().maximum() == 0


class TestScrolling:
    def test_plain_wheel_scrolls_horizontally(self, panel: TimelinePanel) -> None:
        panel.scene.set_pixels_per_second(400.0)
        panel.view.resize(400, 200)
        bar = panel.view.horizontalScrollBar()
        bar.setValue(0)
        before_v = panel.view.verticalScrollBar().value()

        from PySide6.QtGui import QWheelEvent
        from PySide6.QtCore import QPointF

        event = QWheelEvent(
            QPointF(100, 50),
            QPointF(100, 50),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        panel.view.wheelEvent(event)
        assert bar.value() > 0
        assert panel.view.verticalScrollBar().value() == before_v

    def test_ctrl_wheel_zooms_instead_of_scrolling(
        self, panel: TimelinePanel
    ) -> None:
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtCore import QPointF

        before = panel.scene.pixels_per_second
        event = QWheelEvent(
            QPointF(100, 50),
            QPointF(100, 50),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        panel.view.wheelEvent(event)
        assert panel.scene.pixels_per_second > before
