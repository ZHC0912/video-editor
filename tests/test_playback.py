"""Timeline playback: the clock, the double buffer, and the volume slider.

The clock is driven directly rather than through real playback. A test that
waited on a QMediaPlayer would be measuring the backend, and would be slow and
flaky besides. What is checked here is the resolution order, what happens at a
cut, and that nothing corrects the audio.
"""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.model import Clip, Project, Track  # noqa: E402
from core.timebase import TICKS_PER_SECOND as T  # noqa: E402
from core.timebase import FrameRate  # noqa: E402
from ui.playback_controller import (  # noqa: E402
    BED_READY,
    BED_RENDERING,
    BED_UNAVAILABLE,
    DRIFT_TOLERANCE_MS,
    TICK_MS,
    PlaybackController,
)
from ui.preview_panel import PreviewController, PreviewPanel  # noqa: E402
from ui.video_stage import VideoStage  # noqa: E402

VIDEO_SRC = Path("shot.mp4")
OTHER_SRC = Path("other.mp4")
MUSIC = Path("music.wav")


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeBedPlayer(QMediaPlayer):
    """A bed player whose position is set by the test, not by a decoder.

    Subclassing rather than mocking keeps the controller talking to the real
    API surface, so a signature that drifts still fails here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.output = QAudioOutput()
        self.setAudioOutput(self.output)
        self._pos = 0
        self.sources: list[str] = []
        self.commands: list[str] = []

    def position(self) -> int:  # type: ignore[override]
        return self._pos

    def setPosition(self, ms: int) -> None:  # type: ignore[override]
        self._pos = max(0, int(ms))

    def setSource(self, url: QUrl) -> None:  # type: ignore[override]
        self.sources.append(url.fileName())

    def play(self) -> None:  # type: ignore[override]
        self.commands.append("play")

    def pause(self) -> None:  # type: ignore[override]
        self.commands.append("pause")

    def stop(self) -> None:  # type: ignore[override]
        self.commands.append("stop")


def make_project(clips: int = 2, audio: bool = True, gap: bool = False) -> Project:
    """``clips`` video clips of two seconds each, back to back unless ``gap``."""
    video = Track(name="V1", kind="video")
    step = 3 * T if gap else 2 * T
    for index in range(clips):
        video.clips.append(
            Clip(
                src=VIDEO_SRC if index % 2 == 0 else OTHER_SRC,
                src_in=0,
                src_out=2 * T,
                timeline_start=index * step,
            )
        )
    tracks = [video]
    if audio:
        bed = Track(name="A1", kind="audio")
        bed.clips.append(
            Clip(src=MUSIC, src_in=0, src_out=max(1, clips) * step, timeline_start=0)
        )
        tracks.append(bed)
    return Project(name="p", frame_rate=FrameRate(30, 1), tracks=tracks)


class Harness:
    def __init__(self, project: Project | None = None) -> None:
        self.panel = PreviewPanel()
        self.stage = self.panel.stage
        self.bed = FakeBedPlayer()
        self.controller = PlaybackController(project, self.stage, self.bed)
        self.panel.set_controller(self.controller)
        self.project = project
        if project is not None:
            # set_controller already pushed the panel's own project, which is
            # None at this point. Give the controller the real one.
            self.controller.set_project(project)
        self.positions: list[int] = []
        self.states: list[str] = []
        self.finished: list[int] = []
        self.controller.position_changed.connect(self.positions.append)
        self.controller.bed_state_changed.connect(self.states.append)
        self.controller.playback_finished.connect(lambda: self.finished.append(1))

    def give_bed(self, path: Path = Path("bed.wav")) -> None:
        self.controller.set_bed(path)

    def bed_at(self, ticks: int) -> None:
        """Move the bed clock, then let the controller read it."""
        self.bed.setPosition(round(ticks / T * 1000))
        self.controller._tick()

    def close(self) -> None:
        self.controller.shutdown()
        self.panel.deleteLater()


@pytest.fixture
def h(qapp: QApplication) -> Harness:
    harness = Harness(make_project())
    yield harness
    harness.close()


class TestTheSeamHeld:
    """The Protocol is the contract between the panel and whatever plays.

    The entire controller behind it was replaced once without touching the
    panel. It has been widened once, deliberately, by the three scrub methods:
    a scrub is a three phase gesture, and seek() alone could not say where one
    begins or ends.
    """

    def test_the_controller_satisfies_the_protocol(self, h: Harness) -> None:
        assert isinstance(h.controller, PreviewController)

    def test_the_protocol_carries_all_four_ways_to_move(self, h: Harness) -> None:
        # runtime_checkable isinstance only looks for the names, so name them.
        for method in ("seek", "begin_scrub", "scrub_to", "end_scrub"):
            assert hasattr(PreviewController, method), method
            assert callable(getattr(h.controller, method)), method

    def test_the_panel_reaches_the_controller_by_all_four(
        self, h: Harness
    ) -> None:
        """Every one of them has a caller in the panel, or it is decoration."""
        source = Path("ui/preview_panel.py").read_text(encoding="utf-8")
        for method in ("seek", "begin_scrub", "scrub_to", "end_scrub"):
            assert f"self._controller.{method}(" in source, method

    def test_the_panel_accepts_it_without_special_casing(self, h: Harness) -> None:
        assert h.panel.controller() is h.controller


class TestClockResolution:
    def test_a_valid_bed_is_the_clock(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(1500 * T // 1000)
        assert h.controller.position_ticks() == 1500 * T // 1000

    def test_the_bed_position_is_read_never_accumulated(self, h: Harness) -> None:
        """The bed jumping backwards must take the timeline with it."""
        h.give_bed()
        h.controller.play()
        h.bed_at(3 * T)
        h.bed_at(1 * T)
        assert h.controller.position_ticks() == 1 * T

    def test_a_project_with_no_audio_runs_on_the_elapsed_timer(
        self, qapp: QApplication
    ) -> None:
        harness = Harness(make_project(audio=False))
        try:
            harness.controller.play()
            assert harness.controller.bed_state() == BED_UNAVAILABLE
            start = harness.controller.position_ticks()
            time.sleep(0.15)
            harness.controller._tick()
            moved = harness.controller.position_ticks() - start
            # Real elapsed time, not a per-tick increment.
            assert 0.08 * T < moved < 0.4 * T
        finally:
            harness.close()

    def test_playback_does_not_wait_for_a_bed(self, h: Harness) -> None:
        """The bed is mid-render. Play anyway, on the timer."""
        h.controller.invalidate_bed()
        assert h.controller.bed_state() == BED_RENDERING
        h.controller.play()
        assert h.controller.is_playing() is True
        time.sleep(0.12)
        h.controller._tick()
        assert h.controller.position_ticks() > 0

    def test_the_bed_takes_over_the_moment_it_lands(self, h: Harness) -> None:
        h.controller.invalidate_bed()
        h.controller.play()
        time.sleep(0.1)
        h.controller._tick()
        h.give_bed()
        assert h.controller.bed_state() == BED_READY
        h.bed_at(3 * T)
        assert h.controller.position_ticks() == 3 * T

    def test_a_paused_controller_reports_where_it_was_put(self, h: Harness) -> None:
        h.give_bed()
        h.controller.seek(2 * T)
        assert h.controller.is_playing() is False
        assert h.controller.position_ticks() == 2 * T
        h.controller._tick()
        assert h.controller.position_ticks() == 2 * T

    def test_handing_the_clock_back_to_the_timer_does_not_jump(
        self, h: Harness
    ) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(3 * T)
        h.controller.invalidate_bed()
        h.controller._tick()
        # Continues from where the bed had reached, not from zero.
        assert 3 * T <= h.controller.position_ticks() < 3 * T + T // 2


class TestBedState:
    def test_the_states_map_to_the_status_line(self, h: Harness) -> None:
        h.give_bed()
        assert h.panel._status.text() == ""
        h.controller.invalidate_bed()
        assert h.panel._status.text() == "Rendering audio preview"
        h.controller.set_bed(None)
        assert h.panel._status.text() == "No audio in project"

    def test_invalidating_with_no_audio_is_unavailable_not_rendering(
        self, qapp: QApplication
    ) -> None:
        harness = Harness(make_project(audio=False))
        try:
            harness.controller.invalidate_bed()
            assert harness.controller.bed_state() == BED_UNAVAILABLE
        finally:
            harness.close()

    def test_the_state_signal_only_fires_on_a_change(self, h: Harness) -> None:
        h.states.clear()
        h.give_bed()
        h.give_bed()
        assert h.states == [BED_READY]


class TestVideoFollowsTheClock:
    def test_it_enters_the_clip_under_the_playhead(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T)
        assert h.controller._current_clip_id == h.project.tracks[0].clips[0].id

    def test_a_gap_shows_black(self, qapp: QApplication) -> None:
        harness = Harness(make_project(gap=True))
        try:
            harness.give_bed()
            harness.controller.play()
            harness.bed_at(2 * T + T // 2)  # between the clips
            assert harness.controller._current_clip_id is None
            assert harness.stage.is_black() is True
        finally:
            harness.close()

    def test_a_project_with_no_video_track_stays_black(
        self, qapp: QApplication
    ) -> None:
        audio_only = Project(
            name="p",
            tracks=[
                Track(
                    name="A1",
                    kind="audio",
                    clips=[Clip(src=MUSIC, src_in=0, src_out=4 * T, timeline_start=0)],
                )
            ],
        )
        harness = Harness(audio_only)
        try:
            harness.give_bed()
            harness.controller.play()
            harness.bed_at(T)
            assert harness.stage.is_black() is True
            assert harness.controller.position_ticks() == T
        finally:
            harness.close()

    def test_the_next_clip_is_preloaded_on_entering_this_one(
        self, h: Harness
    ) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T // 2)
        clips = h.project.tracks[0].clips
        assert h.controller._preloaded_clip_id == clips[1].id

    def test_a_split_preloads_the_same_source_into_standby(
        self, qapp: QApplication
    ) -> None:
        """Two players open on one file is fine and keeps the cut fast."""
        project = make_project(clips=2)
        for clip in project.tracks[0].clips:
            clip.src = VIDEO_SRC
        harness = Harness(project)
        try:
            harness.give_bed()
            harness.controller.play()
            harness.bed_at(T // 2)
            assert harness.controller._preloaded_clip_id == project.tracks[0].clips[1].id
        finally:
            harness.close()

    def test_the_cut_uses_the_preloaded_player(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T // 2)
        before = h.stage.active_player()
        h.controller._preloaded_clip_id = h.project.tracks[0].clips[1].id
        h.bed_at(2 * T + 100)
        # present_standby swapped the roles rather than loading into the
        # player that was already showing.
        assert h.stage.active_player() is not before
        assert h.controller._current_clip_id == h.project.tracks[0].clips[1].id

    def test_a_cut_corrects_a_standby_that_had_not_settled(
        self, h: Harness
    ) -> None:
        """A preload that had not finished seeking is still presented, but it
        is put on the right frame at once rather than one tick later."""
        h.give_bed()
        h.controller.play()
        h.bed_at(T // 2)
        h.controller._preloaded_clip_id = h.project.tracks[0].clips[1].id
        standby = h.stage.standby_player()
        seen: list[int] = []
        standby.position = lambda: 9999      # nowhere near the in-frame
        standby.setPosition = seen.append
        h.bed_at(2 * T + 100)
        # 100 ticks into the clip, which is under a millisecond.
        assert seen == [1], "the cut left the video parked on the wrong frame"

    def test_a_cut_with_nothing_preloaded_falls_back_to_a_hard_load(
        self, h: Harness
    ) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T // 2)
        before = h.stage.active_player()
        h.controller._preloaded_clip_id = None
        h.bed_at(2 * T + 100)
        assert h.stage.active_player() is before
        assert h.controller._current_clip_id == h.project.tracks[0].clips[1].id


class TestDriftCheck:
    @staticmethod
    def corrections(h: Harness, player_ms: int, timeline_ticks: int) -> list[int]:
        """Put the video player at ``player_ms``, the timeline at
        ``timeline_ticks``, and report what the drift check did about it."""
        h.give_bed()
        h.controller.play()
        h.bed_at(T)
        player = h.stage.active_player()
        seen: list[int] = []
        # The offscreen player never decodes anything, so its own position
        # would sit at zero and every position past the tolerance would look
        # like drift. Both ends of the comparison are set here instead.
        player.position = lambda: player_ms
        player.setPosition = seen.append
        h.bed_at(timeline_ticks)
        return seen

    def test_a_small_offset_is_left_alone(self, h: Harness) -> None:
        # 1010ms wanted, 1000ms reported: 10ms out, well inside 250.
        assert self.corrections(h, 1000, T + T // 100) == []

    def test_just_inside_the_tolerance_is_left_alone(self, h: Harness) -> None:
        wanted = T + (DRIFT_TOLERANCE_MS - 10) * T // 1000
        assert self.corrections(h, 1000, wanted) == []

    def test_a_large_offset_is_corrected(self, h: Harness) -> None:
        # 1500ms wanted, 1000ms reported: half a second out.
        assert self.corrections(h, 1000, 3 * T // 2) == [1500]

    def test_the_correction_goes_to_the_source_position(self, h: Harness) -> None:
        """src_in offsets the clip, so the player position is not the
        timeline position."""
        h.project.tracks[0].clips[0].src_in = 5 * T
        h.project.tracks[0].clips[0].src_out = 7 * T
        assert self.corrections(h, 0, 3 * T // 2) == [6500]

    def test_the_tolerance_is_the_documented_one(self) -> None:
        assert DRIFT_TOLERANCE_MS == 250

    def test_the_audio_is_never_corrected(self, h: Harness) -> None:
        """The whole design in one assertion."""
        h.give_bed()
        h.controller.play()
        h.bed.commands.clear()
        for step in range(1, 40):
            h.bed_at(step * T // 10)
        # No setPosition on the bed, no stop, no restart. Only what play() did.
        assert h.bed.commands == []
        assert h.bed.position() == round(39 * 100)


class TestSeeking:
    def test_it_moves_the_bed_and_forces_a_full_resolve(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T // 2)
        h.controller.seek(2 * T + T // 2)
        assert h.bed.position() == 2500
        assert h.controller._current_clip_id == h.project.tracks[0].clips[1].id

    def test_seeking_lands_in_the_right_clip_during_playback(
        self, qapp: QApplication
    ) -> None:
        project = make_project(clips=4)
        harness = Harness(project)
        try:
            harness.give_bed()
            harness.controller.play()
            clips = project.tracks[0].clips
            for index, clip in enumerate(clips):
                harness.controller.seek(clip.timeline_start + T // 2)
                assert harness.controller._current_clip_id == clip.id, index
            assert harness.controller.is_playing() is True
        finally:
            harness.close()

    def test_it_clamps_to_the_project(self, h: Harness) -> None:
        h.controller.seek(999 * T)
        assert h.controller.position_ticks() == h.project.duration
        h.controller.seek(-5 * T)
        assert h.controller.position_ticks() == 0

    def test_seeking_while_paused_does_not_start_playing(self, h: Harness) -> None:
        h.give_bed()
        h.controller.seek(T)
        assert h.controller.is_playing() is False
        assert "play" not in h.bed.commands


class TestScrub:
    """Grabbing the playhead is a take-manual-control gesture.

    Playback stops on the press and stays stopped on release. The bed is left
    paused and untouched for the whole drag, and aligned exactly once at the
    end, so a subsequent Space resumes in sync.
    """

    def test_scrubbing_while_playing_leaves_it_paused(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T // 2)
        assert h.controller.is_playing() is True

        h.controller.begin_scrub()
        assert h.controller.is_playing() is False
        for ticks in (T, 2 * T, 3 * T):
            h.controller.scrub_to(ticks)
        h.controller.end_scrub(3 * T)

        assert h.controller.is_playing() is False
        assert h.controller.is_scrubbing() is False

    def test_the_bed_is_moved_once_on_release_not_once_per_move(
        self, h: Harness
    ) -> None:
        h.give_bed()
        h.controller.play()
        h.bed.commands.clear()
        seeks: list[int] = []
        real = h.bed.setPosition
        h.bed.setPosition = lambda ms: (seeks.append(ms), real(ms))[1]

        h.controller.begin_scrub()
        for step in range(40):
            h.controller.scrub_to(step * T // 10)
        assert seeks == [], "the bed was moved during the drag"

        h.controller.end_scrub(39 * T // 10)
        assert len(seeks) == 1, f"{len(seeks)} bed seeks for one gesture"
        assert seeks[0] == 3900

    def test_the_bed_is_paused_for_the_whole_drag(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed.commands.clear()
        h.controller.begin_scrub()
        assert h.bed.commands == ["pause"]
        for step in range(20):
            h.controller.scrub_to(step * T // 5)
        h.controller.end_scrub(4 * T // 5)
        # Nothing restarted it. Scrub audio is out of scope for v1.
        assert h.bed.commands == ["pause"]

    def test_space_after_a_scrub_resumes_from_there(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T // 2)
        h.controller.begin_scrub()
        h.controller.scrub_to(3 * T)
        h.controller.end_scrub(3 * T)

        assert h.bed.position() == 3000
        h.controller.play()
        assert h.controller.is_playing() is True
        assert h.controller.position_ticks() == 3 * T
        assert h.bed.position() == 3000

    def test_scrubbing_while_paused_does_not_start_playback(
        self, h: Harness
    ) -> None:
        h.give_bed()
        assert h.controller.is_playing() is False
        h.controller.begin_scrub()
        h.controller.scrub_to(2 * T)
        h.controller.end_scrub(2 * T)
        assert h.controller.is_playing() is False
        assert "play" not in h.bed.commands

    def test_the_stage_follows_the_scrub_across_clips(self, h: Harness) -> None:
        clips = h.project.tracks[0].clips
        h.give_bed()
        h.controller.play()
        h.controller.begin_scrub()
        for ticks, expected in (
            (T // 2, clips[0]),
            (3 * T, clips[1]),
            (T, clips[0]),
            (2 * T, clips[1]),
        ):
            h.controller.scrub_to(ticks)
            assert h.controller._current_clip_id == expected.id, ticks
            assert h.stage.is_black() is False
        h.controller.end_scrub(2 * T)

    def test_the_scrub_places_the_frame_exactly(self, h: Harness) -> None:
        """Not through the loose drift threshold: the user picked this frame."""
        h.give_bed()
        h.controller.begin_scrub()
        h.controller.scrub_to(T // 2)
        player = h.stage.active_player()
        seen: list[int] = []
        player.position = lambda: 500          # already within the tolerance
        player.setPosition = seen.append
        h.controller.scrub_to(T // 2 + T // 100)
        assert seen == [510], "the frame was left to drift correction"

    def test_a_scrub_inside_one_clip_does_not_reload_the_source(
        self, h: Harness
    ) -> None:
        """The bug this refactor removed: a setSource per mouse-move."""
        h.give_bed()
        h.controller.begin_scrub()
        h.controller.scrub_to(T // 4)
        player = h.stage.active_player()
        sources: list[str] = []
        player.setSource = lambda url: sources.append(url.fileName())
        for step in range(30):
            h.controller.scrub_to(T // 4 + step * T // 100)
        assert sources == [], f"{len(sources)} reloads during one scrub"

    def test_the_parked_last_frame_rule_applies_mid_scrub(
        self, h: Harness
    ) -> None:
        h.give_bed()
        h.controller.begin_scrub()
        h.controller.scrub_to(h.project.duration)
        assert h.controller.position_ticks() == h.project.duration
        assert h.controller._current_clip_id == h.project.tracks[0].clips[-1].id
        assert h.stage.is_black() is False
        h.controller.end_scrub(h.project.duration)
        assert h.stage.is_black() is False

    def test_a_scrub_into_a_gap_shows_black(self, qapp: QApplication) -> None:
        harness = Harness(make_project(gap=True))
        try:
            harness.give_bed()
            harness.controller.begin_scrub()
            harness.controller.scrub_to(T)
            assert harness.stage.is_black() is False
            harness.controller.scrub_to(2 * T + T // 2)
            assert harness.stage.is_black() is True
            harness.controller.end_scrub(2 * T + T // 2)
        finally:
            harness.close()


class TestReloadingTheSameSource:
    """Every clip of a split is the same file, so this is the ordinary case."""

    def test_a_hard_load_onto_the_open_source_still_seeks(
        self, h: Harness
    ) -> None:
        """Qt does not re-emit LoadedMedia for a source already open, so a
        load that only called setSource would drop its seek and leave the
        picture on the previous frame."""
        stage = h.stage
        player = stage.active_player()
        stage.load_active(VIDEO_SRC, 1000)

        seen: list[int] = []
        player.setPosition = seen.append
        # Same file, different position: the exact case a backwards scrub
        # across a split produces.
        stage.load_active(VIDEO_SRC, 4000)
        assert seen == [4000], "the pending seek was dropped"

    def test_a_different_source_is_loaded_normally(self, h: Harness) -> None:
        stage = h.stage
        stage.load_active(VIDEO_SRC, 0)
        player = stage.active_player()
        sources: list[str] = []
        player.setSource = lambda url: sources.append(url.fileName())
        stage.load_active(OTHER_SRC, 2000)
        assert sources == [OTHER_SRC.name]

    def test_scrubbing_backwards_across_a_split_lands_on_the_right_frame(
        self, qapp: QApplication
    ) -> None:
        project = make_project(clips=3)
        for clip in project.tracks[0].clips:
            clip.src = VIDEO_SRC          # one file, three clips: a split
        harness = Harness(project)
        try:
            harness.give_bed()
            harness.controller.begin_scrub()
            harness.controller.scrub_to(5 * T)      # the last clip
            player = harness.stage.active_player()
            seen: list[int] = []
            player.setPosition = seen.append
            harness.controller.scrub_to(T)          # back to the first
            assert seen and seen[-1] == 1000
        finally:
            harness.close()


class TestTransportIsUnchangedByMoving:
    """Requirement: stepping, Home and End move, and nothing else."""

    def test_a_frame_step_while_playing_keeps_playing(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T)
        h.panel.step_forward()
        assert h.controller.is_playing() is True

    def test_a_frame_step_while_paused_stays_paused(self, h: Harness) -> None:
        h.give_bed()
        h.controller.seek(T)
        h.panel.step_forward()
        assert h.controller.is_playing() is False

    def test_home_and_end_do_not_change_the_transport(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T)
        h.panel.go_to_start()
        assert h.controller.is_playing() is True
        h.controller.pause()
        h.panel.go_to_end()
        assert h.controller.is_playing() is False

    def test_seek_does_not_touch_the_scrub_state(self, h: Harness) -> None:
        h.give_bed()
        h.controller.seek(T)
        assert h.controller.is_scrubbing() is False


class TestEnd:
    """Playing to the end parks on the last frame.

    clip_at is half open, so nothing matches at exactly project.duration and a
    literal resolve there blacks the stage the moment playback finishes. The
    reported position still has to be the true end: only the picture is
    resolved one frame back.
    """

    @staticmethod
    def last_frame_start(project: Project) -> int:
        rate = project.frame_rate
        from core.timebase import frames_to_ticks, ticks_to_frames

        return frames_to_ticks(ticks_to_frames(project.duration - 1, rate), rate)

    def test_reaching_the_end_pauses_and_reports(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(h.project.duration + T)
        assert h.controller.is_playing() is False
        assert h.finished == [1]
        assert h.controller.position_ticks() == h.project.duration

    def test_playing_to_the_end_leaves_the_last_frame_on_the_stage(
        self, h: Harness
    ) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(h.project.duration + T)
        last = h.project.tracks[0].clips[-1]
        assert h.controller._current_clip_id == last.id
        assert h.stage.is_black() is False

    def test_the_reported_position_is_exactly_the_duration(
        self, h: Harness
    ) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(h.project.duration + 5 * T)
        assert h.controller.position_ticks() == h.project.duration
        assert h.positions[-1] == h.project.duration
        assert h.panel.position_ticks() == h.project.duration

    def test_the_timecode_reads_the_true_end(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(h.project.duration + T)
        assert h.panel._timecode.text() == h.panel._duration_label.text()

    def test_the_video_is_parked_on_the_last_frame_of_the_source(
        self, h: Harness
    ) -> None:
        h.give_bed()
        h.controller.play()
        h.bed_at(T // 2)
        h.controller._preloaded_clip_id = h.project.tracks[0].clips[1].id
        player = h.stage.standby_player()
        seen: list[int] = []
        player.position = lambda: 0
        player.setPosition = seen.append
        h.bed_at(h.project.duration + T)

        last = h.project.tracks[0].clips[-1]
        frame_start = self.last_frame_start(h.project)
        expected = round(
            (last.src_in + frame_start - last.timeline_start) / T * 1000
        )
        assert seen and seen[-1] == expected
        # Inside the clip, not past its end.
        assert last.timeline_start <= frame_start < last.timeline_end

    def test_seeking_to_exactly_the_end_parks_the_same_way(
        self, h: Harness
    ) -> None:
        """Not only playback finishing: scrubbing to the end too."""
        h.give_bed()
        h.controller.seek(h.project.duration)
        assert h.controller.position_ticks() == h.project.duration
        assert h.controller._current_clip_id == h.project.tracks[0].clips[-1].id
        assert h.stage.is_black() is False

    def test_the_panel_end_button_parks_the_same_way(self, h: Harness) -> None:
        h.give_bed()
        h.panel.go_to_end()
        assert h.panel.position_ticks() == h.project.duration
        assert h.stage.is_black() is False

    def test_a_timeline_ending_in_a_gap_still_shows_black(
        self, qapp: QApplication
    ) -> None:
        """Black is the right answer when the timeline genuinely ends in
        nothing, and parking one frame back must not invent a frame."""
        project = make_project(gap=True)
        video = project.tracks[0]
        assert video.clips[-1].timeline_end < project.duration, "no trailing gap"
        harness = Harness(project)
        try:
            harness.give_bed()
            harness.controller.play()
            harness.bed_at(project.duration + T)
            assert harness.controller.position_ticks() == project.duration
            assert harness.controller._current_clip_id is None
            assert harness.stage.is_black() is True
        finally:
            harness.close()

    def test_a_project_with_no_video_at_all_stays_black_at_the_end(
        self, qapp: QApplication
    ) -> None:
        audio_only = Project(
            name="p",
            tracks=[
                Track(
                    name="A1",
                    kind="audio",
                    clips=[Clip(src=MUSIC, src_in=0, src_out=4 * T, timeline_start=0)],
                )
            ],
        )
        harness = Harness(audio_only)
        try:
            harness.give_bed()
            harness.controller.play()
            harness.bed_at(5 * T)
            assert harness.stage.is_black() is True
            assert harness.controller.position_ticks() == 4 * T
        finally:
            harness.close()

    def test_it_parks_inside_the_clip_at_a_variable_frame_rate(
        self, qapp: QApplication
    ) -> None:
        """24249/1000, where a frame is not a whole number of ticks.

        Subtracting a frame length would not land on a boundary; converting
        through the frame index does, at any rate.
        """
        rate = FrameRate(24249, 1000)
        from core.timebase import frames_to_ticks, ticks_to_frames

        end = frames_to_ticks(300, rate)
        project = Project(
            name="vfr",
            frame_rate=rate,
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(src=VIDEO_SRC, src_in=0, src_out=end, timeline_start=0)
                    ],
                )
            ],
        )
        harness = Harness(project)
        try:
            harness.controller.seek(project.duration)
            assert harness.controller.position_ticks() == end
            assert harness.controller._current_clip_id == project.tracks[0].clips[0].id

            parked = harness.controller._frame_position(end)
            assert 0 <= parked < end
            # A real frame boundary, and the last one.
            assert parked == frames_to_ticks(ticks_to_frames(parked, rate), rate)
            assert ticks_to_frames(parked, rate) == ticks_to_frames(end - 1, rate)
        finally:
            harness.close()

    def test_a_one_frame_clip_still_parks_on_itself(
        self, qapp: QApplication
    ) -> None:
        """The shortest clip a trim can produce must not park before its start."""
        rate = FrameRate(30, 1)
        from core.timebase import frames_to_ticks

        one = frames_to_ticks(1, rate)
        project = Project(
            name="tiny",
            frame_rate=rate,
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(src=VIDEO_SRC, src_in=0, src_out=one, timeline_start=0)
                    ],
                )
            ],
        )
        harness = Harness(project)
        try:
            harness.controller.seek(project.duration)
            assert harness.controller._frame_position(one) == 0
            assert harness.controller._current_clip_id == project.tracks[0].clips[0].id
            assert harness.stage.is_black() is False
        finally:
            harness.close()

    def test_an_empty_project_does_not_reach_back_before_zero(
        self, qapp: QApplication
    ) -> None:
        harness = Harness(Project(name="empty"))
        try:
            harness.controller.seek(0)
            assert harness.controller.position_ticks() == 0
            assert harness.stage.is_black() is True
        finally:
            harness.close()

    def test_playing_from_the_end_restarts(self, h: Harness) -> None:
        h.give_bed()
        h.controller.seek(h.project.duration)
        h.controller.play()
        assert h.controller.position_ticks() == 0

    def test_seeking_to_the_end_does_not_report_finished(self, h: Harness) -> None:
        """Only playback reaching the end is the end of playback."""
        h.controller.seek(h.project.duration)
        assert h.finished == []


class TestVolumeIsMonitoringOnly:
    """The slider is a monitoring level and never reaches the model.

    Clip gain and track mutes are baked into the bed by FFmpeg long before a
    player sees it, so changing the volume must not touch gain_db.
    """

    def test_the_slider_changes_the_output_level(self, h: Harness) -> None:
        h.controller.set_volume(50)
        half = h.bed.output.volume()
        h.controller.set_volume(100)
        assert h.bed.output.volume() > half
        h.controller.set_volume(0)
        assert h.bed.output.volume() == pytest.approx(0.0, abs=1e-6)

    def test_it_does_not_mutate_the_model(self, h: Harness) -> None:
        before = h.project.model_dump()
        for value in (0, 1, 37, 80, 99, 100):
            h.controller.set_volume(value)
            h.panel._volume.setValue(value)
        assert h.project.model_dump() == before

    def test_no_clip_gain_is_touched_by_any_volume(self, h: Harness) -> None:
        gains = [
            clip.gain_db for track in h.project.tracks for clip in track.clips
        ]
        h.controller.set_volume(11)
        assert [
            clip.gain_db for track in h.project.tracks for clip in track.clips
        ] == gains

    def test_the_controller_has_no_route_to_a_clip_from_set_volume(self) -> None:
        """A structural check, so a future edit cannot quietly add one."""
        import ast
        import inspect

        source = inspect.getsource(PlaybackController.set_volume)
        tree = ast.parse(source.lstrip())
        names = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert "gain_db" not in names
        assert not names & {"_project", "clips", "tracks"}

    def test_the_slider_is_offered_because_there_is_an_audio_path(
        self, h: Harness
    ) -> None:
        assert h.panel._volume.isEnabled() is True

    def test_a_missing_audio_output_is_survivable(self, qapp: QApplication) -> None:
        stage = VideoStage()
        controller = PlaybackController(None, stage, QMediaPlayer())
        controller.set_volume(50)  # must not raise
        stage.deleteLater()


class TestNoSecondAudioPath:
    """There is one audio output in this application, and there must be.

    All preview sound comes from a single pre-rendered WAV of the whole audio
    timeline, and that file's position is the master clock: while it is
    playing, its position IS the timeline position, and the video is corrected
    to it. A second thing making sound is a second clock. It would drift
    against the first, and there would be no way to say which of the two was
    right, which is the failure the whole design exists to remove.

    The rule is easy to break by accident and silent when broken: the sound
    would still come out, roughly in time, and only a long take would show the
    drift.
    """

    #: The one place a QAudioOutput may be constructed. The window owns it
    #: because the window owns the bed player it belongs to; the playback
    #: controller is handed that player and never makes one of its own.
    BED_OUTPUT_SITE = ("ui/main_window.py", "_bed_output")

    @staticmethod
    def _constructs_audio_output(node: ast.AST) -> bool:
        """Whether an expression is a ``QAudioOutput(...)`` call.

        Both spellings, because either would work: the imported name, and the
        attribute form off the module.
        """
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == "QAudioOutput"
        return isinstance(func, ast.Attribute) and func.attr == "QAudioOutput"

    @classmethod
    def _audio_output_sites(cls) -> list[tuple[str, str]]:
        """Every construction of a QAudioOutput under ui/, with what it is
        assigned to. ``"<unassigned>"`` for one passed straight to a call."""
        sites: list[tuple[str, str]] = []
        for path in sorted(Path("ui").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            assigned: set[int] = set()

            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not cls._constructs_audio_output(node.value):
                    continue
                assigned.add(id(node.value))
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        name = target.attr
                    elif isinstance(target, ast.Name):
                        name = target.id
                    else:
                        name = "<complex target>"
                    sites.append((path.as_posix(), name))

            # Anything constructed and handed straight to something else,
            # which is how this would most plausibly be reintroduced:
            # player.setAudioOutput(QAudioOutput(self)).
            for node in ast.walk(tree):
                if cls._constructs_audio_output(node) and id(node) not in assigned:
                    sites.append((path.as_posix(), "<unassigned>"))

        return sites

    def test_no_second_audio_player_exists(self) -> None:
        """Exactly one QAudioOutput is constructed anywhere under ui/.

        An AST walk rather than a check that some particular file is absent:
        the mistake is building a second audio path, not the name of the
        module it gets built in, and a filename check passes the moment
        somebody recreates it somewhere else.
        """
        sites = self._audio_output_sites()

        assert sites == [self.BED_OUTPUT_SITE], (
            f"expected the bed output and nothing else, found {sites}. "
            f"Preview audio comes from the bed, which is the master clock; a "
            f"second audio output is a second clock."
        )

    def test_neither_video_player_has_an_audio_output(self, h: Harness) -> None:
        assert h.stage.active_player().audioOutput() is None
        assert h.stage.standby_player().audioOutput() is None

    def test_the_video_stage_never_constructs_one(self) -> None:
        """The likeliest place to reintroduce it, so it is named explicitly."""
        assert ("ui/video_stage.py", "<unassigned>") not in self._audio_output_sites()
        source = Path("ui/video_stage.py").read_text(encoding="utf-8")
        assert "setAudioOutput" not in source

    def test_the_controller_never_creates_a_player(self) -> None:
        source = Path("ui/playback_controller.py").read_text(encoding="utf-8")
        assert "QMediaPlayer(" not in source
        assert "QAudioOutput(" not in source


class TestProjectChanges:
    def test_an_edit_updates_the_scrubbers_span(self, h: Harness) -> None:
        h.controller.seek(T)
        track = h.project.tracks[0]
        track.clips.append(
            Clip(src=VIDEO_SRC, src_in=0, src_out=2 * T, timeline_start=20 * T)
        )
        h.controller.project_changed()
        assert h.panel.duration_ticks() == 22 * T

    def test_an_edit_leaves_the_playhead_where_it_was(self, h: Harness) -> None:
        h.controller.seek(3 * T)
        h.controller.project_changed()
        assert h.controller.position_ticks() == 3 * T

    def test_an_edit_that_shortens_the_project_pulls_the_playhead_back(
        self, h: Harness
    ) -> None:
        h.controller.seek(h.project.duration)
        h.project.tracks[0].clips.pop()
        h.controller.project_changed()
        assert h.controller.position_ticks() == h.project.duration

    def test_a_new_project_resets_everything(self, h: Harness) -> None:
        h.give_bed()
        h.controller.play()
        h.controller.set_project(make_project(clips=1))
        assert h.controller.is_playing() is False
        assert h.controller.position_ticks() == 0
        assert h.controller._current_clip_id is None


class TestTheTick:
    def test_the_interval_is_the_documented_one(self) -> None:
        assert TICK_MS == 33

    def test_it_runs_only_while_playing(self, h: Harness) -> None:
        h.give_bed()
        assert h.controller._timer.isActive() is False
        h.controller.play()
        assert h.controller._timer.isActive() is True
        h.controller.pause()
        assert h.controller._timer.isActive() is False

    def test_no_fixed_increment_appears_in_the_source(self) -> None:
        """Position is read from the clock, never advanced by the tick."""
        source = Path("ui/playback_controller.py").read_text(encoding="utf-8")
        for forbidden in ("+= TICK_MS", "_position +=", "position += "):
            assert forbidden not in source, forbidden
