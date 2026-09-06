"""Timeline gestures.

Two halves. The arithmetic that decides where a gesture lands is pure and is
tested directly. The state machine is exercised by calling
TimelineInteraction with scene positions, which is what the view does; no Qt
mouse events are synthesised.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.commands import (  # noqa: E402
    AddClipFromMedia,
    CommandStack,
    MoveClip,
    TrimClip,
)
from core.model import Clip, Project, Track  # noqa: E402
from core.timebase import TICKS_PER_SECOND as T  # noqa: E402
from core.timebase import FrameRate  # noqa: E402
from ui.timeline.interaction import (  # noqa: E402
    DRAG_THRESHOLD_PIXELS,
    MEDIA_MIME,
    SNAP_PIXELS,
    TRIM_HANDLE_PIXELS,
    Span,
    TimelineInteraction,
    Zone,
    clamp_move,
    clamp_trim_left,
    clamp_trim_right,
    decode_media_payload,
    handle_width,
    media_payload,
    snap_position,
    snap_targets,
    track_accepts_media,
)
from ui.timeline.timeline_scene import RULER_HEIGHT, TimelineScene  # noqa: E402

SRC = Path("clip.mp4")
NO_MODIFIER = Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_project() -> Project:
    return Project(
        name="Test",
        frame_rate=FrameRate(30, 1),
        tracks=[Track(name="V1", kind="video"), Track(name="A1", kind="audio")],
    )


def place(track: Track, start: int, duration: int, src_in: int = 0) -> Clip:
    clip = Clip(
        src=SRC, src_in=src_in, src_out=src_in + duration, timeline_start=start
    )
    track.clips.append(clip)
    track.clips.sort(key=lambda c: c.timeline_start)
    return clip


def spans(track: Track) -> list[tuple[int, int]]:
    return [(c.timeline_start, c.timeline_end) for c in track.clips]


# -- the arithmetic ---------------------------------------------------------


class TestSnapPosition:
    EDGES = (2 * T, 5 * T)

    def test_nothing_near_enough_is_left_alone(self) -> None:
        result = snap_position(9 * T, None, self.EDGES, T // 10)
        assert result.ticks == 9 * T and result.target is None

    def test_it_catches_a_clip_edge(self) -> None:
        result = snap_position(2 * T + 100, None, self.EDGES, T // 10)
        assert result.ticks == 2 * T and result.kind == "edge"

    def test_it_catches_zero(self) -> None:
        result = snap_position(50, None, self.EDGES, T // 10)
        assert result.ticks == 0 and result.kind == "zero"

    def test_the_playhead_wins_over_a_nearer_edge(self) -> None:
        """Priority order, not proximity: the playhead is what the user aimed at."""
        playhead = 2 * T + 900
        result = snap_position(2 * T + 800, playhead, self.EDGES, T)
        assert result.ticks == playhead and result.kind == "playhead"

    def test_the_nearest_edge_wins_among_edges(self) -> None:
        result = snap_position(4 * T, None, self.EDGES, 2 * T)
        assert result.ticks == 5 * T

    def test_a_zero_tolerance_snaps_to_nothing(self) -> None:
        """This is what Alt does."""
        result = snap_position(2 * T + 1, 2 * T, self.EDGES, 0)
        assert result.ticks == 2 * T + 1 and result.target is None

    def test_the_tolerance_is_inclusive(self) -> None:
        assert snap_position(2 * T + 10, None, self.EDGES, 10).ticks == 2 * T
        assert snap_position(2 * T + 11, None, self.EDGES, 10).ticks == 2 * T + 11


class TestSnapTargets:
    def test_it_collects_both_edges_of_every_clip_on_every_track(self) -> None:
        project = make_project()
        place(project.tracks[0], 0, T)
        place(project.tracks[1], 3 * T, T)
        assert snap_targets(project) == [0, T, 3 * T, 4 * T]

    def test_the_dragged_clip_is_excluded(self) -> None:
        """Otherwise it snaps to its own edges and cannot be moved."""
        project = make_project()
        clip = place(project.tracks[0], 2 * T, T)
        assert snap_targets(project, exclude=[clip.id]) == []

    def test_no_project_means_no_targets(self) -> None:
        assert snap_targets(None) == []


class TestHandleWidth:
    def test_a_wide_clip_gets_the_full_handle(self) -> None:
        assert handle_width(200) == TRIM_HANDLE_PIXELS

    def test_a_narrow_clip_keeps_a_body_to_grab(self) -> None:
        """Otherwise a zoomed-out clip is all handles and cannot be moved."""
        assert handle_width(3) == 1.0
        assert handle_width(1) < 0.5

    def test_it_never_goes_negative(self) -> None:
        assert handle_width(0) == 0.0


class TestClampMove:
    def test_a_clip_cannot_start_before_zero(self) -> None:
        assert clamp_move(-5 * T) == 0
        assert clamp_move(3 * T) == 3 * T


class TestClampTrimLeft:
    SPAN = Span(src_in=2 * T, src_out=6 * T, timeline_start=10 * T)

    def test_it_moves_src_in_and_start_by_the_same_amount(self) -> None:
        result = clamp_trim_left(self.SPAN, 11 * T, 0, T // 30)
        assert result.timeline_start == 11 * T
        assert result.src_in == 3 * T
        assert result.src_out == self.SPAN.src_out

    def test_it_stops_at_the_start_of_the_source(self) -> None:
        """Two seconds of head are available, so 8s is as far left as it goes."""
        result = clamp_trim_left(self.SPAN, 0, 0, T // 30)
        assert result.timeline_start == 8 * T and result.src_in == 0

    def test_it_stops_at_the_previous_clip(self) -> None:
        result = clamp_trim_left(self.SPAN, 8 * T, 9 * T, T // 30)
        assert result.timeline_start == 9 * T

    def test_it_never_takes_the_clip_below_one_frame(self) -> None:
        frame = T // 30
        result = clamp_trim_left(self.SPAN, 99 * T, 0, frame)
        assert result.timeline_end - result.timeline_start == frame

    def test_it_never_starts_before_the_timeline(self) -> None:
        span = Span(src_in=10 * T, src_out=12 * T, timeline_start=T)
        result = clamp_trim_left(span, -5 * T, 0, T // 30)
        assert result.timeline_start == 0


class TestClampTrimRight:
    SPAN = Span(src_in=T, src_out=3 * T, timeline_start=5 * T)

    def test_only_src_out_moves(self) -> None:
        result = clamp_trim_right(self.SPAN, 8 * T, None, None, T // 30)
        assert result.timeline_start == 5 * T and result.src_in == T
        assert result.src_out == 4 * T

    def test_it_stops_at_the_end_of_the_source(self) -> None:
        result = clamp_trim_right(self.SPAN, 99 * T, None, 4 * T, T // 30)
        assert result.src_out == 4 * T
        assert result.timeline_end == 8 * T

    def test_it_stops_at_the_next_clip(self) -> None:
        result = clamp_trim_right(self.SPAN, 99 * T, 6 * T, None, T // 30)
        assert result.timeline_end == 6 * T

    def test_it_never_takes_the_clip_below_one_frame(self) -> None:
        frame = T // 30
        result = clamp_trim_right(self.SPAN, 0, None, None, frame)
        assert result.duration == frame

    def test_an_unknown_source_length_leaves_the_tail_unbounded(self) -> None:
        """A project opened from disk has no probe result for its clips."""
        result = clamp_trim_right(self.SPAN, 500 * T, None, None, T // 30)
        assert result.timeline_end == 500 * T


class TestMediaPayload:
    def test_it_round_trips(self) -> None:
        raw = media_payload(SRC, 4 * T, True, True)
        data = decode_media_payload(raw)
        assert data["path"] == str(SRC)
        assert data["duration_ticks"] == 4 * T
        assert data["has_video"] is True

    def test_rubbish_decodes_to_nothing(self) -> None:
        assert decode_media_payload(b"not json") is None
        assert decode_media_payload(b'{"nope": 1}') is None

    def test_a_zero_length_source_is_rejected(self) -> None:
        assert decode_media_payload(media_payload(SRC, 0, True, False)) is None


class TestTrackAcceptsMedia:
    def test_video_goes_on_a_video_track(self) -> None:
        assert track_accepts_media("video", has_video=True)
        assert not track_accepts_media("audio", has_video=True)

    def test_audio_only_goes_on_an_audio_track(self) -> None:
        assert track_accepts_media("audio", has_video=False)
        assert not track_accepts_media("video", has_video=False)


# -- the state machine ------------------------------------------------------


class Harness:
    """A scene with a project on it, plus somewhere for the commands to go."""

    def __init__(self, pps: float = 40.0) -> None:
        self.project = make_project()
        self.video, self.audio = self.project.tracks
        self.scene = TimelineScene()
        self.scene.set_viewport_width(2000)
        self.scene.set_pixels_per_second(pps)
        self.interaction = TimelineInteraction(self.scene)
        self.commands: list = []
        self.rejections: list[str] = []
        self.interaction.command_requested.connect(self.commands.append)
        self.interaction.rejected.connect(self.rejections.append)

    def rebuild(self) -> None:
        self.scene.rebuild(self.project)

    def lane_y(self, track: Track) -> float:
        lane = self.scene.lane_for_track(track.id)
        return lane.top + lane.height / 2

    def point(self, ticks: int, track: Track) -> QPointF:
        return QPointF(self.scene.ticks_to_x(ticks), self.lane_y(track))

    def at_x(self, x: float, track: Track) -> QPointF:
        return QPointF(x, self.lane_y(track))

    def apply(self) -> None:
        """Run whatever commands the gestures produced, as the window would."""
        stack = CommandStack()
        for command in self.commands:
            stack.push(command, self.project)
        self.commands.clear()
        self.rebuild()


@pytest.fixture
def h(qapp: QApplication) -> Harness:
    return Harness()


class TestZones:
    def test_the_ruler_strip_is_its_own_zone(self, h: Harness) -> None:
        h.rebuild()
        zone, clip_id = h.interaction.zone_at(QPointF(100, RULER_HEIGHT - 1))
        assert zone == Zone.RULER and clip_id is None

    def test_empty_lane_space_is_nothing(self, h: Harness) -> None:
        h.rebuild()
        assert h.interaction.zone_at(h.point(T, h.video))[0] == Zone.NOTHING

    def test_the_middle_of_a_clip_is_its_body(self, h: Harness) -> None:
        clip = place(h.video, 0, 4 * T)
        h.rebuild()
        zone, clip_id = h.interaction.zone_at(h.point(2 * T, h.video))
        assert zone == Zone.BODY and clip_id == clip.id

    def test_the_edges_are_trim_handles(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        left = h.scene.ticks_to_x(0)
        right = h.scene.ticks_to_x(4 * T)
        assert h.interaction.zone_at(h.at_x(left + 1, h.video))[0] == Zone.TRIM_LEFT
        assert h.interaction.zone_at(h.at_x(right - 1, h.video))[0] == Zone.TRIM_RIGHT

    def test_the_handle_is_the_documented_width(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        left = h.scene.ticks_to_x(0)
        inside = left + TRIM_HANDLE_PIXELS + 1
        assert h.interaction.zone_at(h.at_x(inside, h.video))[0] == Zone.BODY

    def test_the_cursor_says_which_zone(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        assert (
            h.interaction.cursor_for(h.at_x(h.scene.ticks_to_x(0) + 1, h.video))
            == Qt.CursorShape.SizeHorCursor
        )
        assert (
            h.interaction.cursor_for(h.point(2 * T, h.video))
            == Qt.CursorShape.ArrowCursor
        )


class TestSelection:
    def test_a_click_selects_one_clip(self, h: Harness) -> None:
        first = place(h.video, 0, T)
        place(h.video, 2 * T, T)
        h.rebuild()
        h.interaction.press(h.point(T // 2, h.video), NO_MODIFIER)
        h.interaction.release(h.point(T // 2, h.video), NO_MODIFIER)
        assert h.interaction.selected_clip_ids() == [first.id]

    def test_ctrl_click_toggles(self, h: Harness) -> None:
        first = place(h.video, 0, T)
        second = place(h.video, 2 * T, T)
        h.rebuild()
        ctrl = Qt.KeyboardModifier.ControlModifier
        h.interaction.press(h.point(T // 2, h.video), ctrl)
        h.interaction.press(h.point(2 * T + T // 2, h.video), ctrl)
        assert set(h.interaction.selected_clip_ids()) == {first.id, second.id}
        h.interaction.press(h.point(T // 2, h.video), ctrl)
        assert h.interaction.selected_clip_ids() == [second.id]

    def test_ctrl_click_does_not_start_a_drag(self, h: Harness) -> None:
        place(h.video, 0, T)
        h.rebuild()
        h.interaction.press(h.point(T // 2, h.video), Qt.KeyboardModifier.ControlModifier)
        assert not h.interaction.is_dragging()

    def test_clicking_empty_space_clears_the_selection(self, h: Harness) -> None:
        place(h.video, 0, T)
        h.rebuild()
        h.interaction.press(h.point(T // 2, h.video), NO_MODIFIER)
        h.interaction.release(h.point(T // 2, h.video), NO_MODIFIER)
        h.interaction.press(h.point(5 * T, h.video), NO_MODIFIER)
        h.interaction.release(h.point(5 * T, h.video), NO_MODIFIER)
        assert h.interaction.selected_clip_ids() == []

    def test_a_rubber_band_selects_what_it_crosses(self, h: Harness) -> None:
        first = place(h.video, 0, T)
        second = place(h.video, 2 * T, T)
        place(h.video, 10 * T, T)
        h.rebuild()
        start = QPointF(h.scene.ticks_to_x(0) - 5, RULER_HEIGHT + 1)
        end = h.point(3 * T, h.video)
        h.interaction.press(start, NO_MODIFIER)
        h.interaction.drag(end, NO_MODIFIER)
        h.interaction.release(end, NO_MODIFIER)
        assert set(h.interaction.selected_clip_ids()) == {first.id, second.id}

    def test_a_selection_survives_a_rebuild(self, h: Harness) -> None:
        """An edit rebuilds the scene; the clip it acted on stays selected."""
        clip = place(h.video, 0, T)
        h.rebuild()
        h.interaction.press(h.point(T // 2, h.video), NO_MODIFIER)
        h.interaction.release(h.point(T // 2, h.video), NO_MODIFIER)
        h.rebuild()
        assert h.interaction.selected_clip_ids() == [clip.id]


class TestDragToMove:
    def test_one_command_on_release_and_not_before(self, h: Harness) -> None:
        """The rule the whole file exists for."""
        clip = place(h.video, 0, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        for step in range(1, 20):
            h.interaction.drag(h.point(T + step * T // 4, h.video), NO_MODIFIER)
        assert h.commands == []
        h.interaction.release(h.point(6 * T, h.video), NO_MODIFIER)
        assert len(h.commands) == 1
        assert isinstance(h.commands[0], MoveClip)
        assert h.commands[0].clip_id == clip.id

    def test_the_model_is_untouched_until_the_release(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(h.point(9 * T, h.video), NO_MODIFIER)
        assert spans(h.video) == [(0, 2 * T)]

    def test_the_grab_point_is_preserved(self, h: Harness) -> None:
        """Grabbing the middle and dropping at 8s puts the middle at 8s."""
        place(h.video, 0, 4 * T)
        h.rebuild()
        alt = Qt.KeyboardModifier.AltModifier
        h.interaction.press(h.point(2 * T, h.video), alt)
        h.interaction.drag(h.point(8 * T, h.video), alt)
        h.interaction.release(h.point(8 * T, h.video), alt)
        assert h.commands[0].new_timeline_start == 6 * T

    def test_a_drag_that_ends_where_it_started_emits_nothing(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(h.point(T, h.video), NO_MODIFIER)
        h.interaction.release(h.point(T, h.video), NO_MODIFIER)
        assert h.commands == []

    def test_an_overlapping_drop_is_refused(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        place(h.video, 6 * T, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(h.point(7 * T, h.video), NO_MODIFIER)
        h.interaction.release(h.point(7 * T, h.video), NO_MODIFIER)
        assert h.commands == []
        assert "overlap" in h.rejections[0]

    def test_the_ghost_turns_red_on_an_overlap(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        place(h.video, 6 * T, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(h.point(7 * T, h.video), NO_MODIFIER)
        assert h.interaction._ghost.isVisible()
        assert not h.interaction._ghost.is_valid()

    def test_dropping_onto_the_wrong_kind_of_lane_is_refused(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        target = h.point(T, h.audio)
        h.interaction.drag(target, NO_MODIFIER)
        assert not h.interaction._ghost.is_valid()
        # The red ghost is drawn on the lane being refused, under the pointer,
        # not left behind on the clip's own track.
        audio_lane = h.scene.lane_for_track(h.audio.id)
        assert h.interaction._ghost.pos().y() == audio_lane.top
        h.interaction.release(target, NO_MODIFIER)
        assert h.commands == []
        assert "video clip onto that track" in h.rejections[0]

    def test_it_moves_between_tracks_of_the_same_kind(self, h: Harness) -> None:
        second = Track(name="A2", kind="audio")
        h.project.tracks.append(second)
        clip = place(h.audio, 0, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(T, h.audio), NO_MODIFIER)
        target = h.point(T, second)
        h.interaction.drag(target, NO_MODIFIER)
        h.interaction.release(target, NO_MODIFIER)
        assert h.commands[0].to_track_id == second.id
        assert h.commands[0].from_track_id == h.audio.id
        assert h.commands[0].clip_id == clip.id

    def test_a_drag_snaps_to_another_clip_edge(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        place(h.video, 10 * T, 2 * T)
        h.rebuild()
        # Grabbed in the middle, so the pointer runs one second ahead of the
        # clip's start. Aim that start two pixels past the neighbour's end.
        target_x = h.scene.ticks_to_x(12 * T + T) + 2
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(h.at_x(target_x, h.video), NO_MODIFIER)
        h.interaction.release(h.at_x(target_x, h.video), NO_MODIFIER)
        assert h.commands[0].new_timeline_start == 12 * T

    def test_alt_turns_snapping_off(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        place(h.video, 10 * T, 2 * T)
        h.rebuild()
        target_x = h.scene.ticks_to_x(12 * T + T) + 2
        alt = Qt.KeyboardModifier.AltModifier
        h.interaction.press(h.point(T, h.video), alt)
        h.interaction.drag(h.at_x(target_x, h.video), alt)
        h.interaction.release(h.at_x(target_x, h.video), alt)
        assert h.commands[0].new_timeline_start != 12 * T

    def test_a_clip_cannot_be_dragged_before_zero(self, h: Harness) -> None:
        place(h.video, 4 * T, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(5 * T, h.video), NO_MODIFIER)
        h.interaction.drag(QPointF(-500, h.lane_y(h.video)), NO_MODIFIER)
        h.interaction.release(QPointF(-500, h.lane_y(h.video)), NO_MODIFIER)
        assert h.commands[0].new_timeline_start == 0

    def test_escape_abandons_the_drag(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        h.rebuild()
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(h.point(9 * T, h.video), NO_MODIFIER)
        h.interaction.cancel()
        h.interaction.release(h.point(9 * T, h.video), NO_MODIFIER)
        assert h.commands == []
        assert spans(h.video) == [(0, 2 * T)]


class TestTrim:
    def test_the_right_handle_emits_one_trim_on_release(self, h: Harness) -> None:
        clip = place(h.video, 0, 4 * T)
        h.rebuild()
        edge = h.at_x(h.scene.ticks_to_x(4 * T) - 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        for ticks in (3 * T, 2 * T, T):
            h.interaction.drag(h.point(ticks, h.video), NO_MODIFIER)
        assert h.commands == []
        h.interaction.release(h.point(2 * T, h.video), NO_MODIFIER)
        assert len(h.commands) == 1
        command = h.commands[0]
        assert isinstance(command, TrimClip) and command.clip_id == clip.id
        # The release position wins, not the last position dragged through.
        assert command.new_src_out == 2 * T
        assert command.new_timeline_start == 0

    def test_the_left_handle_moves_src_in_and_start_together(self, h: Harness) -> None:
        place(h.video, 2 * T, 4 * T, src_in=2 * T)
        h.rebuild()
        edge = h.at_x(h.scene.ticks_to_x(2 * T) + 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        h.interaction.drag(h.point(3 * T, h.video), NO_MODIFIER)
        h.interaction.release(h.point(3 * T, h.video), NO_MODIFIER)
        command = h.commands[0]
        assert command.new_timeline_start == 3 * T
        assert command.new_src_in == 3 * T
        assert command.new_src_out == 6 * T

    def test_a_trim_that_changes_nothing_emits_nothing(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        edge = h.at_x(h.scene.ticks_to_x(4 * T) - 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        h.interaction.release(edge, NO_MODIFIER)
        assert h.commands == []

    def test_the_tail_stops_at_the_next_clip(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        place(h.video, 4 * T, 2 * T)
        h.rebuild()
        edge = h.at_x(h.scene.ticks_to_x(2 * T) - 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        far = h.point(9 * T, h.video)
        h.interaction.drag(far, NO_MODIFIER)
        h.interaction.release(far, NO_MODIFIER)
        assert h.commands[0].new_src_out == 4 * T

    def test_the_tail_stops_at_the_end_of_the_source(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        h.rebuild()
        h.interaction.media_duration = lambda src: 3 * T
        edge = h.at_x(h.scene.ticks_to_x(2 * T) - 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        far = h.point(9 * T, h.video)
        h.interaction.drag(far, NO_MODIFIER)
        h.interaction.release(far, NO_MODIFIER)
        assert h.commands[0].new_src_out == 3 * T

    def test_the_head_stops_at_the_previous_clip(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        place(h.video, 4 * T, 2 * T, src_in=4 * T)
        h.rebuild()
        edge = h.at_x(h.scene.ticks_to_x(4 * T) + 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        h.interaction.drag(h.point(0, h.video), NO_MODIFIER)
        h.interaction.release(h.point(0, h.video), NO_MODIFIER)
        assert h.commands[0].new_timeline_start == 2 * T

    def test_a_trim_never_goes_below_one_frame(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        edge = h.at_x(h.scene.ticks_to_x(4 * T) - 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        h.interaction.drag(QPointF(-100, h.lane_y(h.video)), NO_MODIFIER)
        h.interaction.release(QPointF(-100, h.lane_y(h.video)), NO_MODIFIER)
        assert h.commands[0].new_src_out == T // 30

    def test_the_resulting_command_applies_cleanly(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        edge = h.at_x(h.scene.ticks_to_x(4 * T) - 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        h.interaction.drag(h.point(2 * T, h.video), NO_MODIFIER)
        h.interaction.release(h.point(2 * T, h.video), NO_MODIFIER)
        h.apply()
        assert spans(h.video) == [(0, 2 * T)]


class TestClickIsNotADrag:
    """A click near an edge must not nudge the clip by a pixel."""

    def test_a_click_on_a_trim_handle_emits_nothing(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        edge = h.at_x(h.scene.ticks_to_x(4 * T) - 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        h.interaction.release(edge, NO_MODIFIER)
        assert h.commands == []

    def test_a_click_on_a_clip_body_emits_nothing(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        h.interaction.press(h.point(2 * T, h.video), NO_MODIFIER)
        h.interaction.release(h.point(2 * T, h.video), NO_MODIFIER)
        assert h.commands == []

    def test_a_wobble_under_the_threshold_emits_nothing(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        start = h.point(2 * T, h.video)
        nudge = QPointF(start.x() + DRAG_THRESHOLD_PIXELS - 1, start.y())
        h.interaction.press(start, NO_MODIFIER)
        h.interaction.drag(nudge, NO_MODIFIER)
        h.interaction.release(nudge, NO_MODIFIER)
        assert h.commands == []

    def test_past_the_threshold_it_is_a_drag(self, h: Harness) -> None:
        place(h.video, 0, 4 * T)
        h.rebuild()
        start = h.point(2 * T, h.video)
        far = QPointF(start.x() + 200, start.y())
        h.interaction.press(start, NO_MODIFIER)
        h.interaction.drag(far, NO_MODIFIER)
        h.interaction.release(far, NO_MODIFIER)
        assert len(h.commands) == 1

    def test_a_click_on_the_ruler_still_moves_the_playhead(self, h: Harness) -> None:
        """The threshold gates drags, not the scrub, which acts on the press."""
        h.rebuild()
        h.interaction.press(QPointF(h.scene.ticks_to_x(3 * T), 4), NO_MODIFIER)
        h.interaction.release(QPointF(h.scene.ticks_to_x(3 * T), 4), NO_MODIFIER)
        assert h.scene.playhead_ticks() == 3 * T


class TestPlayhead:
    def test_clicking_the_ruler_moves_it(self, h: Harness) -> None:
        h.rebuild()
        seen: list[int] = []
        h.interaction.playhead_scrubbed.connect(seen.append)
        h.interaction.press(QPointF(h.scene.ticks_to_x(3 * T), 4), NO_MODIFIER)
        assert h.scene.playhead_ticks() == 3 * T
        assert seen == [3 * T]

    def test_dragging_the_ruler_keeps_moving_it(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.press(QPointF(h.scene.ticks_to_x(T), 4), NO_MODIFIER)
        h.interaction.drag(QPointF(h.scene.ticks_to_x(5 * T), 4), NO_MODIFIER)
        h.interaction.release(QPointF(h.scene.ticks_to_x(5 * T), 4), NO_MODIFIER)
        assert h.scene.playhead_ticks() == 5 * T

    def test_it_lands_on_a_frame_boundary(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.press(QPointF(123.4, 4), NO_MODIFIER)
        assert h.scene.playhead_ticks() % (T // 30) == 0

    def test_it_never_goes_negative(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.press(QPointF(-200, 4), NO_MODIFIER)
        assert h.scene.playhead_ticks() == 0

    def test_the_playhead_is_a_snap_target_for_a_drag(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        h.rebuild()
        h.scene.set_playhead(7 * T)
        # Grabbed one second in, so aim the pointer one second past the target.
        near = h.at_x(h.scene.ticks_to_x(8 * T) - 3, h.video)
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(near, NO_MODIFIER)
        h.interaction.release(near, NO_MODIFIER)
        assert h.commands[0].new_timeline_start == 7 * T


class TestDropFromTheBin:
    PAYLOAD = {
        "path": str(SRC),
        "duration_ticks": 3 * T,
        "has_video": True,
        "has_audio": True,
    }
    AUDIO_ONLY = {
        "path": "music.wav",
        "duration_ticks": 5 * T,
        "has_video": False,
        "has_audio": True,
    }

    def test_a_drop_inserts_a_full_length_clip(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.begin_media_drag(dict(self.PAYLOAD))
        assert h.interaction.update_media_drag(h.point(4 * T, h.video), NO_MODIFIER)
        assert h.interaction.drop_media()
        command = h.commands[0]
        assert isinstance(command, AddClipFromMedia)
        assert (command.src_in, command.src_out) == (0, 3 * T)
        assert command.timeline_start == 4 * T
        assert command.track_id == h.video.id

    def test_video_onto_an_audio_lane_is_rejected(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.begin_media_drag(dict(self.PAYLOAD))
        assert not h.interaction.update_media_drag(h.point(T, h.audio), NO_MODIFIER)
        assert not h.interaction.drop_media()
        assert h.commands == []
        assert "video file onto the audio track" in h.rejections[0]

    def test_audio_onto_a_video_lane_is_rejected(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.begin_media_drag(dict(self.AUDIO_ONLY))
        assert not h.interaction.update_media_drag(h.point(T, h.video), NO_MODIFIER)
        assert not h.interaction.drop_media()
        assert "audio-only file onto the video track" in h.rejections[0]

    def test_audio_onto_an_audio_lane_is_accepted(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.begin_media_drag(dict(self.AUDIO_ONLY))
        assert h.interaction.update_media_drag(h.point(T, h.audio), NO_MODIFIER)
        assert h.interaction.drop_media()
        assert h.commands[0].track_id == h.audio.id

    def test_an_overlapping_drop_is_rejected(self, h: Harness) -> None:
        place(h.video, 0, 5 * T)
        h.rebuild()
        h.interaction.begin_media_drag(dict(self.PAYLOAD))
        assert not h.interaction.update_media_drag(h.point(2 * T, h.video), NO_MODIFIER)
        assert not h.interaction.drop_media()
        assert "overlap" in h.rejections[0]

    def test_a_drop_snaps_like_a_move(self, h: Harness) -> None:
        place(h.video, 0, 2 * T)
        h.rebuild()
        near = h.at_x(h.scene.ticks_to_x(2 * T) + 3, h.video)
        h.interaction.begin_media_drag(dict(self.PAYLOAD))
        h.interaction.update_media_drag(near, NO_MODIFIER)
        h.interaction.drop_media()
        assert h.commands[0].timeline_start == 2 * T

    def test_a_drop_outside_every_lane_is_rejected(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.begin_media_drag(dict(self.PAYLOAD))
        assert not h.interaction.update_media_drag(QPointF(100, 5), NO_MODIFIER)
        assert not h.interaction.drop_media()
        assert h.rejections == ["Drop a clip onto a track"]

    def test_leaving_the_view_takes_the_ghost_with_it(self, h: Harness) -> None:
        h.rebuild()
        h.interaction.begin_media_drag(dict(self.PAYLOAD))
        h.interaction.update_media_drag(h.point(T, h.video), NO_MODIFIER)
        assert h.interaction._ghost.isVisible()
        h.interaction.end_media_drag()
        assert not h.interaction._ghost.isVisible()

    def test_the_mime_type_is_the_one_the_bin_publishes(self) -> None:
        from ui.media_bin import MEDIA_MIME as bin_mime

        assert bin_mime == MEDIA_MIME


class TestNoGestureCanOverlap:
    """Every rejection path, checked against the model afterwards."""

    def test_the_track_is_still_valid_after_a_refused_gesture(
        self, h: Harness
    ) -> None:
        place(h.video, 0, 2 * T)
        place(h.video, 3 * T, 2 * T)
        h.rebuild()

        # Drag the first onto the second.
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(h.point(4 * T, h.video), NO_MODIFIER)
        h.interaction.release(h.point(4 * T, h.video), NO_MODIFIER)
        # Trim the first over the second. This one is not refused: the handle
        # is clamped at the neighbour, which is the other half of the promise.
        edge = h.at_x(h.scene.ticks_to_x(2 * T) - 1, h.video)
        h.interaction.press(edge, NO_MODIFIER)
        h.interaction.drag(h.point(5 * T, h.video), NO_MODIFIER)
        h.interaction.release(h.point(5 * T, h.video), NO_MODIFIER)
        h.apply()

        assert spans(h.video) == [(0, 3 * T), (3 * T, 5 * T)]
        Track.model_validate(h.video.model_dump())


class TestSnapToleranceScalesWithZoom:
    """Eight pixels means eight pixels, whatever a pixel is worth in ticks.

    One clip and the playhead, so there is exactly one target: at 0.5 px/s
    every edge in a busier project would be inside the tolerance at once and
    the nearest would win, which measures proximity rather than tolerance.
    """

    @staticmethod
    def drag_to(h: Harness, gap_pixels: float) -> int:
        clip = place(h.video, 0, 2 * T)
        h.rebuild()
        h.scene.set_playhead(60 * T)
        # Grabbed at its middle, so the pointer leads the start by a second.
        x = h.scene.ticks_to_x(61 * T) - gap_pixels
        h.interaction.press(h.point(T, h.video), NO_MODIFIER)
        h.interaction.drag(h.at_x(x, h.video), NO_MODIFIER)
        h.interaction.release(h.at_x(x, h.video), NO_MODIFIER)
        assert h.commands, "the drag produced nothing"
        assert h.commands[0].clip_id == clip.id
        return h.commands[0].new_timeline_start

    @pytest.mark.parametrize("pps", [0.5, 5.0, 40.0, 200.0])
    def test_just_inside_the_tolerance_snaps(
        self, qapp: QApplication, pps: float
    ) -> None:
        assert self.drag_to(Harness(pps=pps), SNAP_PIXELS - 1) == 60 * T

    @pytest.mark.parametrize("pps", [0.5, 5.0, 40.0, 200.0])
    def test_just_outside_the_tolerance_does_not(
        self, qapp: QApplication, pps: float
    ) -> None:
        assert self.drag_to(Harness(pps=pps), SNAP_PIXELS + 2) != 60 * T
