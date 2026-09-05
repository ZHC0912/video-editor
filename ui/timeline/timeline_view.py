"""The timeline widget: a scrolling view plus a fixed track header column.

The header column is a real QWidget beside the view rather than items inside
the scene. Track names and mute toggles are ordinary controls, and keeping them
out of the scene means they never scroll horizontally, never scale with zoom,
and never need hit-testing against clips. It follows the view's vertical
scrollbar so the headers stay level with their lanes.
"""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QPainter, QWheelEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.model import Project
from core.timebase import ticks_to_seconds
from ui import theme
from ui.timeline.timeline_scene import (
    MAX_PIXELS_PER_SECOND,
    MIN_PIXELS_PER_SECOND,
    TimelineScene,
)

__all__ = [
    "TimelinePanel",
    "TimelineView",
    "TrackHeaderColumn",
    "FitOutcome",
    "HEADER_WIDTH",
]


class FitOutcome(StrEnum):
    """What Shift+Z managed to do."""

    EMPTY = "empty"
    FITTED = "fitted"
    CLAMPED = "clamped"

HEADER_WIDTH = 120

#: One wheel notch multiplies or divides the zoom by this.
_ZOOM_STEP = 1.25

#: Room left either side of a fitted project so it is not flush to the edge.
_FIT_MARGIN = 16

MULTIPLE_VIDEO_TRACK_TOOLTIP = (
    "Multiple video tracks require compositing, not supported in this version."
)


