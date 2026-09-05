"""Timeline scene tests.

The coordinate mapping is the important part. Every later phase reads mouse
positions through ticks_to_x and x_to_ticks; if they disagree under zoom, clips
will land a frame or two off wherever the user drops them and it will look like
a drag bug rather than a mapping bug.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.model import Clip, Project, Track  # noqa: E402
from core.timebase import TICKS_PER_SECOND as SEC  # noqa: E402
from core.timebase import FrameRate, frames_to_ticks, snap_to_frame  # noqa: E402
from ui.timeline.ruler_item import (  # noqa: E402
    MIN_LABEL_SPACING,
    RULER_INTERVALS,
    choose_interval,
    compute_marks,
    expected_label_count,
    label_for_second,
    uses_long_labels,
)
from ui.timeline.timeline_scene import (  # noqa: E402
    AUDIO_LANE_HEIGHT,
    MAX_PIXELS_PER_SECOND,
    MIN_PIXELS_PER_SECOND,
    RULER_HEIGHT,
    VIDEO_LANE_HEIGHT,
    TimelineScene,
)

SRC = Path("C:/media/a.mp4")

#: Spans the clamped range, plus the awkward fractional values in between.
ZOOM_LEVELS = [0.05, 0.2, 1.0, 2.0, 3.7, 10.0, 40.0, 97.3, 200.0, 400.0]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def build_project(rate: FrameRate = FrameRate(30, 1)) -> Project:
    return Project(
        name="p",
        frame_rate=rate,
        tracks=[
            Track(
                id="v1",
                name="V1",
                kind="video",
                clips=[
                    Clip(id="a", src=SRC, src_in=0, src_out=2 * SEC, timeline_start=0),
                    Clip(
                        id="b",
                        src=SRC,
                        src_in=0,
                        src_out=2 * SEC,
                        timeline_start=3 * SEC,
                    ),
                    Clip(
                        id="c",
                        src=SRC,
                        src_in=0,
                        src_out=SEC,
                        timeline_start=6 * SEC,
                    ),
                ],
            ),
            Track(
                id="a1",
                name="A1",
                kind="audio",
                clips=[
                    Clip(id="d", src=SRC, src_in=0, src_out=5 * SEC, timeline_start=0)
                ],
            ),
        ],
    )


@pytest.fixture
def scene(qapp: QApplication) -> TimelineScene:
    s = TimelineScene()
    s.rebuild(build_project())
    return s


# --------------------------------------------------------------------------
# The coordinate mapping
# --------------------------------------------------------------------------

class TestCoordinateRoundTrip:
    """tick -> x -> tick must land on the same frame, at every zoom."""

    @pytest.mark.parametrize("pps", ZOOM_LEVELS)
    @pytest.mark.parametrize("rate", [FrameRate(30, 1), FrameRate(30000, 1001)])
    def test_frame_boundaries_survive_the_round_trip(
        self, scene: TimelineScene, pps: float, rate: FrameRate
    ) -> None:
        scene.rebuild(build_project(rate))
        scene.set_pixels_per_second(pps)

        for frame in (0, 1, 2, 17, 300, 5000, 90000):
            ticks = frames_to_ticks(frame, rate)
            recovered = scene.x_to_ticks(scene.ticks_to_x(ticks))
            assert recovered == ticks, (
                f"frame {frame} at {pps}px/s: {ticks} -> "
                f"{scene.ticks_to_x(ticks)} -> {recovered}"
            )

    @pytest.mark.parametrize("pps", ZOOM_LEVELS)
    def test_arbitrary_ticks_land_on_a_frame_boundary(
        self, scene: TimelineScene, pps: float
    ) -> None:
        # A position picked with a mouse is not on a boundary. It must become
        # one, and it must be the nearest one.
        rate = scene.frame_rate()
        scene.set_pixels_per_second(pps)
        for ticks in (1, 999, 4001, 123457, 7_000_001):
            recovered = scene.x_to_ticks(scene.ticks_to_x(ticks))
            assert recovered == snap_to_frame(ticks, rate)

    @pytest.mark.parametrize("pps", ZOOM_LEVELS)
    def test_the_round_trip_is_idempotent(
        self, scene: TimelineScene, pps: float
    ) -> None:
        scene.set_pixels_per_second(pps)
        for ticks in (0, 4321, 555_555):
            once = scene.x_to_ticks(scene.ticks_to_x(ticks))
            twice = scene.x_to_ticks(scene.ticks_to_x(once))
            assert twice == once

    @pytest.mark.parametrize("pps", ZOOM_LEVELS)
    def test_x_to_ticks_unsnapped_is_close_to_the_truth(
        self, scene: TimelineScene, pps: float
    ) -> None:
        # The zoom anchor uses this. It must not quantise, but it must be
        # accurate to well under a frame.
        scene.set_pixels_per_second(pps)
        for ticks in (0, 12345, 9_999_999):
            recovered = scene.x_to_ticks(scene.ticks_to_x(ticks), snap=False)
            assert abs(recovered - ticks) <= 1

    def test_x_to_ticks_never_goes_negative(self, scene: TimelineScene) -> None:
        assert scene.x_to_ticks(-500.0) == 0

    def test_ticks_to_x_is_linear_in_zoom(self, scene: TimelineScene) -> None:
        scene.set_pixels_per_second(10.0)
        at_ten = scene.ticks_to_x(5 * SEC)
        scene.set_pixels_per_second(20.0)
        assert scene.ticks_to_x(5 * SEC) == pytest.approx(at_ten * 2)

    def test_one_second_is_pixels_per_second_pixels(self, scene: TimelineScene) -> None:
        for pps in ZOOM_LEVELS:
            scene.set_pixels_per_second(pps)
            assert scene.ticks_to_x(SEC) == pytest.approx(pps)


class TestZoomClamp:
    def test_clamped_below(self, scene: TimelineScene) -> None:
        assert scene.set_pixels_per_second(0.01) == MIN_PIXELS_PER_SECOND

    def test_clamped_above(self, scene: TimelineScene) -> None:
        assert scene.set_pixels_per_second(100_000) == MAX_PIXELS_PER_SECOND

    def test_the_property_setter_clamps_too(self, scene: TimelineScene) -> None:
        scene.pixels_per_second = 1e9
        assert scene.pixels_per_second == MAX_PIXELS_PER_SECOND

    def test_zoom_changed_is_emitted_once(self, scene: TimelineScene) -> None:
        seen: list[float] = []
        scene.zoom_changed.connect(seen.append)
        scene.set_pixels_per_second(80.0)
        scene.set_pixels_per_second(80.0)  # no change, no signal
        assert seen == [80.0]


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

class TestLanes:
    def test_video_lanes_sit_above_audio(self, scene: TimelineScene) -> None:
        lanes = scene.lanes()
        assert [lane.kind for lane in lanes] == ["video", "audio"]
        assert lanes[0].top < lanes[1].top

    def test_lane_heights(self, scene: TimelineScene) -> None:
        lanes = scene.lanes()
        assert lanes[0].height == VIDEO_LANE_HEIGHT
        assert lanes[1].height == AUDIO_LANE_HEIGHT

    def test_lanes_start_below_the_ruler(self, scene: TimelineScene) -> None:
        assert scene.lanes()[0].top >= RULER_HEIGHT

    def test_lanes_do_not_overlap(self, scene: TimelineScene) -> None:
        lanes = scene.lanes()
        for upper, lower in zip(lanes, lanes[1:]):
            assert upper.bottom <= lower.top

    def test_audio_first_in_the_model_still_renders_video_on_top(
        self, qapp: QApplication
    ) -> None:
        project = Project(
            name="p",
            tracks=[
                Track(id="a1", name="A1", kind="audio"),
                Track(id="v1", name="V1", kind="video"),
            ],
        )
        s = TimelineScene()
        s.rebuild(project)
        assert [lane.kind for lane in s.lanes()] == ["video", "audio"]

    def test_track_at_y(self, scene: TimelineScene) -> None:
        lanes = scene.lanes()
        assert scene.track_at_y(lanes[0].top + 1) == "v1"
        assert scene.track_at_y(lanes[1].top + 1) == "a1"
        assert scene.track_at_y(0) is None
        assert scene.track_at_y(100_000) is None

    def test_empty_project_has_no_lanes(self, qapp: QApplication) -> None:
        s = TimelineScene()
        s.rebuild(None)
        assert s.lanes() == []
        assert s.clip_items() == []


class TestClipGeometry:
    def test_positions_and_widths_match_the_model(self, scene: TimelineScene) -> None:
        scene.set_pixels_per_second(100.0)
        item = scene.clip_item("b")
        assert item is not None
        # Starts at 3s, runs 2s, at 100 px/s.
        assert item.pos().x() == pytest.approx(300.0)
        assert item.rect().width() == pytest.approx(200.0)

    def test_clips_sit_in_their_own_lane(self, scene: TimelineScene) -> None:
        video_lane = scene.lane_for_track("v1")
        audio_lane = scene.lane_for_track("a1")
        assert scene.clip_item("a").pos().y() == video_lane.top
        assert scene.clip_item("d").pos().y() == audio_lane.top

    def test_geometry_follows_zoom(self, scene: TimelineScene) -> None:
        scene.set_pixels_per_second(50.0)
        narrow = scene.clip_item("a").rect().width()
        scene.set_pixels_per_second(200.0)
        assert scene.clip_item("a").rect().width() == pytest.approx(narrow * 4)

    def test_a_clip_narrower_than_a_pixel_still_has_width(
        self, scene: TimelineScene
    ) -> None:
        scene.set_pixels_per_second(MIN_PIXELS_PER_SECOND)
        for item in scene.clip_items():
            assert item.rect().width() >= 1.0

    def test_items_hold_ids_not_model_objects(self, scene: TimelineScene) -> None:
        item = scene.clip_item("a")
        assert item.clip_id == "a"
        assert item.track_id == "v1"
        assert not hasattr(item, "clip")

    def test_rebuild_replaces_every_item(self, scene: TimelineScene) -> None:
        before = scene.clip_item("a")
        scene.rebuild(build_project())
        after = scene.clip_item("a")
        assert after is not before

    def test_muted_track_marks_its_clips(self, qapp: QApplication) -> None:
        project = build_project()
        project.tracks[1].muted = True
        s = TimelineScene()
        s.rebuild(project)
        assert s.clip_item("d")._muted is True


class TestSceneRect:
    def test_covers_the_project(self, scene: TimelineScene) -> None:
        scene.set_pixels_per_second(100.0)
        # Project runs to 7s; the rect adds trailing pad.
        assert scene.sceneRect().width() > scene.ticks_to_x(7 * SEC)

    def test_height_covers_every_lane(self, scene: TimelineScene) -> None:
        assert scene.sceneRect().height() >= scene.lanes()[-1].bottom


class TestRulerHasNoStaleMarks:
    """The zoom path and the rebuild path must produce the same ruler.

    The reported defect: zooming changed pixels_per_second without changing
    the ruler's bounding rect, so nothing invalidated the item and marks from
    two zoom levels were on screen at once. Releasing Ctrl and triggering a
    rebuild put it right, which is precisely the divergence.
    """

    ZOOM_SEQUENCE = [2.0, 400.0, 10.0, 200.0, 40.0]

    @pytest.fixture
    def wide(self, qapp: QApplication) -> TimelineScene:
        s = TimelineScene()
        s.set_viewport_width(900.0)
        s.rebuild(build_project())
        return s

    @pytest.mark.parametrize("pps", ZOOM_SEQUENCE)
    def test_label_count_matches_the_interval_and_width(
        self, wide: TimelineScene, pps: float
    ) -> None:
        wide.set_pixels_per_second(pps)
        width = wide.sceneRect().width()
        assert len(wide._ruler.labels()) == expected_label_count(width, pps)

    def test_no_marks_survive_a_zoom_change(self, wide: TimelineScene) -> None:
        # Walk the whole sequence, checking after every step rather than only
        # at the end: a stale mark from step 2 must not be visible at step 3.
        for pps in self.ZOOM_SEQUENCE:
            wide.set_pixels_per_second(pps)
            labels = wide._ruler.labels()
            width = wide.sceneRect().width()

            assert len(labels) == expected_label_count(width, pps), (
                f"wrong label count at {pps} px/s"
            )
            xs = [mark.x for mark in labels]
            assert len(xs) == len(set(xs)), f"duplicate label positions at {pps} px/s"
            assert xs == sorted(xs), f"labels out of order at {pps} px/s"
            assert all(0 <= x <= width for x in xs)

            # Every label belongs to the interval currently in force. A mark
            # from a previous zoom level would fail this.
            interval = choose_interval(pps)
            for mark in labels:
                assert mark.seconds % interval == 0
                assert mark.label == label_for_second(mark.seconds)

    def test_marks_are_spaced_by_exactly_one_interval(
        self, wide: TimelineScene
    ) -> None:
        for pps in self.ZOOM_SEQUENCE:
            wide.set_pixels_per_second(pps)
            xs = [mark.x for mark in wide._ruler.labels()]
            spacing = choose_interval(pps) * pps
            for earlier, later in zip(xs, xs[1:]):
                assert later - earlier == pytest.approx(spacing)

    @pytest.mark.parametrize("pps", ZOOM_SEQUENCE)
    def test_the_zoom_path_and_the_rebuild_path_agree(
        self, wide: TimelineScene, pps: float
    ) -> None:
        # Phase 4 rebuilds after every command. If these two produced
        # different rulers, the display would change under the user for no
        # reason they could see.
        wide.set_pixels_per_second(pps)
        via_zoom = wide._ruler.marks()

        wide.rebuild(build_project())
        via_rebuild = wide._ruler.marks()

        assert via_zoom == via_rebuild
        assert len(via_zoom) > 0

    def test_a_rebuild_after_a_zoom_changes_nothing_on_screen(
        self, wide: TimelineScene
    ) -> None:
        # This is the "releasing Ctrl fixes it" case, from the other side:
        # after the fix there is nothing left for the rebuild to correct.
        for pps in self.ZOOM_SEQUENCE:
            wide.set_pixels_per_second(pps)
            before = wide._ruler.marks()
            wide.rebuild(build_project())
            assert wide._ruler.marks() == before

    def test_zooming_repaints_even_when_the_width_is_unchanged(
        self, qapp: QApplication
    ) -> None:
        # The exact mechanism of the defect. On an empty project the scene
        # width is pinned to the viewport, so the bounding rect never changes
        # and only an explicit update() will redraw the ruler.
        s = TimelineScene()
        s.set_viewport_width(780.0)
        s.rebuild(Project(name="empty"))

        updates: list[int] = []
        original = s._ruler.update
        s._ruler.update = lambda *args: (updates.append(1), original(*args))[1]

        widths = []
        for pps in (2.0, 10.0, 40.0, 100.0, 200.0):
            s.set_pixels_per_second(pps)
            widths.append(s._ruler.boundingRect().width())

        assert len(set(widths)) == 1, "width should not change on an empty project"
        assert len(updates) == 5, "the ruler was not repainted on every zoom"

    def test_marks_come_only_from_the_layout_function(
        self, wide: TimelineScene
    ) -> None:
        # paint() draws self._marks and computes nothing, so what the tests
        # inspect is exactly what reaches the screen.
        wide.set_pixels_per_second(63.0)
        assert wide._ruler.marks() == compute_marks(
            wide.sceneRect().width(), 63.0
        )


class TestRulerSpansTheViewport:
    """The ruler is a time axis, not a bar showing the project's extent."""

    def test_an_empty_project_still_gets_a_full_width_ruler(
        self, qapp: QApplication
    ) -> None:
        # The reported defect: duration 0 collapsed the ruler to a stub at the
        # left with blank grey across the rest of the window.
        s = TimelineScene()
        s.set_viewport_width(900.0)
        s.rebuild(Project(name="empty"))
        assert s.project().duration == 0
        assert s.content_width() >= 900.0
        assert s.sceneRect().width() >= 900.0
        assert s._ruler.boundingRect().width() >= 900.0

    def test_a_project_with_no_tracks_at_all(self, qapp: QApplication) -> None:
        s = TimelineScene()
        s.set_viewport_width(640.0)
        s.rebuild(None)
        assert s.sceneRect().width() >= 640.0

    def test_a_very_short_project_still_fills_the_window(
        self, qapp: QApplication
    ) -> None:
        s = TimelineScene()
        s.set_viewport_width(900.0)
        s.rebuild(
            Project(
                name="tiny",
                tracks=[
                    Track(
                        id="v1",
                        name="V1",
                        kind="video",
                        clips=[
                            Clip(
                                id="a",
                                src=SRC,
                                src_in=0,
                                src_out=SEC // 10,
                                timeline_start=0,
                            )
                        ],
                    )
                ],
            )
        )
        s.set_pixels_per_second(2.0)
        assert s.sceneRect().width() >= 900.0
        assert s._ruler.boundingRect().width() >= 900.0

    @pytest.mark.parametrize("pps", ZOOM_LEVELS)
    def test_the_ruler_fills_the_window_at_every_zoom(
        self, qapp: QApplication, pps: float
    ) -> None:
        # Zooming out used to crowd the labels into the left corner because
        # the ruler shrank with the project.
        s = TimelineScene()
        s.set_viewport_width(900.0)
        s.rebuild(build_project())
        s.set_pixels_per_second(pps)
        assert s._ruler.boundingRect().width() >= 900.0

    def test_a_long_project_is_wider_than_the_window(
        self, qapp: QApplication
    ) -> None:
        # The viewport is a floor, not a ceiling: a project that overflows the
        # window must still scroll.
        s = TimelineScene()
        s.set_viewport_width(300.0)
        s.rebuild(build_project())
        s.set_pixels_per_second(400.0)
        assert s.sceneRect().width() > 300.0
        assert s.sceneRect().width() >= s.ticks_to_x(7 * SEC)

    def test_widening_the_window_widens_the_ruler(self, scene: TimelineScene) -> None:
        scene.set_viewport_width(400.0)
        narrow = scene._ruler.boundingRect().width()
        scene.set_viewport_width(1600.0)
        assert scene._ruler.boundingRect().width() > narrow
        assert scene._ruler.boundingRect().width() >= 1600.0

    def test_lanes_are_drawn_across_the_full_width(
        self, qapp: QApplication
    ) -> None:
        # Lane backgrounds follow the scene width, so a lane reads as a
        # surface rather than stopping at the last clip.
        s = TimelineScene()
        s.set_viewport_width(900.0)
        s.rebuild(build_project())
        s.set_pixels_per_second(2.0)
        assert s.sceneRect().width() >= 900.0


