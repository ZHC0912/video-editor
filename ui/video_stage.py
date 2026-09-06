"""The double-buffered video surface.

Two players, two video widgets, one stack. While one plays, the other is loaded
with the next clip and parked, paused, on its in-frame. Swapping at a cut is
then a stack index change instead of a load, which is what removes the hitch at
every edit point.

Built now, driven fully in Phase 5. This phase uses one player at a time.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

__all__ = ["VideoStage"]

# How close the reported position has to be to the requested one before a
# preload counts as settled. A frame at 24fps is about 42ms; this is loose
# enough to survive a seek landing on a nearby keyframe boundary.
_SEEK_TOLERANCE_MS = 120


class VideoStage(QWidget):
    """A stack of two video widgets plus a black page.

    Public API:
        show_black()
        active_player() / standby_player()
        present_standby()
        preload(path, position_ms)
        standby_ready signal
    """

    #: The standby player has loaded its source and its seek has settled.
    standby_ready = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._stack = QStackedWidget(self)
        self._stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        # Page 0 is plain black, shown before anything is loaded and during
        # timeline gaps. Cheaper and steadier than parking a player on a black
        # frame.
        self._black = QWidget(self)
        self._black.setStyleSheet("background: #000000;")
        self._stack.addWidget(self._black)

        self._widgets: list[QVideoWidget] = []
        self._players: list[QMediaPlayer] = []

        # Slot A.
        widget_a = QVideoWidget(self)
        player_a = QMediaPlayer(self)
        # NO QAudioOutput, deliberately. The video players are silent. Phase 5
        # plays a single pre-rendered WAV of the whole timeline and uses its
        # position as the master clock; a second audio source here would drift
        # against it and there would be no way to tell which one was right.
        # If you are here to "fix" missing sound, the bed player is the place.
        player_a.setVideoOutput(widget_a)
        self._widgets.append(widget_a)
        self._players.append(player_a)
        self._stack.addWidget(widget_a)

        # Slot B.
        widget_b = QVideoWidget(self)
        player_b = QMediaPlayer(self)
        # NO QAudioOutput here either, for exactly the same reason. Both video
        # players stay silent for the life of the application.
        player_b.setVideoOutput(widget_b)
        self._widgets.append(widget_b)
        self._players.append(player_b)
        self._stack.addWidget(widget_b)

        self._active = 0
        self._pending_ms: list[int | None] = [None, None]

        for slot, player in enumerate(self._players):
            player.mediaStatusChanged.connect(partial(self._on_media_status, slot))
            player.positionChanged.connect(partial(self._on_position, slot))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

        self.show_black()

    # -- roles ------------------------------------------------------------

    def active_player(self) -> QMediaPlayer:
        return self._players[self._active]

    def standby_player(self) -> QMediaPlayer:
        return self._players[1 - self._active]

    def active_widget(self) -> QVideoWidget:
        return self._widgets[self._active]

    # -- presentation -----------------------------------------------------

    def show_black(self) -> None:
        """Raise the black page. Does not stop either player."""
        self._stack.setCurrentWidget(self._black)

    def show_active(self) -> None:
        """Raise the currently active video widget."""
        self._stack.setCurrentWidget(self._widgets[self._active])

    def is_black(self) -> bool:
        return self._stack.currentWidget() is self._black

    def present_standby(self) -> None:
        """Swap the roles and show the newly active player.

        This runs at a cut, so it does the least possible: change the stack
        index and start the player that was already parked on its in-frame.
        Every expensive thing belongs in preload().
        """
        self._active = 1 - self._active
        self._stack.setCurrentWidget(self._widgets[self._active])
        self._players[self._active].play()

    # -- loading ----------------------------------------------------------

    def preload(self, path: Path | str, position_ms: int = 0) -> None:
        """Load a source into the standby player, seek it, and leave it paused.

        Emits :attr:`standby_ready` once the seek has settled. Nothing becomes
        visible; call :meth:`present_standby` for that.
        """
        slot = 1 - self._active
        player = self._players[slot]
        self._pending_ms[slot] = max(0, int(position_ms))

        url = QUrl.fromLocalFile(str(Path(path).resolve()))
        if player.source() == url:
            # Qt does not re-emit LoadedMedia for a source that is already
            # open, so seek it directly rather than waiting for a status that
            # will never arrive.
            player.pause()
            self._seek_pending(slot)
            return

        player.setSource(url)
        player.pause()

    def load_active(self, path: Path | str, position_ms: int = 0) -> None:
        """Load straight into the active player, seek it, and show it.

        This is the slow path at a cut: the clip that was needed had not been
        preloaded, which happens after a seek or a scrub.
        """
        slot = self._active
        player = self._players[slot]
        self._pending_ms[slot] = max(0, int(position_ms))

        url = QUrl.fromLocalFile(str(Path(path).resolve()))
        if player.source() == url:
            # The same guard preload() carries, and for the same reason: Qt
            # does not re-emit LoadedMedia for a source that is already open,
            # so the pending seek would wait for a status that never arrives
            # and the player would stay on whatever frame it was showing.
            # Scrubbing backwards across a split hits this every time, because
            # every clip of a split is the same file.
            self._seek_pending(slot)
        else:
            player.setSource(url)
        self._stack.setCurrentWidget(self._widgets[slot])

    def stop_all(self) -> None:
        for player in self._players:
            player.stop()
        self._pending_ms = [None, None]
        self.show_black()

    # -- internals --------------------------------------------------------

    def _seek_pending(self, slot: int) -> None:
        target = self._pending_ms[slot]
        if target is None:
            return
        self._players[slot].setPosition(target)
        if target == 0:
            # A freshly loaded player is already at zero and may never report
            # a position change, so settle now rather than waiting forever.
            self._settle(slot)

    def _settle(self, slot: int) -> None:
        if self._pending_ms[slot] is None:
            return
        self._pending_ms[slot] = None
        if slot != self._active:
            self.standby_ready.emit()

    def _on_media_status(self, slot: int, status: QMediaPlayer.MediaStatus) -> None:
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._seek_pending(slot)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            # Nothing will ever settle. Drop the request rather than leaving a
            # caller waiting on a signal that cannot arrive.
            self._pending_ms[slot] = None

    def _on_position(self, slot: int, position_ms: int) -> None:
        target = self._pending_ms[slot]
        if target is not None and abs(position_ms - target) <= _SEEK_TOLERANCE_MS:
            self._settle(slot)
