"""The timeline scene.

THE COORDINATE MAPPING LIVES HERE AND NOWHERE ELSE.

:meth:`TimelineScene.ticks_to_x` and :meth:`TimelineScene.x_to_ticks` are the
only place in the application where a tick becomes a pixel or a pixel becomes a
tick. Every later phase reads mouse positions through them: dragging a clip,
trimming an edge, dropping from the media bin, scrubbing the playhead. If a
second copy of this arithmetic appears somewhere else, the two will disagree
under zoom and the bug will be very hard to see.

Zoom changes ``pixels_per_second`` and re-lays out the items. It does not scale
the view: a QGraphicsView transform would scale lane heights, text and clip
corners along with the time axis.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRectF, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QGraphicsScene

from core.model import Project, Track
from core.timebase import (
    TICKS_PER_SECOND,
    FrameRate,
    seconds_to_ticks,
    snap_to_frame,
    ticks_to_seconds,
)
from ui import theme
from ui.timeline.clip_item import ClipItem
from ui.timeline.playhead_item import PlayheadItem
from ui.timeline.ruler_item import RulerItem
from ui.workers.thumbnail_worker import shared_thumbnail_cache
from ui.workers.waveform_worker import WaveformCache

__all__ = [
    "TimelineScene",
    "Lane",
    "RULER_HEIGHT",
    "VIDEO_LANE_HEIGHT",
    "AUDIO_LANE_HEIGHT",
    "MIN_PIXELS_PER_SECOND",
    "MAX_PIXELS_PER_SECOND",
    "DEFAULT_PIXELS_PER_SECOND",
]

RULER_HEIGHT = 28.0
VIDEO_LANE_HEIGHT = 72.0
AUDIO_LANE_HEIGHT = 56.0
LANE_SPACING = 2.0

MIN_PIXELS_PER_SECOND = 0.05
MAX_PIXELS_PER_SECOND = 400.0
DEFAULT_PIXELS_PER_SECOND = 40.0

#: Empty room after the last clip so the end is not flush against the edge.
TRAILING_PAD_SECONDS = 2.0


@dataclass(frozen=True)
class Lane:
    """Where one track sits vertically. The header column reads these."""

    track_id: str
    name: str
    kind: str
    muted: bool
    top: float
    height: float

    @property
    def bottom(self) -> float:
        return self.top + self.height


class TimelineScene(QGraphicsScene):
    """Renders a Project. Read only in this phase."""

    #: Lane geometry or content width changed; the header column follows this.
    layout_changed = Signal()
    #: pixels_per_second changed.
    zoom_changed = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Project | None = None
        self._pps = DEFAULT_PIXELS_PER_SECOND
        self._lanes: list[Lane] = []
        self._clip_items: dict[str, ClipItem] = {}
        self._playhead_ticks = 0
        self._viewport_width = 0.0

        # Shared with the media bin, so a clip cut from a file already showing
        # there costs no second decode.
        self.thumbnails = shared_thumbnail_cache()
        self.waveforms = WaveformCache(self)
        self.thumbnails.thumbnail_ready.connect(self._on_thumbnail_ready)
        self.waveforms.waveform_ready.connect(self._on_waveform_ready)

        self._ruler = RulerItem(RULER_HEIGHT)
        self.addItem(self._ruler)
        self._playhead = PlayheadItem()
        self.addItem(self._playhead)

        self.setBackgroundBrush(QColor(theme.BACKGROUND))
        self._apply_geometry()

    # -- the coordinate mapping -------------------------------------------

    @property
    def pixels_per_second(self) -> float:
        return self._pps

    @pixels_per_second.setter
    def pixels_per_second(self, value: float) -> None:
        self.set_pixels_per_second(value)

    def set_pixels_per_second(self, value: float) -> float:
        """Set the zoom, clamped. Returns what it actually became."""
        clamped = max(MIN_PIXELS_PER_SECOND, min(MAX_PIXELS_PER_SECOND, float(value)))
        if clamped != self._pps:
            self._pps = clamped
            self.relayout()
            self.zoom_changed.emit(clamped)
        return self._pps

    def ticks_to_x(self, t: int) -> float:
        """Tick position to scene x. The only conversion in that direction."""
        return ticks_to_seconds(t) * self._pps

    def x_to_ticks(self, x: float, snap: bool = True) -> int:
        """Scene x to tick position. The only conversion in that direction.

        Rounds to the nearest tick, then snaps to a frame boundary, because
        every position a user picks with a mouse has to land on a frame. Pass
        ``snap=False`` for the intermediate value in a zoom anchor, where
        quantising twice would make the anchor drift.
        """
        ticks = seconds_to_ticks(max(0.0, float(x)) / self._pps)
        if not snap:
            return ticks
        return snap_to_frame(ticks, self.frame_rate())

    def frame_rate(self) -> FrameRate:
        return self._project.frame_rate if self._project else FrameRate(30, 1)

    def snap_ticks_to_frame(self, ticks: int) -> int:
        """Put a tick value on the project's frame grid.

        Here rather than in the interaction code because it is the same
        quantisation :meth:`x_to_ticks` applies, and the two must never be able
        to disagree about where a frame boundary is.
        """
        return snap_to_frame(int(ticks), self.frame_rate())

    # -- content ----------------------------------------------------------

    def project(self) -> Project | None:
        return self._project

    def lanes(self) -> list[Lane]:
        return list(self._lanes)

    def lane_for_track(self, track_id: str) -> Lane | None:
        for lane in self._lanes:
            if lane.track_id == track_id:
                return lane
        return None

    def track_at_y(self, y: float) -> str | None:
        """Which track's lane contains a scene y. Phase 4 drops clips with this."""
        for lane in self._lanes:
            if lane.top <= y < lane.bottom:
                return lane.track_id
        return None

    def clip_item(self, clip_id: str) -> ClipItem | None:
        return self._clip_items.get(clip_id)

    def clip_item_at(self, x: float, y: float) -> ClipItem | None:
        """The clip under a scene position, or None."""
        for item in self._clip_items.values():
            if item.sceneBoundingRect().contains(x, y):
                return item
        return None

    def selected_clip_ids(self) -> list[str]:
        return [item.clip_id for item in self._clip_items.values() if item.isSelected()]

    def set_selected_clip_ids(self, clip_ids) -> None:
        wanted = set(clip_ids)
        for item in self._clip_items.values():
            item.setSelected(item.clip_id in wanted)

    def clip_items(self) -> list[ClipItem]:
        return list(self._clip_items.values())

    def content_height(self) -> float:
        if not self._lanes:
            return RULER_HEIGHT + VIDEO_LANE_HEIGHT
        return self._lanes[-1].bottom + LANE_SPACING

    def viewport_width(self) -> float:
        return self._viewport_width

    def set_viewport_width(self, width: float) -> None:
        """Tell the scene how wide the window showing it is.

        The view calls this on every resize. Without it the scene would only
        be as wide as the project, and the ruler and lanes would stop dead
        wherever the last clip ends.
        """
        width = max(0.0, float(width))
        if width != self._viewport_width:
            self._viewport_width = width
            self._apply_geometry()

    def content_width(self) -> float:
        """How wide the scene is: the project, or the window, whichever is more.

        The ruler is a time axis, not a bar showing the project's extent. It
        runs the full width of the window at every zoom, including on an empty
        project where there is no extent to show. The lane backgrounds follow
        the same width so the timeline reads as a surface rather than a stub.
        """
        duration = self._project.duration if self._project else 0
        project_width = self.ticks_to_x(duration) + TRAILING_PAD_SECONDS * self._pps
        return max(project_width, self._viewport_width)

    # -- building ---------------------------------------------------------

    def rebuild(self, project: Project | None) -> None:
        """Clear every item and re-create them from ``project``.

        Deliberately crude. Phase 4 optimises this only if it proves slow;
        rebuilding is trivially correct and correctness is worth more here than
        a few milliseconds on an edit.
        """
        self._project = project

        # A rebuild destroys every item, and with them the selection. It is
        # carried across by id, because an edit is expected to leave the clip
        # it acted on still selected: split then duplicate is one thought, not
        # two, and a selection that vanished after every command would make it
        # two.
        selected = self.selected_clip_ids()

        for item in self._clip_items.values():
            self.removeItem(item)
        self._clip_items.clear()

        self._lanes = self._compute_lanes(project)

        if project is not None:
            for lane in self._lanes:
                track = self._track_by_id(project, lane.track_id)
                if track is None:
                    continue
                for clip in track.clips:
                    item = ClipItem(
                        clip_id=clip.id,
                        track_id=track.id,
                        kind=track.kind,
                        label=Path(clip.src).name,
                        src=Path(clip.src),
                        src_in=clip.src_in,
                        src_out=clip.src_out,
                    )
                    item.set_muted(track.muted)
                    self.addItem(item)
                    self._clip_items[clip.id] = item

        self.set_selected_clip_ids(selected)
        self.relayout()
        self.layout_changed.emit()

    def _compute_lanes(self, project: Project | None) -> list[Lane]:
        """Video lanes on top, audio beneath, each kind in project order.

        The model does not order tracks by kind, but every editor puts video
        above audio and a user reading the timeline expects that.
        """
        if project is None:
            return []
        ordered = list(project.video_tracks()) + list(project.audio_tracks())
        lanes: list[Lane] = []
        top = RULER_HEIGHT + LANE_SPACING
        for track in ordered:
            height = VIDEO_LANE_HEIGHT if track.kind == "video" else AUDIO_LANE_HEIGHT
            lanes.append(
                Lane(
                    track_id=track.id,
                    name=track.name,
                    kind=track.kind,
                    muted=track.muted,
                    top=top,
                    height=height,
                )
            )
            top += height + LANE_SPACING
        return lanes

    @staticmethod
    def _track_by_id(project: Project, track_id: str) -> Track | None:
        for track in project.tracks:
            if track.id == track_id:
                return track
        return None

    def relayout(self) -> None:
        """Reposition existing items for the current zoom. No rebuilding."""
        project = self._project
        if project is not None:
            for lane in self._lanes:
                track = self._track_by_id(project, lane.track_id)
                if track is None:
                    continue
                for clip in track.clips:
                    item = self._clip_items.get(clip.id)
                    if item is None:
                        continue
                    x = self.ticks_to_x(clip.timeline_start)
                    width = max(
                        1.0, self.ticks_to_x(clip.timeline_end) - x
                    )
                    item.setPos(x, lane.top)
                    item.setRect(QRectF(0, 0, width, lane.height))

        self._apply_geometry()

        for item in self._clip_items.values():
            item.refresh_media()

    def _apply_geometry(self) -> None:
        """Size the scene and everything that spans it.

        Every path reaches here: rebuild(), relayout(), a zoom change and a
        viewport resize all end up in this one function. The ruler in
        particular must not have a second, shorter update path, because its
        content depends on the zoom while its bounding rect often does not.
        """
        width = max(self.content_width(), 1.0)
        height = self.content_height()
        self.setSceneRect(QRectF(0, 0, width, height))
        self._ruler.relayout(width, self._pps)
        self._playhead.set_height(height)
        self._playhead.setX(self.ticks_to_x(self._playhead_ticks))

    # -- playhead ---------------------------------------------------------

    def playhead_ticks(self) -> int:
        return self._playhead_ticks

    def set_playhead(self, ticks: int) -> None:
        ticks = max(0, int(ticks))
        if ticks == self._playhead_ticks:
            return
        self._playhead_ticks = ticks
        self._playhead.setX(self.ticks_to_x(ticks))

    # -- background -------------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor(theme.BACKGROUND))
        # Lanes are painted across the whole exposed width, not across the
        # project's extent, so a lane looks like a surface running off the
        # edge of the window rather than a bar that stops at the last clip.
        for lane in self._lanes:
            lane_rect = QRectF(rect.left(), lane.top, rect.width(), lane.height)
            painter.fillRect(lane_rect, QColor(theme.PANEL))

    # -- cache signals ----------------------------------------------------

    def _on_thumbnail_ready(self, src: str, _tick: int) -> None:
        for item in self._clip_items.values():
            if item.kind == "video" and str(item.src) == src:
                item.update()

    def _on_waveform_ready(self, src: str) -> None:
        for item in self._clip_items.values():
            if item.kind == "audio" and str(item.src) == src:
                item.update()

    # -- zoom helpers -----------------------------------------------------

    def fit_ticks_to_width(self, ticks: int, viewport_width: float) -> float:
        """The zoom that makes ``ticks`` span ``viewport_width``, clamped."""
        seconds = ticks_to_seconds(max(1, ticks))
        if seconds <= 0 or viewport_width <= 0:
            return self._pps
        return self.set_pixels_per_second(viewport_width / seconds)