class TestPlayhead:
    def test_moves_to_the_mapped_position(self, scene: TimelineScene) -> None:
        scene.set_pixels_per_second(60.0)
        scene.set_playhead(2 * SEC)
        assert scene.playhead_ticks() == 2 * SEC
        assert scene._playhead.x() == pytest.approx(120.0)

    def test_follows_zoom(self, scene: TimelineScene) -> None:
        scene.set_pixels_per_second(60.0)
        scene.set_playhead(2 * SEC)
        scene.set_pixels_per_second(120.0)
        assert scene._playhead.x() == pytest.approx(240.0)

    def test_clamped_at_zero(self, scene: TimelineScene) -> None:
        scene.set_playhead(-5)
        assert scene.playhead_ticks() == 0

    def test_drawn_above_the_clips(self, scene: TimelineScene) -> None:
        assert scene._playhead.zValue() > scene.clip_item("a").zValue()
        assert scene._playhead.zValue() > scene._ruler.zValue()


# --------------------------------------------------------------------------
# Ruler
# --------------------------------------------------------------------------

class TestRulerInterval:
    @pytest.mark.parametrize(
        "pps,expected",
        [
            # The six zoom levels, end to end of the clamped range.
            (0.05, 1800),
            (0.2, 600),
            (1.0, 300),
            (2.0, 60),
            (10.0, 10),
            (40.0, 5),
            (100.0, 1),
            (200.0, 1),
            (400.0, 1),
            # And the transitions in between.
            (0.14, 600),
            (0.27, 300),
            (1.4, 60),
            (3.0, 30),
            (8.0, 10),
            (16.0, 5),
            (80.0, 1),
        ],
    )
    def test_interval_for_zoom(self, pps: float, expected: int) -> None:
        assert choose_interval(pps) == expected

    def test_the_interval_actually_adapts(self) -> None:
        # Not a constant dressed up as a function: the interval shrinks as the
        # zoom grows, across the whole clamped range.
        levels = (0.05, 0.2, 1, 2, 10, 40, 100, 200, 400)
        chosen = [choose_interval(pps) for pps in levels]
        assert chosen == [1800, 600, 300, 60, 10, 5, 1, 1, 1]
        assert chosen == sorted(chosen, reverse=True)
        assert set(chosen) <= set(RULER_INTERVALS)

    def test_every_interval_up_to_thirty_minutes_is_reachable(self) -> None:
        # 3600 is deliberately not in here. At the 0.05 px/s floor a 30 minute
        # interval is already 90px, over the 80px minimum, so the one hour
        # interval is never selected. It is headroom for a lower floor, not
        # dead weight, but it should not be claimed as reachable.
        reached = {choose_interval(pps / 100) for pps in range(5, 40001)}
        assert reached == set(RULER_INTERVALS) - {3600}
        assert max(reached) == 1800

    def test_the_hour_interval_is_headroom_below_the_current_floor(self) -> None:
        assert choose_interval(MIN_PIXELS_PER_SECOND) == 1800
        # It would be chosen if the floor ever went below 80/1800.
        assert choose_interval(0.04) == 3600

    @pytest.mark.parametrize("pps", ZOOM_LEVELS)
    def test_always_one_of_the_allowed_intervals(self, pps: float) -> None:
        assert choose_interval(pps) in RULER_INTERVALS

    def test_labels_never_overlap_anywhere_in_the_clamped_range(self) -> None:
        # Swept finely across the whole zoom range, including the decade below
        # 1 px/s that the new floor opened up: the gap between two labels is
        # never under the minimum, so they can never touch, and there is no
        # band where nothing qualifies.
        pps = MIN_PIXELS_PER_SECOND
        while pps <= MAX_PIXELS_PER_SECOND:
            interval = choose_interval(pps)
            gap = interval * pps
            assert interval in RULER_INTERVALS
            assert gap >= MIN_LABEL_SPACING, f"labels {gap:.1f}px apart at {pps} px/s"
            pps = round(pps + 0.01, 4)

    def test_the_smallest_interval_that_fits_is_the_one_chosen(self) -> None:
        # Choosing a larger interval than necessary would waste the ruler.
        for pps in (0.05, 0.2, 1.0, 2.0, 10.0, 40.0, 100.0, 200.0, 400.0):
            chosen = choose_interval(pps)
            smaller = [i for i in RULER_INTERVALS if i < chosen]
            assert all(i * pps < MIN_LABEL_SPACING for i in smaller)