class TimelineView(QGraphicsView):
    """Scrolling, zooming view onto a TimelineScene."""

    #: The user zoomed. Carries the new pixels_per_second.
    zoomed = Signal(float)
    #: Shift+Z ran. Carries the outcome, the zoom, and the fraction of the
    #: project that ended up visible.
    fitted = Signal(str, float, float)

    def __init__(self, scene: TimelineScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self._scene = scene
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # -- geometry ---------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # The scene sizes its ruler and lanes to at least this width.
        self._scene.set_viewport_width(self.viewport().width())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._scene.set_viewport_width(self.viewport().width())

    # -- zoom and scroll --------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        modifiers = event.modifiers()
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return

        if modifiers & Qt.KeyboardModifier.ControlModifier:
            self.zoom_at(event.position().toPoint(), _ZOOM_STEP if delta > 0 else 1 / _ZOOM_STEP)
            event.accept()
            return

        bar = (
            self.verticalScrollBar()
            if modifiers & Qt.KeyboardModifier.ShiftModifier
            else self.horizontalScrollBar()
        )
        bar.setValue(bar.value() - delta)
        event.accept()

    def zoom_at(self, viewport_pos: QPoint, factor: float) -> float:
        """Zoom by ``factor``, keeping the tick under ``viewport_pos`` still.

        The anchor tick is read unsnapped: snapping it to a frame here would
        move the anchor by up to half a frame on every notch, and repeated
        zooming would visibly walk the timeline sideways.
        """
        anchor_scene_x = self.mapToScene(viewport_pos).x()
        anchor_ticks = self._scene.x_to_ticks(anchor_scene_x, snap=False)

        before = self._scene.pixels_per_second
        after = self._scene.set_pixels_per_second(before * factor)
        if after == before:
            return after

        new_scene_x = self._scene.ticks_to_x(anchor_ticks)
        bar = self.horizontalScrollBar()
        bar.setValue(int(round(bar.value() + (new_scene_x - anchor_scene_x))))
        self.zoomed.emit(after)
        return after

    def zoom_at_centre(self, factor: float) -> float:
        centre = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        return self.zoom_at(centre, factor)

    def fit_project(self) -> float:
        """Shift+Z. Fit the whole project into the viewport.

        Three outcomes, all reported through :attr:`fitted` so the status bar
        can say which one happened:

        EMPTY    nothing on the timeline, so nothing to fit.
        FITTED   the whole project is on screen.
        CLAMPED  the project is longer than the minimum zoom can show. It
                 zooms out as far as it goes and shows as much as it can,
                 rather than silently leaving the view where it was.
        """
        project = self._scene.project()
        duration = project.duration if project else 0
        if duration <= 0:
            self.fitted.emit(FitOutcome.EMPTY, self._scene.pixels_per_second, 1.0)
            return self._scene.pixels_per_second

        width = max(1, self.viewport().width() - _FIT_MARGIN)
        ideal = width / ticks_to_seconds(duration)
        pps = self._scene.fit_ticks_to_width(duration, width)
        self.horizontalScrollBar().setValue(0)
        self.zoomed.emit(pps)

        # Clamped upward means the project needs a lower zoom than exists.
        # Clamped downward means it is so short it cannot fill the window,
        # which still shows all of it.
        if pps > ideal:
            visible = min(1.0, width / (ticks_to_seconds(duration) * pps))
            self.fitted.emit(FitOutcome.CLAMPED, pps, visible)
        else:
            self.fitted.emit(FitOutcome.FITTED, pps, 1.0)
        return pps

    def keyPressEvent(self, event) -> None:
        if (
            event.key() == Qt.Key.Key_Z
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.fit_project()
            event.accept()
            return
        super().keyPressEvent(event)


class TrackHeaderColumn(QWidget):
    """Fixed width column of track names and mute toggles.

    Add-track buttons live at the bottom. The video one is disabled whenever a
    video track already exists: v1 has no compositing, core.filtergraph refuses
    a second video track carrying clips, and the refusal has to be visible here
    rather than discovered at export.
    """

    mute_toggled = Signal(str, bool)
    add_track_requested = Signal(str)

    def __init__(self, scene: TimelineScene, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = scene
        self._scroll_offset = 0.0
        self.setFixedWidth(HEADER_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # The lane rows are positioned in scene coordinates, so this host
        # covers the whole column including the strip level with the ruler.
        # Nothing else may take height from it: a shorter host would clip the
        # bottom lane's controls while the view still showed that lane.
        self._lane_host = QWidget(self)
        self._lane_host.setGeometry(0, 0, HEADER_WIDTH, 1)

        self.add_video_button = QPushButton("+ Video", self)
        self.add_audio_button = QPushButton("+ Audio", self)
        self.add_video_button.clicked.connect(
            lambda: self.add_track_requested.emit("video")
        )
        self.add_audio_button.clicked.connect(
            lambda: self.add_track_requested.emit("audio")
        )
        # They live in the panel's button row, not in this column. Reparented
        # by TimelinePanel below.
        self._rows: dict[str, QWidget] = {}
        self._mute_boxes: dict[str, QCheckBox] = {}

    # -- building ---------------------------------------------------------

    def rebuild(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._mute_boxes.clear()

        for lane in self._scene.lanes():
            row = QFrame(self._lane_host)
            row.setProperty("surface", "panel")
            layout = QVBoxLayout(row)
            layout.setContentsMargins(6, 4, 6, 4)
            layout.setSpacing(2)

            name = QLabel(lane.name, row)
            name.setFont(theme.ui_font(bold=True))
            layout.addWidget(name)

            kind = QLabel("Video" if lane.kind == "video" else "Audio", row)
            kind.setProperty("muted", True)
            layout.addWidget(kind)

            mute = QCheckBox("Mute", row)
            mute.setChecked(lane.muted)
            mute.toggled.connect(
                lambda checked, track_id=lane.track_id: self.mute_toggled.emit(
                    track_id, checked
                )
            )
            layout.addWidget(mute)
            layout.addStretch(1)

            self._rows[lane.track_id] = row
            self._mute_boxes[lane.track_id] = mute
            row.show()

        self.refresh_add_buttons()
        self.relayout()

    def refresh_add_buttons(self) -> None:
        project: Project | None = self._scene.project()
        has_video = bool(project and project.video_tracks())
        self.add_video_button.setEnabled(not has_video)
        self.add_video_button.setToolTip(
            MULTIPLE_VIDEO_TRACK_TOOLTIP if has_video else "Add a video track"
        )
        self.add_audio_button.setEnabled(project is not None)
        self.add_audio_button.setToolTip("Add an audio track")

    def mute_box(self, track_id: str) -> QCheckBox | None:
        return self._mute_boxes.get(track_id)

    # -- geometry ---------------------------------------------------------

    def set_scroll_offset(self, offset: float) -> None:
        """Follow the view's vertical scrollbar so headers stay level."""
        if offset != self._scroll_offset:
            self._scroll_offset = offset
            self.relayout()

    def relayout(self) -> None:
        self._lane_host.setGeometry(0, 0, HEADER_WIDTH, max(1, self.height()))
        for lane in self._scene.lanes():
            row = self._rows.get(lane.track_id)
            if row is None:
                continue
            row.setGeometry(
                2,
                int(lane.top - self._scroll_offset),
                HEADER_WIDTH - 4,
                int(lane.height),
            )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.relayout()


class TimelinePanel(QWidget):
    """Header column plus view. This is what MainWindow embeds."""

    mute_toggled = Signal(str, bool)
    add_track_requested = Signal(str)
    fitted = Signal(str, float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scene = TimelineScene(self)
        self.view = TimelineView(self.scene, self)
        self.headers = TrackHeaderColumn(self.scene, self)
        self.view.fitted.connect(self.fitted)

        self.headers.mute_toggled.connect(self.mute_toggled)
        self.headers.add_track_requested.connect(self.add_track_requested)
        self.scene.layout_changed.connect(self._on_layout_changed)
        self.view.verticalScrollBar().valueChanged.connect(
            lambda value: self.headers.set_scroll_offset(float(value))
        )

        # The header column and the view are top aligned and share one
        # coordinate space: a row's y is its lane's scene y minus the vertical
        # scroll. That is why there is no ruler spacer here, and why the
        # add-track buttons sit in their own row below rather than taking
        # height out of the column.
        lanes_row = QHBoxLayout()
        lanes_row.setContentsMargins(0, 0, 0, 0)
        lanes_row.setSpacing(0)
        lanes_row.addWidget(self.headers)
        lanes_row.addWidget(self.view, 1)

        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(2, 4, 2, 0)
        buttons_row.setSpacing(4)
        buttons_row.addWidget(self.headers.add_video_button)
        buttons_row.addWidget(self.headers.add_audio_button)
        buttons_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(lanes_row, 1)
        layout.addLayout(buttons_row)

    def set_project(self, project: Project | None) -> None:
        self.scene.rebuild(project)

    def set_playhead(self, ticks: int) -> None:
        self.scene.set_playhead(ticks)

    def zoom_range(self) -> tuple[float, float]:
        return MIN_PIXELS_PER_SECOND, MAX_PIXELS_PER_SECOND

    def _on_layout_changed(self) -> None:
        self.headers.rebuild()
