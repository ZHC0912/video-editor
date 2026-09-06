"""The preview panel: a video stage plus a transport.

The panel owns widgets and knows nothing about how playback works. A
controller supplies that, and the panel talks to it through
:class:`PreviewController`. That seam held through Phase 5: the whole
controller was replaced with
:class:`~ui.playback_controller.PlaybackController` and nothing here had to
change to accommodate it. It has since been widened once, deliberately, for
scrubbing: a scrub is a three phase gesture and seek() alone could not say
where one begins and ends.

Direction of traffic:
    user gesture      -> panel calls the controller
    playback progress -> controller calls report_* on the panel
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.model import Project
from core.timebase import FrameRate, frames_to_ticks, ticks_to_frames
from ui import format_utils, theme
from ui.video_stage import VideoStage

__all__ = ["PreviewPanel", "PreviewController"]

# QSlider is a 32 bit int. A tick timebase is not: a 24 hour timeline is over
# ten billion ticks, which would silently overflow. The scrubber therefore has
# a fixed resolution and positions are mapped in and out of it.
_SCRUB_STEPS = 100_000


@runtime_checkable
class PreviewController(Protocol):
    """What the panel needs from whatever is driving playback."""

    def attach(self, panel: "PreviewPanel") -> None: ...
    def set_project(self, project: Project | None) -> None: ...
    def position_ticks(self) -> int: ...
    def duration_ticks(self) -> int: ...
    def seek(self, ticks: int) -> None:
        """Move the playhead to ``ticks`` and put that frame on the stage.

        A one shot move: a frame step, Home, End, a click on the timeline.
        Moves the audio player to match, and does NOT change whether playback
        is running.
        """
        ...

    def begin_scrub(self) -> None:
        """The user grabbed the playhead, by the ruler or by the scrubber.

        Stop playback if it was running, and leave the audio player paused for
        the rest of the gesture. There is no scrub audio in this version.
        """
        ...

    def scrub_to(self, ticks: int) -> None:
        """A position mid drag. The stage follows; the audio player does not.

        Do not move the audio player here. A drag is tens of these, and
        seeking a media player per mouse move is both wasteful and audible.
        end_scrub aligns it, once.
        """
        ...

    def end_scrub(self, ticks: int) -> None:
        """The drag ended at ``ticks``.

        This is where the audio player is moved, exactly once, so that a
        subsequent play() resumes from the right place in sync. Playback stays
        stopped: nothing auto resumes, the user presses Space.
        """
        ...

    def play(self) -> None: ...
    def pause(self) -> None: ...
    def is_playing(self) -> bool: ...
    def set_volume(self, percent: int) -> None: ...


class PreviewPanel(QWidget):
    """Video stage, transport bar, scrubber, timecode, volume, status line."""

    #: Emitted whenever the playhead moves, in ticks.
    #
    #: "qint64", not int. A Qt signal declared with Python's int is a 32 bit
    #: C++ int, and a tick position passes two billion after about five hours.
    #: Every signal in this application carrying ticks must be declared this
    #: way.
    position_changed = Signal("qint64")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._controller: PreviewController | None = None
        self._project: Project | None = None
        self._position = 0
        self._duration = 0
        self._scrubbing = False

        self.stage = VideoStage(self)
        self.stage.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        style = self.style()

        def button(standard_pixmap: QStyle.StandardPixmap, tip: str) -> QToolButton:
            btn = QToolButton(self)
            btn.setIcon(style.standardIcon(standard_pixmap))
            btn.setToolTip(tip)
            btn.setAutoRaise(False)
            return btn

        self._skip_start = button(
            QStyle.StandardPixmap.SP_MediaSkipBackward, "Go to start (Home)"
        )
        self._frame_back = button(
            QStyle.StandardPixmap.SP_MediaSeekBackward, "Previous frame (Left)"
        )
        self._play_pause = button(QStyle.StandardPixmap.SP_MediaPlay, "Play (Space)")
        self._frame_forward = button(
            QStyle.StandardPixmap.SP_MediaSeekForward, "Next frame (Right)"
        )
        self._skip_end = button(
            QStyle.StandardPixmap.SP_MediaSkipForward, "Go to end (End)"
        )

        self._skip_start.clicked.connect(self.go_to_start)
        self._frame_back.clicked.connect(self.step_back)
        self._play_pause.clicked.connect(self.toggle_play)
        self._frame_forward.clicked.connect(self.step_forward)
        self._skip_end.clicked.connect(self.go_to_end)

        self._scrubber = QSlider(Qt.Orientation.Horizontal, self)
        self._scrubber.setRange(0, _SCRUB_STEPS)
        self._scrubber.setSingleStep(1)
        self._scrubber.setPageStep(_SCRUB_STEPS // 20)
        self._scrubber.sliderPressed.connect(self._on_scrub_pressed)
        self._scrubber.sliderReleased.connect(self._on_scrub_released)
        self._scrubber.sliderMoved.connect(self._on_scrub_moved)

        self._timecode = QLabel("00:00:00:00", self)
        self._timecode.setFont(theme.mono_font())
        self._timecode.setProperty("timecode", True)
        self._timecode.setToolTip("Position, HH:MM:SS:FF")

        self._duration_label = QLabel("00:00:00:00", self)
        self._duration_label.setFont(theme.mono_font())
        self._duration_label.setProperty("muted", True)
        self._duration_label.setToolTip("Duration, HH:MM:SS:FF")

        self._volume = QSlider(Qt.Orientation.Horizontal, self)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setFixedWidth(90)
        self._volume.setToolTip("Volume")
        self._volume.valueChanged.connect(self._on_volume_changed)

        # The controller owns this text: "" when the bed is driving
        # playback, "Rendering audio preview" while one is being built,
        # "No audio in project" when there is nothing to mix.
        self._status = QLabel("", self)
        self._status.setProperty("muted", True)

        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(4)
        transport.addWidget(self._skip_start)
        transport.addWidget(self._frame_back)
        transport.addWidget(self._play_pause)
        transport.addWidget(self._frame_forward)
        transport.addWidget(self._skip_end)
        transport.addSpacing(8)
        transport.addWidget(self._timecode)
        transport.addWidget(QLabel("/", self))
        transport.addWidget(self._duration_label)
        transport.addStretch(1)
        transport.addWidget(QLabel("Vol", self))
        transport.addWidget(self._volume)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.stage, 1)
        layout.addWidget(self._scrubber)
        layout.addLayout(transport)
        layout.addWidget(self._status)

        self._refresh_labels()
        self._update_enabled()

    # -- wiring -----------------------------------------------------------

    def set_controller(self, controller: PreviewController | None) -> None:
        """Install the object that actually drives playback."""
        self._controller = controller
        if controller is not None:
            controller.attach(self)
            controller.set_project(self._project)
            controller.set_volume(self._volume.value())
        self._update_enabled()

    def controller(self) -> PreviewController | None:
        return self._controller

    # -- public API, driven by Phase 5 ------------------------------------

    def set_project(self, project: Project | None) -> None:
        self._project = project
        if self._controller is not None:
            self._controller.set_project(project)
        self._position = 0
        self._duration = project.duration if project else 0
        self._refresh_labels()
        self._update_enabled()

    def project(self) -> Project | None:
        return self._project

    def frame_rate(self) -> FrameRate:
        return self._project.frame_rate if self._project else FrameRate(30, 1)

    def position_ticks(self) -> int:
        """The playhead, in ticks.

        The panel's own cached value, not a read back through the controller.
        A controller stores its position in player milliseconds, so asking it
        would round trip every frame step through a coarser unit and the tick
        arithmetic would stop being exact. The controller keeps this value
        current by calling report_position.
        """
        return self._position

    def duration_ticks(self) -> int:
        return self._duration

    def seek(self, ticks: int) -> None:
        ticks = self._clamp(ticks)
        if self._controller is not None:
            self._controller.seek(ticks)
        else:
            self.report_position(ticks)

    def _clamp(self, ticks: int) -> int:
        return max(0, min(int(ticks), self._duration or int(ticks)))

    def play(self) -> None:
        if self._controller is not None:
            self._controller.play()

    def pause(self) -> None:
        if self._controller is not None:
            self._controller.pause()

    def is_playing(self) -> bool:
        return self._controller is not None and self._controller.is_playing()

    def toggle_play(self) -> None:
        self.pause() if self.is_playing() else self.play()

    # -- transport --------------------------------------------------------

    def frame_step_ticks(self) -> int:
        """The length of one frame at the project's rate.

        4000 ticks at 30fps, 4004 at 29.97. Never a rounded millisecond. This
        is the frame duration for measuring and drawing; stepping does not use
        it, for the reason in :meth:`step_forward`.
        """
        return frames_to_ticks(1, self.frame_rate())

    def step_forward(self) -> None:
        """Move to the start of the next frame.

        Converted through the frame index rather than added to the current
        position. Adding a per frame constant accumulates its rounding error:
        at a rate whose frames are not a whole number of ticks long, 24249/1000
        say, two thousand presses land four figures of ticks away from the
        frame they claim to be on. Going through the index is exact at any
        rate, and it self corrects when the position arrives mid frame, which
        it does whenever it came from a player reporting milliseconds.
        """
        rate = self.frame_rate()
        frame = ticks_to_frames(self.position_ticks(), rate)
        self.seek(frames_to_ticks(frame + 1, rate))

    def step_back(self) -> None:
        """Move to the start of the previous frame. See :meth:`step_forward`."""
        rate = self.frame_rate()
        frame = ticks_to_frames(self.position_ticks(), rate)
        self.seek(frames_to_ticks(max(0, frame - 1), rate))

    def go_to_start(self) -> None:
        self.seek(0)

    def go_to_end(self) -> None:
        self.seek(self._duration)

    # -- callbacks for the controller -------------------------------------

    def report_position(self, ticks: int) -> None:
        """The controller telling the panel where the playhead is."""
        ticks = max(0, int(ticks))
        if ticks == self._position:
            return
        self._position = ticks
        self._refresh_labels()
        if not self._scrubbing:
            self._sync_scrubber()
        self.position_changed.emit(ticks)

    def report_duration(self, ticks: int) -> None:
        self._duration = max(0, int(ticks))
        self._refresh_labels()
        self._sync_scrubber()
        self._update_enabled()

    def report_playing(self, playing: bool) -> None:
        icon = (
            QStyle.StandardPixmap.SP_MediaPause
            if playing
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self._play_pause.setIcon(self.style().standardIcon(icon))
        self._play_pause.setToolTip("Pause (Space)" if playing else "Play (Space)")

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_audio_available(self, available: bool) -> None:
        """Whether the current controller can actually make sound.

        The slider is a monitoring level for whatever the controller is
        playing. It never reaches the model: clip gain and track mutes are
        baked into the audio bed by FFmpeg long before a player sees it.
        """
        self._volume.setEnabled(available)
        self._volume.setToolTip(
            "Volume" if available else "Audio preview arrives with the timeline"
        )

    # -- internals --------------------------------------------------------

    def _refresh_labels(self) -> None:
        rate = self.frame_rate()
        self._timecode.setText(format_utils.timecode(self._position, rate))
        self._duration_label.setText(format_utils.timecode(self._duration, rate))

    def _sync_scrubber(self) -> None:
        if self._duration <= 0:
            self._scrubber.setValue(0)
            return
        value = round(self._position * _SCRUB_STEPS / self._duration)
        self._scrubber.blockSignals(True)
        self._scrubber.setValue(max(0, min(_SCRUB_STEPS, value)))
        self._scrubber.blockSignals(False)

    def _scrub_to_ticks(self, value: int) -> int:
        if self._duration <= 0:
            return 0
        return round(value * self._duration / _SCRUB_STEPS)

    def _on_scrub_pressed(self) -> None:
        """The scrubber and the timeline ruler are one gesture through two
        widgets, so they take the same three phase route. Anything else is an
        inconsistency a user feels without being able to name it."""
        self._scrubbing = True
        if self._controller is not None:
            self._controller.begin_scrub()

    def _on_scrub_moved(self, value: int) -> None:
        self._scrub(self._scrub_to_ticks(value), final=False)

    def _on_scrub_released(self) -> None:
        # sliderReleased carries no value, so read the one the slider settled
        # on. A release with no preceding move is a click on the groove, and
        # this is then the whole gesture.
        self._scrubbing = False
        self._scrub(self._scrub_to_ticks(self._scrubber.value()), final=True)

    def _scrub(self, ticks: int, final: bool) -> None:
        ticks = self._clamp(ticks)
        if self._controller is None:
            self.report_position(ticks)
        elif final:
            self._controller.end_scrub(ticks)
        else:
            self._controller.scrub_to(ticks)

    def _on_volume_changed(self, value: int) -> None:
        if self._controller is not None:
            self._controller.set_volume(value)

    def _update_enabled(self) -> None:
        live = self._controller is not None and self._duration > 0
        for widget in (
            self._skip_start,
            self._frame_back,
            self._play_pause,
            self._frame_forward,
            self._skip_end,
            self._scrubber,
        ):
            widget.setEnabled(live)
