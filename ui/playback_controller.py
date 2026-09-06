"""Timeline playback. The audio bed is the clock; video is corrected to it.

AUDIO IS THE CLOCK
------------------
The pre-rendered bed WAV plays as one continuous file in one QMediaPlayer. Its
reported position IS the timeline position. Nothing in this file corrects the
audio, and nothing may be added that does.

The reason is perceptual, not technical. Humans detect audio discontinuity far
more readily than a video frame held one beat too long. Correcting video
against audio produces errors nobody sees. Correcting audio against video
produces clicks and pitch artefacts everybody hears. So the direction of
correction is fixed: video follows audio, always, and the loose 250ms drift
threshold below is deliberately loose because a video-only correction of that
size is a single held frame.

VIDEO IS DOUBLE BUFFERED
------------------------
The next clip is loaded, seeked and parked on its in-frame in the standby
player while the current one plays. A cut is then a stack index change, not a
load. :class:`~ui.video_stage.VideoStage` owns that machinery; this controller
only decides when to preload and when to present.

WHAT IS NOT HERE, AND MUST NOT BE ADDED
---------------------------------------
No clip-by-clip audio playback. No second audio player. No audio drift
correction of any kind. No fixed per-tick position increment: the position is
READ from the clock on every tick and never accumulated, which is why a
dropped timer tick or a slow frame cannot make playback slide.

CLOCK RESOLUTION, in this order:

    1. a valid bed exists            -> the bed player's position
    2. the project has no audio      -> a QElapsedTimer since play()
    3. the bed is mid-render         -> the same elapsed timer, and the bed
                                        takes over the moment it lands

Playback never blocks waiting for a bed. It degrades to the timer.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudio, QMediaPlayer

from core.model import Project, clip_at, next_clip_after
from core.timebase import (
    frames_to_ticks,
    seconds_to_ticks,
    ticks_to_frames,
    ticks_to_seconds,
)
from ui.video_stage import VideoStage

__all__ = [
    "PlaybackController",
    "TICK_MS",
    "DRIFT_TOLERANCE_MS",
    "BED_READY",
    "BED_RENDERING",
    "BED_UNAVAILABLE",
]

#: The video tick. Roughly 30Hz, and it drives VIDEO ONLY. It never advances
#: the position; it only asks the clock what the position now is.
TICK_MS = 33

#: How far the active video player may sit from where the timeline says it
#: should be before it is nudged. One held frame's worth of correction.
#: Tightening this causes visible thrash at every tick. Do not lower it.
DRIFT_TOLERANCE_MS = 250

BED_READY = "ready"
BED_RENDERING = "rendering"
BED_UNAVAILABLE = "unavailable"

#: What the status line says for each bed state.
_STATUS_TEXT = {
    BED_READY: "",
    BED_RENDERING: "Rendering audio preview",
    BED_UNAVAILABLE: "No audio in project",
}


def _ticks_to_ms(ticks: int) -> int:
    return round(ticks_to_seconds(max(0, int(ticks))) * 1000)


def _ms_to_ticks(ms: int) -> int:
    return seconds_to_ticks(max(0, int(ms)) / 1000)


class PlaybackController(QObject):
    """Plays the timeline through a PreviewPanel.

    Implements :class:`~ui.preview_panel.PreviewController`, which is the seam
    Phase 2 left for exactly this.
    """

    #: The playhead moved. "qint64", not int: a Qt signal declared with
    #: Python's int is a 32 bit C++ int, and ticks pass two billion after
    #: about five hours. Every tick-carrying signal here is declared this way.
    position_changed = Signal("qint64")
    #: The position reached the end of the project and playback stopped.
    playback_finished = Signal()
    #: "ready", "rendering" or "unavailable".
    bed_state_changed = Signal(str)

    def __init__(
        self,
        project: Project | None,
        video_stage: VideoStage,
        bed_player: QMediaPlayer,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._stage = video_stage
        self._bed = bed_player
        self._panel = None

        self._bed_path: Path | None = None
        self._bed_valid = False
        self._current_clip_id: str | None = None
        self._preloaded_clip_id: str | None = None

        # Only used when there is no usable bed. Started at play(), read as an
        # offset from the position playback began at.
        self._fallback_timer = QElapsedTimer()
        self._fallback_origin_ticks = 0

        self._position = 0
        self._playing = False
        self._scrubbing = False
        self._bed_state = BED_UNAVAILABLE

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        # Precise, because a coarse timer on Windows can slip to 15ms
        # granularity and the video would judder against a bed that does not.
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    # -- PreviewController protocol ---------------------------------------

    def attach(self, panel) -> None:
        self._panel = panel
        # The bed player has a real QAudioOutput, so the volume slider is live
        # again. It is a monitoring level and nothing else: clip gain and track
        # mutes are baked into the bed by FFmpeg, and are not reachable here.
        panel.set_audio_available(True)
        self._report_bed_state()

    def set_project(self, project: Project | None) -> None:
        self._project = project
        self._position = 0
        self._current_clip_id = None
        self._preloaded_clip_id = None
        self._playing = False
        self._scrubbing = False
        self._timer.stop()
        self._stage.stop_all()
        self._report_duration()
        self._report_playing()

    def position_ticks(self) -> int:
        return self._position

    def duration_ticks(self) -> int:
        return self._project.duration if self._project else 0

    def seek(self, ticks: int) -> None:
        """Move the playhead. Works while playing, and does not change whether
        it is playing: a frame step, Home or End moves the position and nothing
        else."""
        self._move_to(ticks, move_bed=True)

    def _move_to(self, ticks: int, move_bed: bool) -> None:
        """Put the playhead at ``ticks`` and put the picture on that frame.

        The video is placed EXACTLY rather than through the loose drift
        threshold, because this position is one the user chose rather than one
        playback drifted to.

        The clip ids are deliberately NOT cleared. Clearing them would make
        every call re-enter the clip, and re-entering means load_active, which
        means a setSource. During a scrub that is one setSource per mouse-move
        on a file that is already open. Staying in the same clip and seeking
        the player is both correct and free.
        """
        ticks = max(0, int(ticks))
        duration = self.duration_ticks()
        if duration > 0:
            ticks = min(ticks, duration)

        self._position = ticks
        if move_bed:
            self._bed.setPosition(_ticks_to_ms(ticks))
        self._fallback_origin_ticks = ticks
        self._fallback_timer.restart()

        self._resolve(ticks, allow_finish=False, exact=True)

    # -- scrubbing --------------------------------------------------------

    def begin_scrub(self) -> None:
        """The user grabbed the playhead. Playback stops.

        Grabbing the playhead is a take-manual-control gesture, and every
        editor stops on it. Nothing here remembers that it was playing,
        because nothing auto-resumes: the user presses Space when ready.
        """
        self._scrubbing = True
        if self._playing:
            self.pause()

    def scrub_to(self, ticks: int) -> None:
        """A position mid-drag. The picture follows; the bed does not move.

        The bed is left paused and where it was for the whole drag. Seeking it
        per mouse-move would be fifty seeks for one gesture, and scrub audio is
        out of scope for v1 anyway. end_scrub aligns it, once.
        """
        self._move_to(ticks, move_bed=False)

    def end_scrub(self, ticks: int) -> None:
        """The drag ended. Align the bed once, and stay paused."""
        self._scrubbing = False
        self._move_to(ticks, move_bed=True)

    def is_scrubbing(self) -> bool:
        return self._scrubbing

    def play(self) -> None:
        if self._project is None or self.duration_ticks() <= 0:
            return
        if self._position >= self.duration_ticks():
            self.seek(0)

        self._playing = True
        if self._using_bed():
            self._bed.setPosition(_ticks_to_ms(self._position))
            self._bed.play()
        else:
            self._fallback_origin_ticks = self._position
            self._fallback_timer.restart()

        if self._current_clip_id is not None:
            self._stage.active_player().play()
        self._timer.start()
        self._report_playing()
        self._tick()

    def pause(self) -> None:
        self._playing = False
        self._timer.stop()
        self._bed.pause()
        self._stage.active_player().pause()
        self._report_playing()

    def is_playing(self) -> bool:
        return self._playing

    def set_volume(self, percent: int) -> None:
        """Monitoring level for the bed player.

        THIS DOES NOT TOUCH THE MODEL. ``clip.gain_db`` and track mutes are
        baked into the bed by FFmpeg before it ever reaches this player, so
        turning the slider down changes what the operator hears and nothing
        about what will be exported. There is deliberately no path from here to
        a Clip.
        """
        output = self._bed.audioOutput()
        if output is None:
            return
        # A slider that maps linearly onto amplitude feels dead across its
        # bottom half. Qt's own conversion is what a volume control should use.
        output.setVolume(
            QAudio.convertVolume(
                max(0, min(100, int(percent))) / 100.0,
                QAudio.VolumeScale.LogarithmicVolumeScale,
                QAudio.VolumeScale.LinearVolumeScale,
            )
        )

    # -- the bed ----------------------------------------------------------

    def set_bed(self, path: Path | None) -> None:
        """A freshly rendered bed arrived, or there is none to have.

        ``None`` means the project has no unmuted audio at all, which is not a
        failure: playback carries on silently on the elapsed timer.
        """
        if path is None:
            self._bed_path = None
            self._bed_valid = False
            self._bed.stop()
            self._bed.setSource(QUrl())
            self._set_bed_state(BED_UNAVAILABLE)
            if self._playing:
                # Hand the clock over without a jump.
                self._fallback_origin_ticks = self._position
                self._fallback_timer.restart()
            return

        self._bed_path = Path(path)
        self._bed_valid = True
        self._bed.setSource(QUrl.fromLocalFile(str(self._bed_path.resolve())))
        self._bed.setPosition(_ticks_to_ms(self._position))
        self._set_bed_state(BED_READY)
        if self._playing:
            # It landed mid-playback. Start it where the timer had got to, so
            # the handover from timer to bed happens at the current position
            # rather than restarting the clock.
            self._bed.play()

    def invalidate_bed(self) -> None:
        """The bed is stale, a new one is being rendered.

        Playback does not stop and does not wait. The elapsed timer takes over
        from wherever the bed had reached, and set_bed hands it back.
        """
        self._bed_valid = False
        self._bed.pause()
        if self._playing:
            self._fallback_origin_ticks = self._position
            self._fallback_timer.restart()
        self._set_bed_state(
            BED_RENDERING if self._project_has_audio() else BED_UNAVAILABLE
        )

    def bed_state(self) -> str:
        return self._bed_state

    def bed_path(self) -> Path | None:
        return self._bed_path

    # -- edits ------------------------------------------------------------

    def project_changed(self) -> None:
        """The open project was edited in place.

        Not the same as set_project: the playhead stays where it is. What has
        to be re-read is the duration, for the scrubber's span, and the clip
        under the playhead, which an edit may have moved out from under it.
        """
        self._report_duration()
        self._current_clip_id = None
        self._preloaded_clip_id = None
        position = min(self._position, self.duration_ticks() or self._position)
        self._position = position
        self._resolve(position, allow_finish=False)

    # -- the tick ---------------------------------------------------------

    def _tick(self) -> None:
        self._resolve(self._read_clock(), allow_finish=True)


    def _read_clock(self) -> int:
        """Where the timeline is, now. Read, never accumulated."""
        if not self._playing:
            # A seek or a pause owns the position; nothing is running.
            return self._position
        if self._using_bed():
            return _ms_to_ticks(self._bed.position())
        if not self._fallback_timer.isValid():
            return self._position
        return self._fallback_origin_ticks + _ms_to_ticks(
            self._fallback_timer.elapsed()
        )

    def _resolve(
        self, position: int, allow_finish: bool, exact: bool = False
    ) -> None:
        duration = self.duration_ticks()
        finished = duration > 0 and position >= duration
        if finished:
            position = duration

        self._position = max(0, position)
        self._update_video(self._frame_position(self._position), exact)

        if finished and allow_finish and self._playing:
            self.pause()
            self.playback_finished.emit()

        self.position_changed.emit(self._position)
        if self._panel is not None:
            self._panel.report_position(self._position)

    def _frame_position(self, position: int) -> int:
        """Which frame to show for a reported position.

        The same position, except at the very end of the timeline. clip_at is
        half open, so nothing matches at exactly project.duration and the stage
        would go black the instant playback finished. Every editor parks on the
        last frame instead, so the frame is resolved one frame back while the
        REPORTED position stays at the true end: the timecode still reads the
        end of the project and the playhead still sits at the end of the
        timeline. Only the picture is resolved earlier.

        Converted through the frame index rather than by subtracting a frame
        length, for the reason in core.timebase.frames_to_ticks: at a rate
        whose frames are not a whole number of ticks, subtracting a constant
        does not land on a boundary.

        A timeline that ends in a gap is unaffected. One frame back from the
        end of a gap is still inside that gap, so black stays black.
        """
        duration = self.duration_ticks()
        if duration <= 0 or position < duration:
            return position
        rate = self._project.frame_rate if self._project else None
        if rate is None:
            return position
        return frames_to_ticks(ticks_to_frames(duration - 1, rate), rate)

    # -- video ------------------------------------------------------------

    def _update_video(self, position: int, exact: bool = False) -> None:
        track = self._video_track()
        target = clip_at(track, position) if track is not None else None

        if target is None:
            if self._current_clip_id is not None or not self._stage.is_black():
                self._stage.show_black()
            self._current_clip_id = None
            self._schedule_preload(position)
            return

        if target.id != self._current_clip_id:
            self._enter_clip(target, position, exact)
            return

        self._align(target, position, exact)

    def _enter_clip(self, clip, position: int, exact: bool = False) -> None:
        in_ms = _ticks_to_ms(clip.src_in + max(0, position - clip.timeline_start))

        if clip.id == self._preloaded_clip_id:
            # The fast path. The standby player is already open on this source
            # and parked on its in-frame, so the cut is a stack index change.
            self._stage.present_standby()
            if not self._playing:
                self._stage.active_player().pause()
            # A preload that had not finished seeking by the time the cut came
            # is still worth presenting, because the alternative is a hard
            # load, but it may be parked on the wrong frame. Correct it now
            # rather than leaving it to the next tick: this is the same
            # video-only correction as the drift check, just taken at the one
            # moment it is known to be worth taking.
            self._align(clip, position, exact)
        else:
            # The slow path: nothing was preloaded for this clip, which happens
            # after a seek or when a preload had no time to settle.
            self._stage.load_active(clip.src, in_ms)
            if self._playing:
                self._stage.active_player().play()

        self._current_clip_id = clip.id
        self._preloaded_clip_id = None
        self._schedule_preload(position)

    def _schedule_preload(self, position: int) -> None:
        track = self._video_track()
        if track is None:
            return
        following = next_clip_after(track, position)
        if following is None or following.id == self._preloaded_clip_id:
            return
        if following.id == self._current_clip_id:
            return
        # A split leaves two clips on one source file. Preload anyway: two
        # players open on the same file is fine, and it keeps the cut on the
        # fast path.
        self._stage.preload(following.src, _ticks_to_ms(following.src_in))
        self._preloaded_clip_id = following.id

    def _align(self, clip, position: int, exact: bool) -> None:
        """Put the active video player where the timeline says it should be.

        Two modes, and the difference matters. During playback the video is
        only nudged when it has drifted past the tolerance, because correcting
        every tick would thrash. After a seek or mid-scrub the position is one
        the user chose, so it is set outright.
        """
        expected_ms = _ticks_to_ms(
            clip.src_in + max(0, position - clip.timeline_start)
        )
        player = self._stage.active_player()
        if exact or abs(player.position() - expected_ms) > DRIFT_TOLERANCE_MS:
            player.setPosition(expected_ms)

    def _drift_check(self, clip, position: int) -> None:
        """Nudge the video back to the audio. Never the other way round."""
        self._align(clip, position, exact=False)

    # -- helpers ----------------------------------------------------------

    def _video_track(self):
        if self._project is None:
            return None
        video = self._project.video_tracks()
        return video[0] if video else None

    def _project_has_audio(self) -> bool:
        return bool(self._project is not None and self._project.has_audio)

    def _using_bed(self) -> bool:
        return self._bed_valid and self._bed_path is not None

    def _set_bed_state(self, state: str) -> None:
        if state == self._bed_state:
            return
        self._bed_state = state
        self._report_bed_state()

    def _report_bed_state(self) -> None:
        self.bed_state_changed.emit(self._bed_state)
        if self._panel is not None:
            self._panel.set_status(_STATUS_TEXT[self._bed_state])

    def _report_duration(self) -> None:
        if self._panel is not None:
            self._panel.report_duration(self.duration_ticks())

    def _report_playing(self) -> None:
        if self._panel is not None:
            self._panel.report_playing(self._playing)

    # -- shutdown ---------------------------------------------------------

    def shutdown(self) -> None:
        """Stop everything. Safe to call more than once."""
        self._playing = False
        self._timer.stop()
        self._bed.stop()
        self._bed.setSource(QUrl())
        self._stage.stop_all()
        self._current_clip_id = None
        self._preloaded_clip_id = None
