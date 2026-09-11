"""The application shell.

Menus, the media bin, the preview panel, the timeline, and the export job.

Nothing long running happens on the GUI thread. Probing is fast enough to be
synchronous; rendering is not, and runs on a QThread. The encoder capability
probe and the relink folder search are on pool threads for the same reason.

The window owns the project lifecycle: what is open, whether it has unsaved
changes, where it autosaves, and what is remembered between runs. The model
itself is only ever changed through :meth:`MainWindow.run_command`.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import (
    QElapsedTimer,
    QObject,
    QRect,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QLabel,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
)

from core.commands import (
    AddTrack,
    Command,
    CommandError,
    CommandStack,
    DeleteClip,
    DuplicateClip,
    MacroCommand,
    RelinkMedia,
    SetTrackMuted,
    SplitClip,
)
from core.encoders import SOFTWARE_H264
from core.filtergraph import RenderError, build_full
from core.media_check import (
    MissingSource,
    missing_clip_ids,
    missing_sources,
    relink_map,
    without_missing_clips,
)
from core.model import MediaInfo, Project, Track
from core.project_io import (
    PROJECT_EXTENSION,
    PROJECT_FORMAT_NAME,
    ProjectIOError,
    autosave_is_newer,
    autosave_path,
    discard_autosave,
    is_project_path,
    load,
    save,
    with_project_extension,
)
from core.render import render
from core.timebase import (
    TICKS_PER_SECOND,
    frames_to_ticks,
    snap_to_frame,
    ticks_to_frames,
)
from ui import format_utils, poster, theme, window_geometry
from ui.dialogs import confirm, show_error, split_error
from ui.export_dialog import ExportDialog, ExportRequest
from ui.media_bin import MediaBinList, has_droppable_files, dropped_paths, set_drop_highlight
from ui.playback_controller import PlaybackController
from ui.preview_panel import PreviewPanel
from ui.relink_dialog import RelinkDialog
from ui.settings import Settings
from ui.timeline.interaction import media_payload
from ui.timeline.timeline_view import FitOutcome, TimelinePanel
from ui.workers.audio_bed_worker import AudioBedWorker
from ui.workers.encoder_probe import EncoderProbe
from ui.workers.probe_worker import ProbeQueue
from ui.workers.thumbnail_worker import shared_thumbnail_cache

__all__ = ["MainWindow", "AUTOSAVE_INTERVAL_MS"]

#: Phase 6 says every 120 seconds while there are unsaved changes.
AUTOSAVE_INTERVAL_MS = 120_000

MEDIA_FILTER = (
    "Media files (*.mp4 *.mov *.mkv *.avi *.m4v *.webm *.wav *.mp3 *.aac *.flac);;"
    "All files (*)"
)
#: One filter, built from the extension core defines, used by Open and by
#: Save As. The All files fallback keeps a renamed or oddly named project
#: reachable, because load() does not care what a file is called.
PROJECT_FILTER = (
    f"{PROJECT_FORMAT_NAME} (*{PROJECT_EXTENSION});;All files (*)"
)


class _RenderWorker(QObject):
    """Runs core.render.render on a worker thread.

    on_progress is called from the thread reading ffmpeg's stderr, so it must
    not touch a widget. Emitting a signal is the whole of the translation: Qt
    delivers it to the GUI thread queued, and the progress dialog is updated
    from there. No timer, no interpolation.
    """

    progress = Signal(float, float)
    finished = Signal()
    failed = Signal(str, str)

    def __init__(
        self,
        project: Project,
        out_path: Path,
        cancel: threading.Event,
        crf: int = 20,
        encoder: str = SOFTWARE_H264,
        out_size: tuple[int, int] | None = None,
    ) -> None:
        super().__init__()
        self._project = project
        self._out_path = out_path
        self._cancel = cancel
        self._crf = crf
        self._encoder = encoder
        self._out_size = out_size

    @Slot()
    def run(self) -> None:
        try:
            render(
                self._project,
                self._out_path,
                self._cancel,
                crf=self._crf,
                on_progress=self.progress.emit,
                encoder=self._encoder,
                out_size=self._out_size,
            )
        except RenderError as exc:
            self.failed.emit(*split_error(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a dialog, never a traceback
            self.failed.emit(f"Export failed: {exc}", "")
            return
        self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.setWindowTitle("VidEditor")

        # Everything remembered between runs. Injectable so a test can point
        # at a temporary file instead of the user's real settings.
        self.settings = settings if settings is not None else Settings()
        self.apply_geometry(self.settings.geometry())

        self._project: Project | None = None
        self._project_path: Path | None = None
        self._dirty = False
        # Every change to the project goes through this. See run_command.
        self.commands = CommandStack()
        # Bin contents, keyed by normalised path. Rows exist before their
        # probe returns, so a key may be present with no MediaInfo yet.
        self._media: dict[str, MediaInfo] = {}
        # Source files the project refers to that are not on disk. Cached
        # rather than recomputed on demand: it is read by every refresh of the
        # edit actions, which happens on every click, and it costs a stat per
        # distinct source file.
        self._missing: list[MissingSource] = []

        self._render_thread: QThread | None = None
        self._render_worker: _RenderWorker | None = None
        self._render_cancel: threading.Event | None = None
        self._render_dialog: QProgressDialog | None = None
        # Wall clock for the export, which is what "elapsed" and "remaining"
        # on the progress line mean. The media time ffmpeg reports is the
        # other axis.
        self._render_clock = QElapsedTimer()

        # Whether the GPU encoder is offerable. Answered on a pool thread,
        # because finding out spawns ffmpeg twice; until it answers the
        # export dialog offers software encoding only.
        self.encoder_probe = EncoderProbe(self)

        # Unsaved work is written beside the project file every two minutes.
        # A single shot timer restarted on each edit would autosave two
        # minutes after the LAST edit, which during a long editing session is
        # never; this one just runs.
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self.autosave_now)

        # The playhead is written from two directions: playback pushes it, and
        # dragging the ruler pulls it. An explicit flag, not blockSignals, so
        # that the guard is visible at every site that takes part in it and a
        # third writer added later cannot slip past by connecting elsewhere.
        self._syncing_playhead = False

        # One bed for the window. Phase 5 consumes it; nothing does yet, so
        # for now it only reports into the status bar. It is wired up now
        # because every trigger for it already exists.
        self.audio_bed = AudioBedWorker(self)
        self.audio_bed.bed_ready.connect(self._on_bed_ready)
        self.audio_bed.bed_failed.connect(self._on_bed_failed)
        self.audio_bed.bed_invalidated.connect(self._on_bed_invalidated)
        self.audio_bed.bed_unavailable.connect(self._on_bed_unavailable)

        self._build_ui()
        self._build_menus()
        self.new_project()
        # Splitter sizes are restored on the first show, not here: see
        # showEvent.
        self._layout_restored = False
        self._autosave_timer.start()
        self.encoder_probe.start()

    def showEvent(self, event) -> None:
        """Restore the saved layout the first time the window is shown.

        Not in the constructor. A QSplitter given sizes before it has been
        laid out scales them into whatever width it happens to have, which is
        a couple of hundred pixels of nothing, and the numbers that come back
        afterwards are not the numbers that went in. After the first show it
        has its real width and the sizes mean what they say.
        """
        super().showEvent(event)
        if not self._layout_restored:
            self._layout_restored = True
            self._restore_layout()

    # -- geometry ---------------------------------------------------------

    def apply_geometry(self, saved: QRect | None = None) -> None:
        """Size and place the window inside the screen that is actually there.

        Phase 6 passes the QRect it read back from QSettings. It is validated
        rather than trusted: a geometry saved on a monitor that is no longer
        connected would open the window where the mouse cannot reach it.
        """
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            # No screens at all, which happens under the offscreen platform.
            # Nothing to clamp against, so leave Qt's default alone.
            return

        available = screen.availableGeometry()
        screens = [s.availableGeometry() for s in QGuiApplication.screens()]

        self.setMinimumSize(window_geometry.minimum_size(available))
        self.setGeometry(window_geometry.restored_geometry(saved, screens, available))

    # -- construction -----------------------------------------------------

    def _build_ui(self) -> None:
        self.media_list = MediaBinList(
            self.handle_dropped_paths, self, payload_for=self._bin_drag_payload
        )
        self.media_list.setAlternatingRowColors(False)
        self.media_list.setIconSize(poster.POSTER_SIZE)
        # The poster takes a fixed 114px of every row, so the summary text is
        # elided to fit rather than pushing a horizontal scrollbar under the
        # list.
        self.media_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.media_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.media_list.setWordWrap(False)
        self.media_list.itemDoubleClicked.connect(self._on_media_activated)

        # The same LRU the timeline's filmstrips use. A clip cut from a file
        # already in the bin costs no second decode.
        self._thumbnails = shared_thumbnail_cache()
        self._thumbnails.thumbnail_ready.connect(self._on_bin_thumbnail)

        # Dropped files are probed off the GUI thread; rows appear at once and
        # fill in as each probe returns.
        self._bin_keys: set[str] = set()
        self._probes = ProbeQueue(self)
        self._probes.probed.connect(self._on_probe_finished)
        self._probes.failed.connect(self._on_probe_failed)
        self._probes.batch_finished.connect(self._on_probe_batch_finished)

        bin_panel = QFrame(self)
        bin_panel.setProperty("surface", "panel")
        bin_layout = QVBoxLayout(bin_panel)
        bin_layout.setContentsMargins(8, 8, 8, 8)
        bin_layout.setSpacing(6)
        bin_title = QLabel("Media", bin_panel)
        bin_title.setFont(theme.ui_font(bold=True))
        bin_hint = QLabel("Drag onto a track to use", bin_panel)
        bin_hint.setProperty("muted", True)
        bin_layout.addWidget(bin_title)
        bin_layout.addWidget(self.media_list, 1)
        bin_layout.addWidget(bin_hint)
        # Wide enough for a poster plus two lines of metadata beside it.
        bin_panel.setMinimumWidth(330)

        self.preview = PreviewPanel(self)

        # THE one audio path in the application. The video stage's two players
        # are deliberately silent; this single player opens the pre-rendered
        # bed and its position is the timeline's clock. A second audio source
        # anywhere would drift against it with no way to tell which was right.
        self._bed_output = QAudioOutput(self)
        self._bed_player = QMediaPlayer(self)
        self._bed_player.setAudioOutput(self._bed_output)

        self.playback = PlaybackController(
            None, self.preview.stage, self._bed_player, self
        )
        self.preview.set_controller(self.playback)

        preview_panel = QFrame(self)
        preview_panel.setProperty("surface", "panel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        preview_layout.addWidget(self.preview)

        top = QSplitter(Qt.Orientation.Horizontal, self)
        self._top_splitter = top
        top.addWidget(bin_panel)
        top.addWidget(preview_panel)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setSizes([360, 1040])

        self.timeline = TimelinePanel(self)
        self.timeline.mute_toggled.connect(self._on_track_muted)
        self.timeline.add_track_requested.connect(self._on_add_track)
        self.timeline.fitted.connect(self._on_timeline_fitted)
        self.timeline.command_requested.connect(self.run_command)
        self.timeline.rejected.connect(self._on_gesture_rejected)
        self.timeline.selection_changed.connect(self._refresh_edit_actions)
        self.timeline.scrub_started.connect(self._on_scrub_started)
        self.timeline.playhead_scrubbed.connect(self._on_playhead_scrubbed)
        self.timeline.scrub_finished.connect(self._on_scrub_finished)
        self.timeline.set_media_duration_lookup(self._media_duration)
        self.preview.position_changed.connect(self._on_preview_position)

        timeline_frame = QFrame(self)
        timeline_frame.setProperty("surface", "timeline")
        timeline_layout = QVBoxLayout(timeline_frame)
        timeline_layout.setContentsMargins(4, 4, 4, 4)
        timeline_layout.addWidget(self.timeline)
        timeline_frame.setMinimumHeight(180)

        split = QSplitter(Qt.Orientation.Vertical, self)
        self._main_splitter = split
        split.addWidget(top)
        split.addWidget(timeline_frame)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([560, 260])

        # A QFrame rather than a bare QWidget so the drag-over border from the
        # stylesheet has something to paint on.
        container = QFrame(self)
        self._drop_frame = container
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(split)
        self.setCentralWidget(container)

        # Dropping on the wrong panel is the ordinary mistake, so the whole
        # window takes files, not just the bin.
        self.setAcceptDrops(True)

        self._status_name = QLabel("", self)
        self._status_playhead = QLabel("", self)
        self._status_playhead.setFont(theme.mono_font())
        self._status_playhead.setToolTip("Playhead")
        self._status_duration = QLabel("", self)
        self._status_duration.setFont(theme.mono_font())
        self._status_duration.setToolTip("Project duration")
        self._status_rate = QLabel("", self)

        status = QStatusBar(self)
        status.addWidget(self._status_name)
        status.addPermanentWidget(self._status_playhead)
        status.addPermanentWidget(self._status_duration)
        status.addPermanentWidget(self._status_rate)
        self.setStatusBar(status)

    def _build_menus(self) -> None:
        def action(menu, text: str, slot, shortcut: QKeySequence | str | None = None):
            act = QAction(text, self)
            if shortcut is not None:
                act.setShortcut(shortcut)
            act.triggered.connect(slot)
            menu.addAction(act)
            return act

        file_menu = self.menuBar().addMenu("&File")
        action(file_menu, "&New", self.new_project, QKeySequence.StandardKey.New)
        action(file_menu, "&Open...", self.open_project, QKeySequence.StandardKey.Open)
        self.recent_menu = file_menu.addMenu("Open &Recent")
        action(file_menu, "&Save", self.save_project, QKeySequence.StandardKey.Save)
        action(
            file_menu,
            "Save &As...",
            self.save_project_as,
            QKeySequence.StandardKey.SaveAs,
        )
        file_menu.addSeparator()
        action(file_menu, "&Import Media...", self.import_media, "Ctrl+I")
        file_menu.addSeparator()
        self.export_action = action(
            file_menu, "&Export...", self.export_project, "Ctrl+E"
        )
        file_menu.addSeparator()
        action(file_menu, "E&xit", self.close, QKeySequence.StandardKey.Quit)

        # The shortcuts that act on the timeline live on the window as actions
        # rather than in the view's key handler, so they appear in the menu and
        # work wherever the focus happens to be. The playhead nudges are the
        # exception, see keyPressEvent.
        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = action(
            edit_menu, "&Undo", self.undo, QKeySequence.StandardKey.Undo
        )
        self.redo_action = action(
            edit_menu, "&Redo", self.redo, QKeySequence.StandardKey.Redo
        )
        # Ctrl+Y as well as the platform default, which is Ctrl+Shift+Z on
        # some of them.
        self.redo_action.setShortcuts(
            [QKeySequence(QKeySequence.StandardKey.Redo), QKeySequence("Ctrl+Y")]
        )
        edit_menu.addSeparator()
        self.split_action = action(
            edit_menu, "&Split at playhead", self.split_selection, "S"
        )
        self.delete_action = action(
            edit_menu, "&Delete", self.delete_selection, QKeySequence.StandardKey.Delete
        )
        self.duplicate_action = action(
            edit_menu, "D&uplicate", self.duplicate_selection, "Ctrl+D"
        )
        edit_menu.addSeparator()
        self.mute_action = action(
            edit_menu, "Toggle track &mute", self.toggle_selected_track_mute, "M"
        )
        edit_menu.addSeparator()
        self.relink_action = action(
            edit_menu, "&Relink missing media...", self.relink_missing_media
        )

        play_menu = self.menuBar().addMenu("&Playback")
        action(play_menu, "&Play/Pause", self.preview.toggle_play, "Space")
        play_menu.addSeparator()
        action(play_menu, "Go to &start", self.playhead_to_start, "Home")
        action(play_menu, "Go to &end", self.playhead_to_end, "End")

        self._refresh_recent_menu()
        self._refresh_edit_actions()

    # -- the command stack ------------------------------------------------

    def run_command(self, command: Command) -> bool:
        """Apply an edit. The ONLY route by which the project changes.

        Nothing else in the UI may touch the model. A control that mutated a
        track directly would be invisible to undo, and would leave the project
        changed with nothing having marked it dirty.
        """
        if self._project is None:
            return False
        try:
            self.commands.push(command, self._project)
        except CommandError as exc:
            # An ordinary refusal: an overlap, a bad drop. The status bar
            # rather than a modal dialog, because it is the gesture that
            # failed and the user is still holding the mouse.
            self.statusBar().showMessage(str(exc), 6000)
            return False
        self._after_stack_change(command)
        self.statusBar().showMessage(command.label, 3000)
        return True

    def undo(self) -> None:
        if self._project is None:
            return
        command = self.commands.undo(self._project)
        if command is None:
            return
        self._after_stack_change(command)
        self.statusBar().showMessage(f"Undo {command.label}", 4000)

    def redo(self) -> None:
        if self._project is None:
            return
        command = self.commands.redo(self._project)
        if command is None:
            return
        self._after_stack_change(command)
        self.statusBar().showMessage(f"Redo {command.label}", 4000)

    def _after_stack_change(self, command: Command) -> None:
        """Bring the window up to date after a trip through the command stack.

        THE ONE CALLER OF mark_dirty(). Undo and redo arrive here as well as
        push, because undoing back past the last save leaves the file on disk
        just as out of date as a fresh edit does.

        The scene is rebuilt wholesale rather than patched. Crude on purpose:
        correctness is worth more per edit than a few milliseconds, and
        PHASES.md says to report it before optimising rather than quietly
        switching to targeted updates.
        """
        self.mark_dirty()
        self.timeline.set_project(self._project)
        # A relink resolves clips, an undo of one un-resolves them, and a drop
        # can add a clip pointing at a file that has since been deleted. All
        # three arrive here.
        self._refresh_missing_media()
        # Not set_project: the playhead stays where it is. What has to be
        # re-read is the duration the scrubber spans and the clip under the
        # playhead, which an edit may have moved out from under it.
        self.playback.project_changed()
        self._refresh_status()
        self._refresh_edit_actions()
        if command.touches_audio:
            # One line, and Phase 5 breaks silently without it: the bed would
            # go on playing audio the timeline no longer contains.
            self.audio_bed.invalidate()

    def _refresh_edit_actions(self) -> None:
        self.undo_action.setEnabled(self.commands.can_undo())
        self.redo_action.setEnabled(self.commands.can_redo())
        self.undo_action.setText(
            f"&Undo {self.commands.undo_label()}"
            if self.commands.can_undo()
            else "&Undo"
        )
        self.redo_action.setText(
            f"&Redo {self.commands.redo_label()}"
            if self.commands.can_redo()
            else "&Redo"
        )
        # Enabled only when there is something to relink, so the menu says
        # whether the project has a problem without opening anything.
        missing = len(self._missing_sources())
        self.relink_action.setEnabled(missing > 0)
        self.relink_action.setText(
            f"&Relink missing media... ({missing})"
            if missing
            else "&Relink missing media..."
        )
        selected = bool(self.timeline.selected_clip_ids())
        self.split_action.setEnabled(selected)
        self.delete_action.setEnabled(selected)
        self.duplicate_action.setEnabled(selected)
        self.mute_action.setEnabled(selected)

    def _on_gesture_rejected(self, message: str) -> None:
        self.statusBar().showMessage(message, 6000)

    # -- edits on the selection -------------------------------------------

    def _selected(self) -> list[tuple[str, str]]:
        """(track_id, clip_id) for every selected clip, in timeline order."""
        if self._project is None:
            return []
        wanted = set(self.timeline.selected_clip_ids())
        found: list[tuple[str, str]] = []
        for track in self._project.tracks:
            for clip in track.clips:
                if clip.id in wanted:
                    found.append((track.id, clip.id))
        return found

    def _run_batch(self, label: str, commands: list[Command]) -> bool:
        """One command, or several bundled so that one undo reverses the lot."""
        if not commands:
            return False
        if len(commands) == 1:
            return self.run_command(commands[0])
        return self.run_command(MacroCommand(label, commands))

    def split_selection(self) -> None:
        if self._project is None:
            return
        at = self.timeline.playhead_ticks()
        commands: list[Command] = []
        for track_id, clip_id in self._selected():
            clip = self._clip(track_id, clip_id)
            # Only the clips the playhead actually crosses. A selection that
            # spans the timeline usually includes some that it does not.
            if clip is not None and clip.timeline_start < at < clip.timeline_end:
                commands.append(SplitClip(track_id, clip_id, at))
        if not commands:
            self.statusBar().showMessage(
                "Nothing to split: the playhead is not inside a selected clip", 5000
            )
            return
        self._run_batch(f"Split {len(commands)} clips", commands)

    def delete_selection(self) -> None:
        selected = self._selected()
        self._run_batch(
            f"Delete {len(selected)} clips",
            [DeleteClip(track_id, clip_id) for track_id, clip_id in selected],
        )

    def duplicate_selection(self) -> None:
        selected = self._selected()
        self._run_batch(
            f"Duplicate {len(selected)} clips",
            [DuplicateClip(track_id, clip_id) for track_id, clip_id in selected],
        )

    def toggle_selected_track_mute(self) -> None:
        selected = self._selected()
        if not selected:
            self.statusBar().showMessage("Select a clip first", 4000)
            return
        track = self._track(selected[0][0])
        if track is not None:
            self.run_command(SetTrackMuted(track.id, not track.muted))

    def _clip(self, track_id: str, clip_id: str):
        track = self._track(track_id)
        if track is None:
            return None
        for clip in track.clips:
            if clip.id == clip_id:
                return clip
        return None

    def _track(self, track_id: str) -> Track | None:
        if self._project is None:
            return None
        for track in self._project.tracks:
            if track.id == track_id:
                return track
        return None

    # -- the playhead -----------------------------------------------------

    def keyPressEvent(self, event) -> None:
        """Playhead nudges.

        Handled here rather than as window-wide QActions so the arrow keys
        still move the selection in the media bin. A QAction shortcut takes
        the key from whatever has focus; an unhandled key press only reaches
        the window once nothing else has wanted it.
        """
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Left:
            self.nudge_playhead(-1, whole_second=shift)
        elif key == Qt.Key.Key_Right:
            self.nudge_playhead(1, whole_second=shift)
        elif key == Qt.Key.Key_Home:
            self.playhead_to_start()
        elif key == Qt.Key.Key_End:
            self.playhead_to_end()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def nudge_playhead(self, direction: int, whole_second: bool = False) -> None:
        """Move the playhead one frame, or one second, in ``direction``.

        A frame step converts out to the frame index and back rather than
        adding a per-frame constant. At a rate whose frame duration is not a
        whole number of ticks, adding accumulates error; going through the
        index cannot.
        """
        if self._project is None:
            return
        rate = self._project.frame_rate
        current = self.timeline.playhead_ticks()
        if whole_second:
            target = snap_to_frame(current + direction * TICKS_PER_SECOND, rate)
        else:
            target = frames_to_ticks(ticks_to_frames(current, rate) + direction, rate)
        self.move_playhead_to(target)

    def playhead_to_start(self) -> None:
        self.move_playhead_to(0)

    def playhead_to_end(self) -> None:
        if self._project is not None:
            self.move_playhead_to(self._project.duration)

    def move_playhead_to(self, ticks: int) -> None:
        """Put the playhead somewhere, from a shortcut or a menu.

        Both halves move together, under the same guard the two signal
        directions use, so a nudge seeks playback rather than sliding the
        marker away from the frame on screen.
        """
        ticks = max(0, int(ticks))
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        try:
            self.timeline.set_playhead(ticks)
            self.preview.seek(ticks)
        finally:
            self._syncing_playhead = False
        self._refresh_playhead_label()

    def _refresh_playhead_label(self) -> None:
        if self._project is None:
            self._status_playhead.setText("")
            return
        self._status_playhead.setText(
            format_utils.timecode(
                self.timeline.playhead_ticks(), self._project.frame_rate
            )
        )

    # -- what is remembered between runs -----------------------------------

    def _restore_layout(self) -> None:
        """Put the splitters and the zoom back where they were last time.

        Geometry is restored earlier, in the constructor, because a window has
        to be placed before it is shown. These do not: they are inside it.
        """
        top = self.settings.splitter(Settings.SPLIT_TOP)
        if top:
            self._top_splitter.setSizes(top)
        main = self.settings.splitter(Settings.SPLIT_MAIN)
        if main:
            self._main_splitter.setSizes(main)

        zoom = self.settings.zoom()
        if zoom is not None:
            self.timeline.set_zoom(zoom)

        if self.settings.maximised():
            self.showMaximized()

    def _save_layout(self) -> None:
        """Write the window's shape back. Called on close.

        The geometry saved is normalGeometry(), not geometry(): a maximised
        window reports the screen it fills, and restoring that as a normal
        window would open it edge to edge with no way to tell it had ever been
        anything else. The maximised flag is stored separately.
        """
        self.settings.set_geometry(self.normalGeometry())
        self.settings.set_maximised(self.isMaximized())
        self.settings.set_splitter(Settings.SPLIT_TOP, self._top_splitter.sizes())
        self.settings.set_splitter(Settings.SPLIT_MAIN, self._main_splitter.sizes())
        self.settings.set_zoom(self.timeline.zoom())
        self.settings.sync()

    def _refresh_recent_menu(self) -> None:
        """Rebuild File > Open Recent from the settings list."""
        self.recent_menu.clear()
        recent = self.settings.recent_files()
        if not recent:
            empty = QAction("No recent projects", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return

        for index, path in enumerate(recent, start=1):
            act = QAction(f"&{index}  {Path(path).name}", self)
            act.setToolTip(path)
            act.setData(path)
            # default=path, or every entry would close over the last one.
            act.triggered.connect(lambda _checked=False, p=path: self.open_recent(p))
            self.recent_menu.addAction(act)

        self.recent_menu.addSeparator()
        clear = QAction("Clear list", self)
        clear.triggered.connect(self.clear_recent_files)
        self.recent_menu.addAction(clear)

    def open_recent(self, path: str | Path) -> bool:
        """Open a project from the recent list.

        A file that has been deleted or moved is dropped from the list rather
        than left there to fail again: the entry is stale, and the user
        clicking it is how they found out.
        """
        path = Path(path)
        if not path.is_file():
            self.settings.forget_recent(path)
            self._refresh_recent_menu()
            show_error(
                self,
                "Project not found",
                f"{path.name} is no longer at that location.",
                str(path),
            )
            return False
        return self.load_project_file(path)

    def clear_recent_files(self) -> None:
        self.settings.set_recent_files([])
        self._refresh_recent_menu()

    def _remember_recent(self, path: Path) -> None:
        self.settings.remember_recent(path)
        self._refresh_recent_menu()

    # -- autosave ----------------------------------------------------------

    def autosave_path(self) -> Path | None:
        """Where this project would autosave, or None if it was never saved.

        An unsaved project has nowhere to put a sidecar: the autosave lives
        beside the project file and there is no project file. This is the one
        real hole in the recovery story and it is the one Phase 6 specifies.
        """
        if self._project_path is None:
            return None
        return autosave_path(self._project_path)

    @Slot()
    def autosave_now(self) -> bool:
        """Write the autosave sidecar if there is anything to write.

        Returns whether a file was written. Failure is reported in the status
        bar and nowhere else: an autosave is a safety net, and a modal dialog
        every two minutes because a folder went read-only would be worse than
        the thing it is warning about.
        """
        path = self.autosave_path()
        if path is None or self._project is None or not self._dirty:
            return False
        try:
            save(self._project, path)
        except (ProjectIOError, OSError) as exc:
            self.statusBar().showMessage(f"Autosave failed: {exc}", 6000)
            return False
        self.statusBar().showMessage(f"Autosaved {path.name}", 2000)
        return True

    def _discard_autosave(self) -> None:
        """Drop the sidecar. The work it held is now in the project file."""
        if self._project_path is not None:
            discard_autosave(self._project_path)

    def pending_recoveries(self) -> list[Path]:
        """Recently opened projects whose autosave is newer than they are.

        Only the recent list is searched. There is nowhere else to look: an
        autosave is a sidecar of a project file, and the application does not
        keep a register of every project that has ever existed.
        """
        return [
            Path(path)
            for path in self.settings.recent_files()
            if autosave_is_newer(Path(path))
        ]

    def offer_autosave_recovery(self) -> bool:
        """Ask about unsaved work left by a session that did not finish.

        Called after the window is shown, not during construction: it is a
        modal dialog, and a modal dialog raised by a constructor is a window
        that appears to hang before it has drawn itself.

        Returns whether a recovery was actually loaded.
        """
        for project_path in self.pending_recoveries():
            sidecar = autosave_path(project_path)
            if not confirm(
                self,
                "Recover unsaved work",
                f"{project_path.name} has an autosave that is newer than the "
                f"saved project.\n\nRecover it? Choosing No deletes the "
                f"autosave and leaves the saved project alone.",
            ):
                discard_autosave(project_path)
                continue

            if not self.load_project_file(sidecar, prompt=True):
                continue
            # Opened from the sidecar, but it IS the project: point the window
            # at the real file so Save overwrites that and not the autosave,
            # and mark it dirty, because the saved file is still the old one.
            #
            # The second and last caller of mark_dirty, and the only one that
            # is not a trip through the command stack. It has to be: the
            # unsaved work is the whole loaded model, not an edit that could
            # be pushed. Everything else in this window still gets its dirty
            # state by editing something.
            self._project_path = project_path
            self.mark_dirty()
            self._refresh_status()
            self.statusBar().showMessage(
                f"Recovered unsaved work in {project_path.name}. Save to keep it.",
                10000,
            )
            return True
        return False

    # -- missing media -----------------------------------------------------

    def _missing_sources(self) -> list[MissingSource]:
        return self._missing

    def _refresh_missing_media(self) -> None:
        """Re-check the project's sources and tell the timeline what it found."""
        self._missing = missing_sources(self._project)
        self.timeline.set_missing_clip_ids(missing_clip_ids(self._project))

    def relink_missing_media(self) -> bool:
        """Offer to point missing sources at files that do exist.

        The dialog answers in files; the command works in clip ids. The
        conversion is :func:`core.media_check.relink_map`, and it is the only
        route: a relink addressed by track and clip index would land on the
        wrong clip whenever model order and lane order disagree, which they do
        as soon as a video track has been removed and re-added.
        """
        if self._project is None:
            return False
        if not self._missing:
            self.statusBar().showMessage("No missing media in this project", 4000)
            return False

        dialog = RelinkDialog(self._missing, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        replacements = dialog.replacements()
        if not replacements:
            return False

        by_clip_id = relink_map(self._project, replacements)
        if not by_clip_id:
            return False
        if not self.run_command(RelinkMedia(by_clip_id)):
            return False

        # Whatever was relinked now has a file; anything left does not.
        still_missing = len(self._missing)
        self.statusBar().showMessage(
            f"Relinked {len(by_clip_id)} clip(s)"
            + (f", {still_missing} file(s) still missing" if still_missing else ""),
            6000,
        )
        return True

    # -- project ----------------------------------------------------------

    def new_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        project = Project(
            name="Untitled",
            tracks=[
                Track(name="V1", kind="video"),
                Track(name="A1", kind="audio"),
            ],
        )
        self._set_project(project, None)

    def open_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", PROJECT_FILTER
        )
        if not path:
            return
        self.load_project_file(Path(path), prompt=False)

    def load_project_file(self, path: Path, prompt: bool = True) -> bool:
        """Open a project from a known path. Used by File > Open and by drops."""
        if prompt and not self._confirm_discard_changes():
            return False
        try:
            project = load(path)
        except (ProjectIOError, ValueError, OSError) as exc:
            show_error(self, "Could not open project", *split_error(exc))
            return False
        self._set_project(project, path)
        self.statusBar().showMessage(f"Opened {path.name}", 4000)
        # Checked on load, as Phase 6 asks, and offered straight away: a
        # project whose media has moved is unusable until it is answered, and
        # burying that in a menu would leave the user staring at hatched clips.
        if self._missing:
            self.relink_missing_media()
        return True

    def is_dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        """The one place unsaved-changes state is turned on.

        Called from two places and no others. :meth:`_after_stack_change` is
        where every trip through the command stack ends up, and it is how
        every edit in the application becomes unsaved work. No control marks
        the project dirty by choosing to: it is dirty because something
        undoable happened to it, so a control added later cannot forget.

        :meth:`offer_autosave_recovery` is the exception, and is one because
        the unsaved work it loads is the entire model rather than an edit:
        there is no command to push, and the project genuinely differs from
        the file on disk the moment it opens.

        tests/test_editing.py checks all of that with an AST walk over this
        file.
        """
        self._dirty = True

    def _confirm_discard_changes(self) -> bool:
        """Ask before throwing away unsaved edits. True means carry on.

        Phase 6 owns the project lifecycle properly, including prompting on
        close and autosave recovery. This is the minimum needed so that
        dropping a project file cannot silently discard work.
        """
        if not self._dirty or self._project is None:
            return True

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save changes to {self._project.name} first?")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        answer = box.exec()

        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            self.save_project()
            return not self._dirty
        return True

    def save_project(self) -> None:
        if self._project is None:
            return
        if self._project_path is None:
            self.save_project_as()
            return
        self._write_project(self._project_path)

    def save_project_as(self) -> None:
        if self._project is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save project as",
            f"{self._project.name}{PROJECT_EXTENSION}",
            PROJECT_FILTER,
        )
        if not path:
            return
        # A name typed without any extension gets the project one, so it is
        # still visible to the Open dialog's filter next time.
        self._write_project(with_project_extension(Path(path)))

    def _write_project(self, path: Path) -> None:
        try:
            save(self._project, path)
        except (ProjectIOError, OSError) as exc:
            show_error(self, "Could not save project", *split_error(exc))
            return
        self._project_path = path
        self._dirty = False
        # The sidecar held work that is now in the project file. Leaving it
        # would make the next startup offer to recover something older than
        # what is already on disk.
        self._discard_autosave()
        self._remember_recent(path)
        self._refresh_status()
        self.statusBar().showMessage(f"Saved {path.name}", 4000)

    def _set_project(self, project: Project, path: Path | None) -> None:
        self._project = project
        self._project_path = path
        self._dirty = False
        self._refresh_missing_media()
        if path is not None:
            self._remember_recent(path)
        # The history belongs to the project that was open. Keeping it would
        # let Ctrl+Z apply the inverse of an edit to a different model.
        self.commands.clear()
        self.preview.set_project(project)
        self.timeline.set_project(project)
        # Loading a project invalidates the bed, same as any audio edit.
        self.audio_bed.set_project(project)
        self._refresh_status()
        self._refresh_edit_actions()
        self.move_playhead_to(0)

    # -- track edits ------------------------------------------------------

    def _on_track_muted(self, track_id: str, muted: bool) -> None:
        """Mute toggle in the track header."""
        track = self._track(track_id)
        if track is None or track.muted == muted:
            return
        if not self.run_command(SetTrackMuted(track_id, muted)):
            # Put the checkbox back where the model still says it should be.
            self.timeline.set_project(self._project)

    def _on_add_track(self, kind: str) -> None:
        """Add-track buttons in the header.

        AddTrack carries the refusal of a second video track. The button is
        already disabled when one exists; this is the same rule reached the
        other way, and a dialog rather than a status message because the user
        pressed a button and deserves to be told why nothing happened.
        """
        if self._project is None:
            return
        if kind == "video" and self._project.video_tracks():
            show_error(
                self,
                "Cannot add a video track",
                "Multiple video tracks require compositing, "
                "not supported in this version.",
            )
            return

        existing = len(
            self._project.video_tracks()
            if kind == "video"
            else self._project.audio_tracks()
        )
        prefix = "V" if kind == "video" else "A"
        self.run_command(AddTrack(kind, f"{prefix}{existing + 1}"))

    @Slot()
    def _on_scrub_started(self) -> None:
        """The playhead was grabbed. Playback stops and stays stopped.

        Held inside the re-entrancy guard: pausing calls back into the panel
        to change the transport icon, and this is exactly the path where a
        careless handler would turn one gesture into a second seek.
        """
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        try:
            self.playback.begin_scrub()
        finally:
            self._syncing_playhead = False

    @Slot("qint64")
    def _on_playhead_scrubbed(self, ticks: int) -> None:
        """A position mid-drag. The picture follows; the bed does not move.

        Straight to the controller rather than through PreviewPanel.seek: the
        panel's seek is the full one, which would move the bed on every mouse
        event. The panel still ends up in step, because the controller reports
        the position back to it.
        """
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        try:
            self.playback.scrub_to(ticks)
        finally:
            self._syncing_playhead = False
        self._refresh_playhead_label()

    @Slot("qint64")
    def _on_scrub_finished(self, ticks: int) -> None:
        """The drag ended. Align the bed, once, and leave it paused."""
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        try:
            self.playback.end_scrub(ticks)
        finally:
            self._syncing_playhead = False
        self._refresh_playhead_label()

    @Slot("qint64")
    def _on_preview_position(self, ticks: int) -> None:
        """Playback moved. The playhead follows playback."""
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        try:
            self.timeline.set_playhead(ticks)
        finally:
            self._syncing_playhead = False
        self._refresh_playhead_label()

    @Slot(str, float, float)
    def _on_timeline_fitted(
        self, outcome: str, pixels_per_second: float, visible: float
    ) -> None:
        """Say what Shift+Z managed, rather than appearing to do nothing."""
        if outcome == FitOutcome.EMPTY:
            message = "Nothing to fit: the timeline is empty"
        elif outcome == FitOutcome.CLAMPED:
            message = (
                f"Fit to window: project is longer than the minimum zoom allows, "
                f"showing {visible:.0%} of it at {pixels_per_second:g} px/s"
            )
        else:
            message = (
                f"Fit to window: whole project visible at {pixels_per_second:.3g} px/s"
            )
        self.statusBar().showMessage(message, 6000)

    # -- audio bed --------------------------------------------------------

    def _on_bed_ready(self, path: str) -> None:
        """A bed landed. Hand it to playback; it takes over the clock at once.

        The preview's status line belongs to the controller now: it is the
        thing that knows whether the bed is actually driving playback, and two
        writers to one label would fight.
        """
        self.playback.set_bed(Path(path))
        self.statusBar().showMessage("Audio bed rendered", 4000)

    def _on_bed_failed(self, message: str) -> None:
        # Playback carries on, silently, on the elapsed timer.
        self.playback.set_bed(None)
        self.statusBar().showMessage(f"Audio bed failed: {message}", 8000)

    def _on_bed_invalidated(self) -> None:
        self.playback.invalidate_bed()

    def _on_bed_unavailable(self) -> None:
        self.playback.set_bed(None)

    def _refresh_status(self) -> None:
        if self._project is None:
            return
        name = self._project.name
        if self._project_path is not None:
            name = f"{name}  ({self._project_path.name})"
        self._status_name.setText(name)
        self._status_duration.setText(
            format_utils.timecode(self._project.duration, self._project.frame_rate)
        )
        self._status_rate.setText(
            f"{self._project.width}x{self._project.height}  "
            f"{format_utils.frame_rate_text(self._project.frame_rate)}"
        )

    # -- media bin --------------------------------------------------------

    def import_media(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import media", "", MEDIA_FILTER
        )
        if paths:
            self.import_media_paths(Path(p) for p in paths)

    def import_media_paths(self, paths: Iterable[Path]) -> int:
        """Add files to the bin, probing them in the background.

        Every row appears at once with its filename and a placeholder, then
        fills in as its probe returns. Twenty files therefore cost twenty
        background ffprobe calls and no frozen window.

        Returns how many rows were actually created; files already in the bin
        are skipped rather than added twice.
        """
        fresh: list[Path] = []
        for path in paths:
            key = self._media_key(path)
            if key in self._bin_keys:
                continue
            self._bin_keys.add(key)
            fresh.append(path)
            self._create_bin_row(path)

        if fresh:
            self._probes.submit(fresh)
        return len(fresh)

    @staticmethod
    def _media_key(path: Path) -> str:
        """One identity per file, so a drop cannot add the same file twice."""
        return os.path.normcase(str(Path(path).absolute()))

    def _bin_row(self, key: str) -> QListWidgetItem | None:
        for row in range(self.media_list.count()):
            item = self.media_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                return item
        return None

    def _create_bin_row(self, path: Path) -> QListWidgetItem:
        """A row that exists before anything is known about the file."""
        item = QListWidgetItem(f"{Path(path).name}\nReading...")
        item.setData(Qt.ItemDataRole.UserRole, self._media_key(path))
        item.setToolTip(str(path))
        item.setIcon(QIcon(poster.placeholder_pixmap()))
        self.media_list.addItem(item)
        return item

    def _add_media(self, info: MediaInfo) -> None:
        """Record a completed probe and fill in its row, creating it if needed."""
        key = self._media_key(info.path)
        self._bin_keys.add(key)
        self._media[key] = info
        item = self._bin_row(key) or self._create_bin_row(info.path)
        self._fill_bin_row(item, info)

    def _fill_bin_row(self, item: QListWidgetItem, info: MediaInfo) -> None:
        item.setText(
            f"{format_utils.media_title(info)}\n{format_utils.media_summary(info)}"
        )
        item.setToolTip(str(info.path))

        if not info.has_video:
            item.setIcon(QIcon(poster.audio_glyph_pixmap()))
            return

        tick = poster.poster_tick(info)
        frame = self._thumbnails.peek(info.path, tick)
        if frame is None:
            item.setIcon(QIcon(poster.placeholder_pixmap()))
            self._thumbnails.request(info.path, tick)
        else:
            item.setIcon(QIcon(poster.fit_poster(frame)))

    def _mark_bin_row_failed(self, path: str, reason: str) -> None:
        item = self._bin_row(self._media_key(Path(path)))
        if item is None:
            return
        item.setText(f"{Path(path).name}\nCould not be read")
        item.setToolTip(f"{path}\n{reason}")

    @Slot(str, object)
    def _on_probe_finished(self, path: str, info: MediaInfo) -> None:
        self._add_media(info)

    @Slot(str, str)
    def _on_probe_failed(self, path: str, reason: str) -> None:
        self._mark_bin_row_failed(path, reason)

    @Slot(int, object)
    def _on_probe_batch_finished(self, _batch: int, failures: list) -> None:
        """One summary for the whole drop, never a dialog per bad file."""
        if not failures:
            return
        for path, _reason in failures:
            key = self._media_key(Path(path))
            self._bin_keys.discard(key)
            item = self._bin_row(key)
            if item is not None:
                self.media_list.takeItem(self.media_list.row(item))

        detail = "\n\n".join(f"{Path(p).name}\n{reason}" for p, reason in failures)
        show_error(
            self,
            "Some files could not be imported",
            f"{len(failures)} file(s) could not be read and were skipped.",
            detail,
        )

    def _on_bin_thumbnail(self, src: str, tick: int) -> None:
        """A poster frame arrived. Swap it into whichever rows wanted it."""
        for row in range(self.media_list.count()):
            item = self.media_list.item(row)
            info = self._media.get(item.data(Qt.ItemDataRole.UserRole))
            if info is None or not info.has_video or str(info.path) != src:
                continue
            frame = self._thumbnails.peek(info.path, poster.poster_tick(info))
            if frame is not None:
                item.setIcon(QIcon(poster.fit_poster(frame)))

    def _bin_drag_payload(self, item: QListWidgetItem) -> bytes | None:
        """What a row carries when it is dragged to the timeline.

        None while the file is still being probed: there is no duration to
        make a clip out of yet, so the row simply does not drag.
        """
        info = self._media.get(item.data(Qt.ItemDataRole.UserRole))
        if info is None:
            return None
        return media_payload(
            info.path, info.duration_ticks, info.has_video, info.has_audio
        )

    def _media_duration(self, src) -> int | None:
        """How long a source file is, when the bin knows.

        The trim handles use this as the right-hand bound. A project opened
        from disk has clips whose sources were never probed in this session,
        and there the answer is None; see clamp_trim_right.
        """
        info = self._media.get(self._media_key(Path(src)))
        return info.duration_ticks if info is not None else None

    def _on_media_activated(self, item: QListWidgetItem) -> None:
        """Double-click in the bin.

        The preview plays the TIMELINE now, so there is no longer such a thing
        as previewing a file that is not on it. Rather than leave the gesture
        silently dead, say what to do with the file instead.
        """
        info = self._media.get(item.data(Qt.ItemDataRole.UserRole))
        if info is None:
            # Still being probed.
            return
        self.statusBar().showMessage(
            f"Drag {info.path.name} onto a track to use it", 4000
        )

    # -- drag and drop ----------------------------------------------------

    def handle_dropped_paths(self, paths: Iterable[Path]) -> None:
        """Route a drop: project files are opened, everything else imported."""
        paths = [Path(p) for p in paths]
        projects = [p for p in paths if is_project_path(p)]
        media = [p for p in paths if not is_project_path(p)]

        if projects:
            self.load_project_file(projects[0])
            if len(projects) > 1:
                self.statusBar().showMessage(
                    f"Opened {projects[0].name}; "
                    f"{len(projects) - 1} other project file(s) ignored",
                    6000,
                )

        if media:
            added = self.import_media_paths(media)
            skipped = len(media) - added
            message = f"Importing {added} file(s)"
            if skipped:
                message += f", {skipped} already in the bin"
            self.statusBar().showMessage(message, 4000)

    def dragEnterEvent(self, event) -> None:
        if has_droppable_files(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            set_drop_highlight(self._drop_frame, True)
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        if has_droppable_files(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        set_drop_highlight(self._drop_frame, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        set_drop_highlight(self._drop_frame, False)
        paths = dropped_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.acceptProposedAction()
        self.handle_dropped_paths(paths)

    # -- export -----------------------------------------------------------

    def export_project(self) -> None:
        if self._project is None or self._render_thread is not None:
            return

        # Validate on the GUI thread first. Building the graph is pure string
        # work and takes microseconds, and every RenderError the user can
        # provoke by editing (no video track, a second video track carrying
        # clips, an empty project) is raised here. Catching it now means a
        # plain dialog instead of a progress bar that flashes up and dies.
        #
        # Against the REAL project, before anything is dropped: these are
        # complaints about how the timeline is arranged, and the user is
        # looking at the timeline. Validating the trimmed copy first would
        # answer "there are two video tracks" with "there are no clips".
        try:
            build_full(self._project)
        except RenderError as exc:
            show_error(self, "Cannot export", *split_error(exc))
            return

        # Clips whose source has gone are dropped next. FFmpeg's answer to a
        # missing input is an error about a file it could not open, several
        # hundred lines into a graph the user did not write; saying so first,
        # and rendering the rest, is the behaviour Phase 6 asks for and the
        # only one that is any use.
        exportable, skipped = without_missing_clips(self._project)
        try:
            build_full(exportable)
        except RenderError as exc:
            # Structurally fine a moment ago, so what is left cannot be
            # exported because of what was taken out.
            if skipped:
                show_error(
                    self,
                    "Cannot export",
                    "Every clip that would be exported has a missing source "
                    "file. Relink them from the Edit menu.",
                    "\n".join(sorted(set(skipped))),
                )
            else:
                show_error(self, "Cannot export", *split_error(exc))
            return

        dialog = ExportDialog(
            exportable,
            self,
            hardware_available=self.encoder_probe.available(),
            suggested_path=self._suggested_export_path(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        request = dialog.request()
        if request is None:
            return

        if skipped and not self._confirm_skipping(skipped):
            return

        self.start_export(exportable, request)

    def _suggested_export_path(self) -> Path:
        """Beside the project file, named after it. Otherwise just a name."""
        name = f"{self._project.name}.mp4"
        if self._project_path is not None:
            return self._project_path.with_suffix(".mp4")
        return Path(name)

    def _confirm_skipping(self, skipped: list[str]) -> bool:
        """Say what will be missing from the file before making it."""
        names = ", ".join(sorted(set(skipped))[:5])
        return confirm(
            self,
            "Some clips will be skipped",
            f"{len(skipped)} clip(s) have no source file and will be left out "
            f"of the export:\n\n{names}\n\nExport anyway?",
        )

    def start_export(self, project: Project, request: ExportRequest) -> None:
        """Run one export. Everything chosen is already in ``request``."""
        if self._render_thread is not None:
            return

        self._render_cancel = threading.Event()
        self._render_clock.restart()

        dialog = QProgressDialog("Starting ffmpeg...", "Cancel", 0, 1000, self)
        dialog.setWindowTitle("Exporting")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        dialog.canceled.connect(self._on_export_cancelled)
        self._render_dialog = dialog

        worker = _RenderWorker(
            project,
            request.out_path,
            self._render_cancel,
            crf=request.crf,
            encoder=request.encoder,
            out_size=request.out_size,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_export_progress)
        worker.finished.connect(self._on_export_finished)
        worker.failed.connect(self._on_export_failed)

        self._render_worker = worker
        self._render_thread = thread
        thread.start()
        dialog.show()

    @Slot(float, float)
    def _on_export_progress(self, done_sec: float, total_sec: float) -> None:
        if self._render_dialog is None:
            return
        if total_sec > 0:
            self._render_dialog.setValue(round(done_sec / total_sec * 1000))
        self._render_dialog.setLabelText(
            format_utils.export_progress(
                done_sec, total_sec, self._render_clock.elapsed() / 1000
            )
        )

    @Slot()
    def _on_export_cancelled(self) -> None:
        if self._render_cancel is not None:
            self._render_cancel.set()
        if self._render_dialog is not None:
            self._render_dialog.setLabelText("Cancelling...")

    @Slot()
    def _on_export_finished(self) -> None:
        cancelled = self._render_cancel is not None and self._render_cancel.is_set()
        self._teardown_export()
        self.statusBar().showMessage(
            "Export cancelled" if cancelled else "Export complete", 6000
        )

    @Slot(str, str)
    def _on_export_failed(self, message: str, detail: str) -> None:
        self._teardown_export()
        show_error(self, "Export failed", message, detail)

    def _teardown_export(self) -> None:
        if self._render_dialog is not None:
            self._render_dialog.close()
            self._render_dialog.deleteLater()
            self._render_dialog = None
        if self._render_thread is not None:
            self._render_thread.quit()
            self._render_thread.wait()
            self._render_thread.deleteLater()
            self._render_thread = None
        if self._render_worker is not None:
            self._render_worker.deleteLater()
            self._render_worker = None
        self._render_cancel = None

    # -- shutdown ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Ask about unsaved work before tearing anything down.

        The prompt comes first and nothing is stopped until it is answered,
        so cancelling leaves a window that still has its export running, its
        bed worker alive and its preview loaded. Tearing down and then asking
        would leave a half dead window if the answer was Cancel.
        """
        if not self._confirm_discard_changes():
            event.ignore()
            return

        self._autosave_timer.stop()
        self._save_layout()
        # A clean exit means the autosave has nothing left to recover: either
        # the work was saved or the user chose to discard it. Leaving the
        # sidecar behind would offer it back on the next launch.
        self._discard_autosave()

        if self._render_cancel is not None:
            self._render_cancel.set()
        self._teardown_export()
        self.playback.shutdown()
        self.audio_bed.shutdown()
        self.audio_bed.discard_bed()
        super().closeEvent(event)
