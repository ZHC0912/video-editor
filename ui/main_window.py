"""The application shell.

Menus, the media bin, the preview panel, a placeholder where the timeline goes
in Phase 3, and the export job.

Nothing long running happens on the GUI thread. Probing is fast enough to be
synchronous; rendering is not, and runs on a QThread.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressDialog,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from core.filtergraph import RenderError, build_full
from core.model import MediaInfo, Project, Track
from core.probe import ProbeError, probe
from core.project_io import ProjectIOError, load, save
from core.render import render
from ui import format_utils, theme, window_geometry
from ui.dialogs import show_error, split_error
from ui.preview_panel import PreviewPanel
from ui.single_player_controller import SinglePlayerController

__all__ = ["MainWindow"]

MEDIA_FILTER = (
    "Media files (*.mp4 *.mov *.mkv *.avi *.m4v *.webm *.wav *.mp3 *.aac *.flac);;"
    "All files (*)"
)
PROJECT_FILTER = "VidEditor project (*.vidproj);;All files (*)"


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
        self._media: list[MediaInfo] = []

        self._render_thread: QThread | None = None
        self._render_worker: _RenderWorker | None = None
        self._render_cancel: threading.Event | None = None
        self._render_dialog: QProgressDialog | None = None

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
        self.media_list = QListWidget(self)
        self.media_list.setAlternatingRowColors(False)
        self.media_list.itemDoubleClicked.connect(self._on_media_activated)

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
        bin_panel.setMinimumWidth(240)

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
        top.setSizes([280, 1100])

        # Phase 3 replaces this with the QGraphicsView timeline.
        self.timeline_placeholder = QFrame(self)
        self.timeline_placeholder.setProperty("surface", "timeline")
        placeholder_layout = QVBoxLayout(self.timeline_placeholder)
        placeholder_label = QLabel("Timeline arrives in Phase 3", self)
        placeholder_label.setProperty("muted", True)
        placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(placeholder_label)
        self.timeline_placeholder.setMinimumHeight(180)

        split = QSplitter(Qt.Orientation.Vertical, self)
        split.addWidget(top)
        split.addWidget(self.timeline_placeholder)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([560, 260])

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(split)
        self.setCentralWidget(container)

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
        project = Project(
            name="Untitled",
            tracks=[
                Track(name="V1", kind="video"),
                Track(name="A1", kind="audio"),
            ],
        )
        self._set_project(project, None)

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", PROJECT_FILTER
        )
        if not path:
            return
        try:
            project = load(Path(path))
        except (ProjectIOError, ValueError, OSError) as exc:
            show_error(self, "Could not open project", *split_error(exc))
            return
        self._set_project(project, Path(path))

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
            self, "Save project as", f"{self._project.name}.vidproj", PROJECT_FILTER
        )
        if not path:
            return
        self._write_project(Path(path))

    def _write_project(self, path: Path) -> None:
        try:
            save(self._project, path)
        except (ProjectIOError, OSError) as exc:
            show_error(self, "Could not save project", *split_error(exc))
            return
        self._project_path = path
        self._refresh_status()
        self.statusBar().showMessage(f"Saved {path.name}", 4000)

    def _set_project(self, project: Project, path: Path | None) -> None:
        self._project = project
        self._project_path = path
        self.preview.set_project(project)
        self._refresh_status()

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
        failures: list[str] = []
        for raw in paths:
            try:
                info = probe(Path(raw))
            except ProbeError as exc:
                failures.append(f"{Path(raw).name}\n{split_error(exc)[0]}")
                continue
            self._add_media(info)

        if failures:
            show_error(
                self,
                "Some files could not be imported",
                f"{len(failures)} of {len(paths)} files could not be read.",
                "\n\n".join(failures),
            )

    def _add_media(self, info: MediaInfo) -> None:
        self._media.append(info)
        item = QListWidgetItem(
            f"{format_utils.media_title(info)}\n{format_utils.media_summary(info)}"
        )
        item.setToolTip(str(info.path))
        item.setData(Qt.ItemDataRole.UserRole, len(self._media) - 1)
        self.media_list.addItem(item)

    def _on_media_activated(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        info = self._media[index]
        self.player.load(info.path)
        self.player.play()

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
        if self._render_cancel is not None:
            self._render_cancel.set()
        self._teardown_export()
        self.player.clear()
        super().closeEvent(event)