class TestLabelFormat:
    """MM:SS under an hour, HH:MM:SS over it."""

    def test_short_form(self) -> None:
        assert label_for_second(0) == "00:00"
        assert label_for_second(5) == "00:05"
        assert label_for_second(65) == "01:05"
        assert label_for_second(600) == "10:00"

    def test_long_form(self) -> None:
        assert label_for_second(0, long_form=True) == "00:00:00"
        assert label_for_second(65, long_form=True) == "00:01:05"
        assert label_for_second(3600, long_form=True) == "01:00:00"
        assert label_for_second(3661, long_form=True) == "01:01:01"
        assert label_for_second(5509, long_form=True) == "01:31:49"

    def test_the_span_decides_which_form(self) -> None:
        # A 1000px ruler at 1 px/s spans 1000s, well under an hour.
        assert uses_long_labels(1000, 1.0) is False
        # The same ruler at 0.2 px/s spans 5000s, over an hour.
        assert uses_long_labels(1000, 0.2) is True
        assert uses_long_labels(1000, 1000 / 3600) is False  # exactly an hour
        assert uses_long_labels(1000, 0.2777) is True

    def test_a_long_ruler_labels_every_mark_in_long_form(self) -> None:
        # The lecture case: 1:31:49 at a zoom that shows all of it.
        marks = compute_marks(1000.0, 0.18)
        assert marks
        labels = [m.label for m in marks if m.is_labelled]
        assert all(label.count(":") == 2 for label in labels)
        assert labels[0] == "00:00:00"

    def test_a_short_ruler_labels_every_mark_in_short_form(self) -> None:
        marks = compute_marks(1000.0, 40.0)
        labels = [m.label for m in marks if m.is_labelled]
        assert all(label.count(":") == 1 for label in labels)
        assert labels[0] == "00:00"

    def test_the_format_does_not_change_along_one_ruler(self) -> None:
        # Every label on a given ruler is in the same form, so scrolling does
        # not make the format flip halfway across.
        for pps in (0.05, 0.2, 1.0, 40.0, 400.0):
            labels = [m.label for m in compute_marks(1200.0, pps) if m.is_labelled]
            assert len({label.count(":") for label in labels}) == 1

    def test_minutes_never_run_past_sixty(self) -> None:
        # The actual defect with MM:SS past an hour: it reads 90:00.
        marks = compute_marks(2000.0, 0.2)
        for mark in marks:
            if mark.is_labelled:
                minutes = int(mark.label.split(":")[-2])
                assert minutes < 60
