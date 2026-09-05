"""The application shell.

Menus, the media bin, the preview panel, a placeholder where the timeline goes
in Phase 3, and the export job.

Nothing long running happens on the GUI thread. Probing is fast enough to be
synchronous; rendering is not, and runs on a QThread.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from core.filtergraph import RenderError, build_full
from core.model import MediaInfo, Project, Track
from core.probe import ProbeError, probe
from core.project_io import (
    PROJECT_EXTENSION,
    PROJECT_FORMAT_NAME,
    ProjectIOError,
    is_project_path,
    load,
    save,
    with_project_extension,
)
from core.render import render
from ui import format_utils, poster, theme, window_geometry
from ui.dialogs import show_error, split_error
from ui.media_bin import MediaBinList, has_droppable_files, dropped_paths, set_drop_highlight
from ui.preview_panel import PreviewPanel
from ui.single_player_controller import SinglePlayerController
from ui.timeline.timeline_view import FitOutcome, TimelinePanel
from ui.workers.audio_bed_worker import AudioBedWorker
from ui.workers.probe_worker import ProbeQueue
from ui.workers.thumbnail_worker import shared_thumbnail_cache

__all__ = ["MainWindow"]

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
        self, project: Project, out_path: Path, cancel: threading.Event
    ) -> None:
        super().__init__()
        self._project = project
        self._out_path = out_path
        self._cancel = cancel

    @Slot()
    def run(self) -> None:
        try:
            render(
                self._project,
                self._out_path,
                self._cancel,
                on_progress=self.progress.emit,
            )
        except RenderError as exc:
            self.failed.emit(*split_error(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a dialog, never a traceback
            self.failed.emit(f"Export failed: {exc}", "")
            return
        self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VidEditor")
        self.apply_geometry()

        self._project: Project | None = None
        self._project_path: Path | None = None
        self._dirty = False
        # Bin contents, keyed by normalised path. Rows exist before their
        # probe returns, so a key may be present with no MediaInfo yet.
        self._media: dict[str, MediaInfo] = {}

        self._render_thread: QThread | None = None
        self._render_worker: _RenderWorker | None = None
        self._render_cancel: threading.Event | None = None
        self._render_dialog: QProgressDialog | None = None

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
        self.media_list = MediaBinList(self.handle_dropped_paths, self)
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
        bin_hint = QLabel("Double-click to preview", bin_panel)
        bin_hint.setProperty("muted", True)
        bin_layout.addWidget(bin_title)
        bin_layout.addWidget(self.media_list, 1)
        bin_layout.addWidget(bin_hint)
        # Wide enough for a poster plus two lines of metadata beside it.
        bin_panel.setMinimumWidth(330)

        self.preview = PreviewPanel(self)
        self.player = SinglePlayerController(self.preview.stage, self)
        self.preview.set_controller(self.player)

        preview_panel = QFrame(self)
        preview_panel.setProperty("surface", "panel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        preview_layout.addWidget(self.preview)

        top = QSplitter(Qt.Orientation.Horizontal, self)
        top.addWidget(bin_panel)
        top.addWidget(preview_panel)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setSizes([360, 1040])

        self.timeline = TimelinePanel(self)
        self.timeline.mute_toggled.connect(self._on_track_muted)
        self.timeline.add_track_requested.connect(self._on_add_track)
        self.timeline.fitted.connect(self._on_timeline_fitted)
        self.preview.position_changed.connect(self.timeline.set_playhead)

        timeline_frame = QFrame(self)
        timeline_frame.setProperty("surface", "timeline")
        timeline_layout = QVBoxLayout(timeline_frame)
        timeline_layout.setContentsMargins(4, 4, 4, 4)
        timeline_layout.addWidget(self.timeline)
        timeline_frame.setMinimumHeight(180)

        split = QSplitter(Qt.Orientation.Vertical, self)
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
        self._status_duration = QLabel("", self)
        self._status_duration.setFont(theme.mono_font())
        self._status_rate = QLabel("", self)

        status = QStatusBar(self)
        status.addWidget(self._status_name)
        status.addPermanentWidget(self._status_duration)
        status.addPermanentWidget(self._status_rate)
        self.setStatusBar(status)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        def action(text: str, slot, shortcut: QKeySequence | str | None = None):
            act = QAction(text, self)
            if shortcut is not None:
                act.setShortcut(shortcut)
            act.triggered.connect(slot)
            file_menu.addAction(act)
            return act

        action("&New", self.new_project, QKeySequence.StandardKey.New)
        action("&Open...", self.open_project, QKeySequence.StandardKey.Open)
        action("&Save", self.save_project, QKeySequence.StandardKey.Save)
        action("Save &As...", self.save_project_as, QKeySequence.StandardKey.SaveAs)
        file_menu.addSeparator()
        action("&Import Media...", self.import_media, "Ctrl+I")
        file_menu.addSeparator()
        self.export_action = action("&Export...", self.export_project, "Ctrl+E")
        file_menu.addSeparator()
        action("E&xit", self.close, QKeySequence.StandardKey.Quit)

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
        return True

    def is_dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        """The one place unsaved-changes state is turned on.

        Phase 3's track controls call this directly, because they mutate the
        project directly. In Phase 4 that stops: CommandStack.push becomes the
        only caller, so anything undoable marks the project dirty by virtue of
        being a command, and no new control can forget to. The direct calls
        below disappear along with the direct mutations they sit in.
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
        self._refresh_status()
        self.statusBar().showMessage(f"Saved {path.name}", 4000)

    def _set_project(self, project: Project, path: Path | None) -> None:
        self._project = project
        self._project_path = path
        self._dirty = False
        self.preview.set_project(project)
        self.timeline.set_project(project)
        # Loading a project invalidates the bed, same as any audio edit.
        self.audio_bed.set_project(project)
        self._refresh_status()

    # -- track edits ------------------------------------------------------

    def _on_track_muted(self, track_id: str, muted: bool) -> None:
        """Mute toggle in the track header.

        Phase 4 routes this through a SetTrackMuted command so it can be
        undone. For now it is a direct change, because the header control is
        part of this phase's layout and a dead toggle would be worse than a
        temporary one.
        """
        if self._project is None:
            return
        for track in self._project.tracks:
            if track.id == track_id:
                if track.muted == muted:
                    return
                track.muted = muted
                # Phase 4 replaces this with SetTrackMuted going through the
                # command stack, which marks the project dirty for us.
                self.mark_dirty()
                break
        else:
            return

        self.timeline.set_project(self._project)
        self._refresh_status()
        if self._track_kind(track_id) == "audio":
            self.audio_bed.invalidate()

    def _on_add_track(self, kind: str) -> None:
        """Add-track buttons in the header.

        Phase 4 replaces this with the AddTrack command, which carries the
        same refusal. The video button is already disabled when a video track
        exists; this check is the second line of the same rule.
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
        self._project.tracks.append(
            Track(name=f"{prefix}{existing + 1}", kind=kind)
        )
        # Phase 4 replaces this with the AddTrack command.
        self.mark_dirty()
        self.timeline.set_project(self._project)
        if kind == "audio":
            self.audio_bed.invalidate()

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

    def _track_kind(self, track_id: str) -> str | None:
        if self._project is None:
            return None
        for track in self._project.tracks:
            if track.id == track_id:
                return track.kind
        return None

    # -- audio bed --------------------------------------------------------

    def _on_bed_ready(self, path: str) -> None:
        self.preview.set_status(f"Audio bed ready: {Path(path).name}")
        self.statusBar().showMessage("Audio bed rendered", 4000)

    def _on_bed_failed(self, message: str) -> None:
        self.preview.set_status("Audio bed failed")
        self.statusBar().showMessage(f"Audio bed failed: {message}", 8000)

    def _on_bed_invalidated(self) -> None:
        if self._project is not None and self._project.has_audio:
            self.preview.set_status("Audio bed rendering...")

    def _on_bed_unavailable(self) -> None:
        self.preview.set_status("")

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

    def _on_media_activated(self, item: QListWidgetItem) -> None:
        info = self._media.get(item.data(Qt.ItemDataRole.UserRole))
        if info is None:
            # Still being probed. Nothing to play yet.
            return
        self.player.load(info.path)
        self.player.play()

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
        try:
            build_full(self._project)
        except RenderError as exc:
            show_error(self, "Cannot export", *split_error(exc))
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export video", f"{self._project.name}.mp4", "MP4 video (*.mp4)"
        )
        if not path:
            return

        self._render_cancel = threading.Event()

        dialog = QProgressDialog("Starting ffmpeg...", "Cancel", 0, 1000, self)
        dialog.setWindowTitle("Exporting")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        dialog.canceled.connect(self._on_export_cancelled)
        self._render_dialog = dialog

        worker = _RenderWorker(self._project, Path(path), self._render_cancel)
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
            format_utils.elapsed_of_total(done_sec, total_sec)
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

        if self._render_cancel is not None:
            self._render_cancel.set()
        self._teardown_export()
        self.audio_bed.shutdown()
        self.audio_bed.discard_bed()
        self.player.clear()
        super().closeEvent(event)
