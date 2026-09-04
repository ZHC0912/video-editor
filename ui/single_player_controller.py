"""Plays one imported file, end to end.

This phase only. It exists so the preview panel has something to drive it
before the timeline exists, and it lives in its own file so that Phase 5
replaces it without preview_panel.py being touched.

The preview is SILENT in this phase, and that is deliberate. The stage's video
players never get a QAudioOutput, so the only ways to have sound here would be
to break that rule or to open the file a second time in a separate audio
player. The second one was tried and it meant every import was demuxed twice,
with two backend banners for one double-click. Audio preview belongs to the
pre-rendered bed in Phase 5, which is one decode for the whole timeline.

One file, one setSource, one open.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtMultimedia import QMediaPlayer

from core.model import Project
from core.timebase import TICKS_PER_SECOND, seconds_to_ticks, ticks_to_seconds
from ui.preview_panel import PreviewPanel
from ui.video_stage import VideoStage

__all__ = ["SinglePlayerController"]

_MS_PER_TICK = 1000 / TICKS_PER_SECOND


def _ticks_to_ms(ticks: int) -> int:
    return round(ticks_to_seconds(ticks) * 1000)


def _ms_to_ticks(ms: int) -> int:
    return seconds_to_ticks(ms / 1000)


class SinglePlayerController(QObject):
    """Drives a PreviewPanel from a single media file."""

    def __init__(self, stage: VideoStage, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stage = stage
        self._panel: PreviewPanel | None = None
        self._project: Project | None = None
        self._path: Path | None = None
        self._duration_ticks = 0

        video = self._stage.active_player()
        video.positionChanged.connect(self._on_position)
        video.durationChanged.connect(self._on_duration)
        video.playbackStateChanged.connect(self._on_state)
        video.mediaStatusChanged.connect(self._on_status)

    # -- PreviewController protocol ---------------------------------------

    def attach(self, panel: PreviewPanel) -> None:
        self._panel = panel
        # Nothing here makes sound, so do not offer a volume control that
        # cannot do anything. Phase 5's bed player takes it over.
        panel.set_audio_available(False)

    def set_project(self, project: Project | None) -> None:
        self._project = project

    def position_ticks(self) -> int:
        return _ms_to_ticks(self._stage.active_player().position())

    def duration_ticks(self) -> int:
        return self._duration_ticks

    def seek(self, ticks: int) -> None:
        self._stage.active_player().setPosition(_ticks_to_ms(max(0, ticks)))
        if self._panel is not None:
            self._panel.report_position(max(0, ticks))

    def play(self) -> None:
        if self._path is None:
            return
        self._stage.active_player().play()

    def pause(self) -> None:
        self._stage.active_player().pause()

    def is_playing(self) -> bool:
        return (
            self._stage.active_player().playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        )

    def set_volume(self, percent: int) -> None:
        """No-op. This phase has no audio path. Phase 5's bed player has one."""

    # -- this phase's own API ---------------------------------------------

    def load(self, path: Path) -> None:
        """Open a file in the preview and park it at the first frame.

        Exactly one setSource, on exactly one player.
        """
        self._path = Path(path)
        self._stage.load_active(self._path, position_ms=0)

        if self._panel is not None:
            self._panel.set_status(f"Previewing {self._path.name}  (silent until Phase 5)")
            self._panel.report_position(0)

    def clear(self) -> None:
        self._path = None
        self._duration_ticks = 0
        self._stage.stop_all()
        if self._panel is not None:
            self._panel.set_status("")
            self._panel.report_duration(0)

    # -- player signals ---------------------------------------------------

    def _on_position(self, ms: int) -> None:
        if self._panel is not None:
            self._panel.report_position(_ms_to_ticks(ms))

    def _on_duration(self, ms: int) -> None:
        self._duration_ticks = _ms_to_ticks(ms)
        if self._panel is not None:
            self._panel.report_duration(self._duration_ticks)

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        if self._panel is not None:
            self._panel.report_playing(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            # Park at the end rather than snapping back to zero.
            if self._panel is not None:
                self._panel.report_playing(False)
