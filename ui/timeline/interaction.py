"""Timeline gestures: move, trim, select, scrub, and drop from the media bin.

ONE COMMAND PER GESTURE. A drag produces a single :class:`~core.commands.MoveClip`
on mouse release, never one per mouse-move event. The model is not touched while
the pointer is down at all: the drag maintains a candidate position and draws a
ghost at it, and only the release turns that candidate into a command. If undo
ever replays a drag frame by frame, this rule has been broken.

The arithmetic that decides where a gesture lands is written as free functions
at the top of this file. They take and return integers, know nothing about Qt,
and are the part worth testing directly. :class:`TimelineInteraction` below is
the state machine that feeds them pointer positions and draws the result.

Every pixel/tick conversion goes through the scene. There is no second copy of
that arithmetic here, and there must not be: see the module docstring of
:mod:`ui.timeline.timeline_scene`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from core.commands import AddClipFromMedia, Command, MoveClip, TrimClip, free_span
from core.model import Clip, Project, Track
from core.timebase import frames_to_ticks
from ui import theme

__all__ = [
    "TimelineInteraction",
    "handle_width",
    "DRAG_THRESHOLD_PIXELS",
    "Zone",
    "Span",
    "SnapResult",
    "snap_position",
    "snap_targets",
    "clamp_move",
    "clamp_trim_left",
    "clamp_trim_right",
    "media_payload",
    "MEDIA_MIME",
    "SNAP_PIXELS",
    "TRIM_HANDLE_PIXELS",
]

#: A snap catches within this many screen pixels of a target. Screen pixels,
#: not ticks: the tolerance has to feel the same at every zoom.
SNAP_PIXELS = 8.0

#: Width of the hot zone at each end of a clip that grabs a trim handle.
TRIM_HANDLE_PIXELS = 6.0

#: A gesture does not begin until the pointer has travelled this far. Without
#: it, a click a few pixels inside a clip edge is a one-pixel trim, and a click
#: on a clip body is a one-pixel move.
DRAG_THRESHOLD_PIXELS = 3.0

#: The custom drag format the media bin publishes and the timeline accepts.
MEDIA_MIME = "application/x-videditor-media"

_GHOST_Z = 75.0


class Zone(StrEnum):
    """What is under the pointer."""

    NOTHING = "nothing"
    RULER = "ruler"
    BODY = "body"
    TRIM_LEFT = "trim_left"
    TRIM_RIGHT = "trim_right"


@dataclass(frozen=True)
class Span:
    """A clip's three tick values, without the model object."""

    src_in: int
    src_out: int
    timeline_start: int

    @property
    def duration(self) -> int:
        return self.src_out - self.src_in

    @property
    def timeline_end(self) -> int:
        return self.timeline_start + self.duration


@dataclass(frozen=True)
class SnapResult:
    ticks: int
    #: What it snapped to, or None when nothing was near enough.
    target: int | None
    #: "playhead", "edge", "zero" or "none". The status bar says which.
    kind: str


# -- the arithmetic ---------------------------------------------------------


def snap_targets(project: Project | None, exclude: Iterable[str] = ()) -> list[int]:
    """Every clip edge on every track, minus the clips being dragged.

    A clip snapping to its own edges would pin it where it started.
    """
    if project is None:
        return []
    skip = set(exclude)
    edges: set[int] = set()
    for track in project.tracks:
        for clip in track.clips:
            if clip.id in skip:
                continue
            edges.add(clip.timeline_start)
            edges.add(clip.timeline_end)
    return sorted(edges)


def snap_position(
    ticks: int,
    playhead: int | None,
    edges: Sequence[int],
    tolerance: int,
) -> SnapResult:
    """Pull ``ticks`` onto the nearest target, in priority order.

    Playhead first, then any clip edge on any track, then zero. Priority, not
    proximity: when the playhead is within reach it wins even if a clip edge is
    closer, because the playhead is where the user just put it and is what they
    are lining up against.
    """
    if tolerance <= 0:
        return SnapResult(ticks, None, "none")

    if playhead is not None and abs(ticks - playhead) <= tolerance:
        return SnapResult(playhead, playhead, "playhead")

    best: int | None = None
    best_distance = tolerance + 1
    for edge in edges:
        distance = abs(ticks - edge)
        if distance <= tolerance and distance < best_distance:
            best, best_distance = edge, distance
    if best is not None:
        return SnapResult(best, best, "edge")

    if abs(ticks) <= tolerance:
        return SnapResult(0, 0, "zero")
    return SnapResult(ticks, None, "none")


def handle_width(clip_width: float) -> float:
    """How wide the trim hot zone is on a clip this many pixels across.

    Never more than a third of the clip, so a narrow clip keeps a body that
    can be grabbed and moved. At a wide zoom-out a clip is a few pixels across,
    and a fixed six-pixel handle at each end would leave nothing in between:
    every clip would be all handles and none of them could be dragged.
    """
    return min(TRIM_HANDLE_PIXELS, max(0.0, clip_width) / 3.0)


def clamp_move(start: int) -> int:
    """A clip cannot begin before the timeline does."""
    return max(0, start)


def clamp_trim_left(
    span: Span, wanted_start: int, previous_end: int, min_duration: int
) -> Span:
    """Drag the head of a clip. ``src_in`` and ``timeline_start`` move together.

    Bounded by, in order: the start of the source media, the end of the clip
    before it on the same track, the start of the timeline, and one frame of
    remaining duration.
    """
    # The head cannot go further left than the source has material for.
    lowest = span.timeline_start - span.src_in
    # ...nor further left than the neighbour, nor before zero.
    lowest = max(lowest, previous_end, 0)
    # ...nor so far right that nothing is left.
    highest = span.timeline_end - min_duration

    start = max(lowest, min(highest, wanted_start))
    delta = start - span.timeline_start
    return Span(span.src_in + delta, span.src_out, start)


def clamp_trim_right(
    span: Span,
    wanted_end: int,
    next_start: int | None,
    media_duration: int | None,
    min_duration: int,
) -> Span:
    """Drag the tail of a clip. Only ``src_out`` moves.

    ``media_duration`` is the length of the source file, when it is known. It
    is not always: a project opened from disk carries clip spans but no probe
    result, and nothing in this phase goes and fetches one. Where it is unknown
    the tail is unbounded to the right, which is the same freedom the file
    format already allows.
    """
    lowest = span.timeline_start + min_duration
    highest = None if media_duration is None else (
        span.timeline_start + (media_duration - span.src_in)
    )
    if next_start is not None:
        highest = next_start if highest is None else min(highest, next_start)

    end = max(lowest, wanted_end if highest is None else min(highest, wanted_end))
    return Span(span.src_in, span.src_in + (end - span.timeline_start), span.timeline_start)


def media_payload(
    src: Path | str, duration_ticks: int, has_video: bool, has_audio: bool
) -> bytes:
    """Encode what a bin entry needs to become a clip.

    The drag carries the probe result, not just the path, so the timeline can
    size and validate the ghost without a lookup back into the window.
    """
    return json.dumps(
        {
            "path": str(src),
            "duration_ticks": int(duration_ticks),
            "has_video": bool(has_video),
            "has_audio": bool(has_audio),
        }
    ).encode("utf-8")


def decode_media_payload(raw: bytes | bytearray | memoryview) -> dict | None:
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or "path" not in data:
        return None
    if int(data.get("duration_ticks", 0)) <= 0:
        return None
    return data


def track_accepts_media(track_kind: str, has_video: bool) -> bool:
    """A source with a video stream belongs on a video track, and only there.

    An audio-only file onto a video lane has no picture to show; anything with
    picture onto an audio lane would silently drop it. Both are rejected rather
    than half honoured.
    """
    return track_kind == ("video" if has_video else "audio")


# -- the ghost --------------------------------------------------------------


class GhostItem(QGraphicsRectItem):
    """Translucent preview of where the gesture would land.

    Accent while the drop is legal, red while it is not. It is a scene item
    rather than a painted overlay so it lives in the same coordinate space as
    the clips it is being lined up against.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setZValue(_GHOST_Z)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setVisible(False)
        self._valid = True

    def show_at(self, rect: QRectF, valid: bool) -> None:
        self.setRect(QRectF(0, 0, rect.width(), rect.height()))
        self.setPos(rect.left(), rect.top())
        self._valid = valid
        colour = QColor(theme.ACCENT if valid else theme.ERROR)
        fill = QColor(colour)
        fill.setAlpha(90)
        self.setBrush(QBrush(fill))
        self.setPen(QPen(colour, 2))
        self.setVisible(True)

    def hide_ghost(self) -> None:
        self.setVisible(False)

    def is_valid(self) -> bool:
        return self._valid


# -- gesture state ----------------------------------------------------------


@dataclass
class _Move:
    clip_id: str
    from_track_id: str
    kind: str
    span: Span
    grab_offset: int
    to_track_id: str
    start: int
    valid: bool
    #: Why the drop is refused, when it is. Worked out where the refusal is
    #: detected; reconstructing it at release time got it wrong, because a
    #: wrong-kind drag keeps the clip on its own track and then looks exactly
    #: like an overlap.
    reason: str = ""


@dataclass
class _Trim:
    clip_id: str
    track_id: str
    kind: str
    original: Span
    edge: Zone
    result: Span


@dataclass
class _MediaDrag:
    payload: dict
    track_id: str | None
    start: int
    valid: bool


class TimelineInteraction(QObject):
    """Turns pointer input on the timeline into commands.

    Driven by :class:`~ui.timeline.timeline_view.TimelineView`, which converts
    widget coordinates to scene coordinates and forwards them here. Keeping the
    view as a thin adapter means every gesture can be tested by calling these
    methods with scene positions, without synthesising Qt events.
    """

    #: A completed gesture. Carries a core.commands.Command for the window to push.
    command_requested = Signal(object)
    #: A scrub gesture began: the ruler was pressed. Playback stops on this.
    scrub_started = Signal()
    #: The playhead was dragged or clicked to a new tick, mid-gesture.
    playhead_scrubbed = Signal("qint64")
    #: The scrub gesture ended, at this tick. Playback stays stopped.
    scrub_finished = Signal("qint64")
    #: Clip selection changed.
    selection_changed = Signal()
    #: A gesture was refused. Carries a sentence for the status bar.
    rejected = Signal(str)

    def __init__(self, scene, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._scene = scene
        self._ghost = GhostItem()
        scene.addItem(self._ghost)

        self._band = QGraphicsRectItem()
        self._band.setZValue(_GHOST_Z + 1)
        self._band.setVisible(False)
        self._band.setBrush(QBrush(QColor(theme.ACCENT).lighter(120)))
        self._band.setOpacity(0.18)
        self._band.setPen(QPen(QColor(theme.ACCENT), 1, Qt.PenStyle.DashLine))
        scene.addItem(self._band)

        self._move: _Move | None = None
        self._trim: _Trim | None = None
        self._media: _MediaDrag | None = None
        self._scrubbing = False
        self._band_origin: QPointF | None = None
        self._band_additive = False
        self._press_pos: QPointF | None = None
        self._moved = False

        #: Installed by MainWindow. Answers "how long is this source file",
        #: which is what bounds a right-hand trim. Missing answers are fine.
        self.media_duration = lambda src: None

    # -- queries ----------------------------------------------------------

    def is_dragging(self) -> bool:
        return bool(
            self._move or self._trim or self._scrubbing or self._band_origin or self._media
        )

    def zone_at(self, pos: QPointF) -> tuple[Zone, str | None]:
        """What the pointer is over, and the clip id when it is over a clip."""
        from ui.timeline.timeline_scene import RULER_HEIGHT

        if pos.y() < RULER_HEIGHT:
            return Zone.RULER, None
        item = self._clip_at(pos)
        if item is None:
            return Zone.NOTHING, None
        rect = item.sceneBoundingRect()
        handle = handle_width(rect.width())
        if pos.x() - rect.left() <= handle:
            return Zone.TRIM_LEFT, item.clip_id
        if rect.right() - pos.x() <= handle:
            return Zone.TRIM_RIGHT, item.clip_id
        return Zone.BODY, item.clip_id

    def cursor_for(self, pos: QPointF) -> Qt.CursorShape:
        zone, _ = self.zone_at(pos)
        if zone in (Zone.TRIM_LEFT, Zone.TRIM_RIGHT):
            return Qt.CursorShape.SizeHorCursor
        if zone == Zone.RULER:
            return Qt.CursorShape.PointingHandCursor
        return Qt.CursorShape.ArrowCursor

    def selected_clip_ids(self) -> list[str]:
        return [item.clip_id for item in self._scene.clip_items() if item.isSelected()]

    # -- the gesture ------------------------------------------------------

    def press(self, pos: QPointF, modifiers: Qt.KeyboardModifier) -> bool:
        """Left button down. True when the gesture was taken."""
        zone, clip_id = self.zone_at(pos)
        self._press_pos = QPointF(pos)
        self._moved = False

        if zone == Zone.RULER:
            self._scrubbing = True
            # Announced before the first position, so playback is already
            # stopped by the time anything acts on where the playhead went.
            self.scrub_started.emit()
            self._scrub_to(pos)
            return True

        if clip_id is None:
            self._begin_band(pos, modifiers)
            return True

        self._apply_click_selection(clip_id, modifiers)
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+click is a selection gesture, not a drag.
            return True

        if zone in (Zone.TRIM_LEFT, Zone.TRIM_RIGHT):
            self._begin_trim(clip_id, zone)
        else:
            self._begin_move(clip_id, pos)
        return True

    def drag(self, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        if self._scrubbing:
            self._scrub_to(pos)
            return
        if not self._past_threshold(pos):
            return
        if self._move is not None:
            self._update_move(pos, modifiers)
        elif self._trim is not None:
            self._update_trim(pos, modifiers)
        elif self._band_origin is not None:
            self._update_band(pos)

    def _past_threshold(self, pos: QPointF) -> bool:
        """True once this gesture counts as a drag rather than a click."""
        if self._moved:
            return True
        if self._press_pos is None:
            return True
        delta = pos - self._press_pos
        if abs(delta.x()) >= DRAG_THRESHOLD_PIXELS or abs(delta.y()) >= DRAG_THRESHOLD_PIXELS:
            self._moved = True
        return self._moved

    def release(self, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        # The release position is part of the gesture, not just its terminator.
        # Qt usually sends a move first, but not always, and a release that
        # ignored where the button came up would commit the second-to-last
        # position.
        self.drag(pos, modifiers)
        if self._scrubbing:
            self._end_scrub()
            return
        if self._move is not None:
            self._finish_move()
            return
        if self._trim is not None:
            self._finish_trim()
            return
        if self._band_origin is not None:
            self._finish_band()

    def cancel(self) -> None:
        """Escape. Abandon whatever is in progress, changing nothing.

        A scrub is the exception: it has already moved the playhead on every
        move, so it is finished rather than abandoned, which is what gets the
        bed aligned with where the playhead actually ended up.
        """
        if self._scrubbing:
            self._end_scrub()
        self._move = None
        self._trim = None
        self._media = None
        self._band_origin = None
        self._press_pos = None
        self._moved = False
        self._ghost.hide_ghost()
        self._band.setVisible(False)

    # -- selection --------------------------------------------------------

    def _apply_click_selection(
        self, clip_id: str, modifiers: Qt.KeyboardModifier
    ) -> None:
        item = self._scene.clip_item(clip_id)
        if item is None:
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            item.setSelected(not item.isSelected())
        elif not item.isSelected():
            self.select_only(clip_id)
        self.selection_changed.emit()

    def select_only(self, clip_id: str | None) -> None:
        for item in self._scene.clip_items():
            item.setSelected(item.clip_id == clip_id)

    def clear_selection(self) -> None:
        for item in self._scene.clip_items():
            item.setSelected(False)
        self.selection_changed.emit()

    def _begin_band(self, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        self._band_additive = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if not self._band_additive:
            self.select_only(None)
            self.selection_changed.emit()
        self._band_origin = QPointF(pos)
        self._band.setRect(QRectF(0, 0, 0, 0))
        self._band.setPos(pos)
        self._band.setVisible(True)

    def _update_band(self, pos: QPointF) -> None:
        assert self._band_origin is not None
        rect = QRectF(self._band_origin, pos).normalized()
        self._band.setPos(rect.topLeft())
        self._band.setRect(QRectF(0, 0, rect.width(), rect.height()))

    def _finish_band(self) -> None:
        assert self._band_origin is not None
        rect = QRectF(
            self._band.pos(),
            QPointF(
                self._band.pos().x() + self._band.rect().width(),
                self._band.pos().y() + self._band.rect().height(),
            ),
        )
        for item in self._scene.clip_items():
            if item.sceneBoundingRect().intersects(rect):
                item.setSelected(True)
        self._band.setVisible(False)
        self._band_origin = None
        self.selection_changed.emit()

    # -- move -------------------------------------------------------------

    def _begin_move(self, clip_id: str, pos: QPointF) -> None:
        found = self._find_clip(clip_id)
        if found is None:
            return
        track, clip = found
        span = _span_of(clip)
        pointer = self._scene.x_to_ticks(pos.x(), snap=False)
        self._move = _Move(
            clip_id=clip_id,
            from_track_id=track.id,
            kind=track.kind,
            span=span,
            grab_offset=pointer - span.timeline_start,
            to_track_id=track.id,
            start=span.timeline_start,
            valid=True,
            reason="",
        )
        self._draw_move_ghost()

    def _update_move(self, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        move = self._move
        assert move is not None
        project = self._scene.project()

        pointer = self._scene.x_to_ticks(pos.x(), snap=False)
        wanted = clamp_move(pointer - move.grab_offset)
        wanted = self._snapped(wanted, modifiers, exclude=(move.clip_id,))

        target_id = self._scene.track_at_y(pos.y()) or move.from_track_id
        target = _track_by_id(project, target_id)
        same_kind = target is not None and target.kind == move.kind

        # The ghost follows the pointer onto the wrong kind of lane and turns
        # red there, rather than staying behind on the clip's own track. It
        # has to say "not this lane", and a red rectangle somewhere else says
        # "not this position".
        fits = (
            same_kind
            and free_span(
                target, wanted, wanted + move.span.duration, ignore=(move.clip_id,)
            )
        )
        move.to_track_id = target_id
        move.start = wanted
        move.valid = bool(same_kind and fits)
        if not same_kind:
            move.reason = f"Cannot move a {move.kind} clip onto that track"
        elif not fits:
            move.reason = "Cannot drop there: it would overlap another clip"
        else:
            move.reason = ""
        self._draw_move_ghost()

    def _draw_move_ghost(self) -> None:
        move = self._move
        assert move is not None
        lane = self._scene.lane_for_track(move.to_track_id)
        if lane is None:
            return
        left = self._scene.ticks_to_x(move.start)
        right = self._scene.ticks_to_x(move.start + move.span.duration)
        self._ghost.show_at(
            QRectF(left, lane.top, max(2.0, right - left), lane.height), move.valid
        )

    def _finish_move(self) -> None:
        move = self._move
        self._move = None
        self._ghost.hide_ghost()
        assert move is not None

        if not move.valid:
            self.rejected.emit(move.reason or "Cannot drop there")
            return
        if (
            move.to_track_id == move.from_track_id
            and move.start == move.span.timeline_start
        ):
            return
        # Exactly one command, on release. Not one per mouse-move event.
        self.command_requested.emit(
            MoveClip(
                clip_id=move.clip_id,
                from_track_id=move.from_track_id,
                to_track_id=move.to_track_id,
                new_timeline_start=move.start,
            )
        )

    # -- trim -------------------------------------------------------------

    def _begin_trim(self, clip_id: str, edge: Zone) -> None:
        found = self._find_clip(clip_id)
        if found is None:
            return
        track, clip = found
        span = _span_of(clip)
        self._trim = _Trim(
            clip_id=clip_id,
            track_id=track.id,
            kind=track.kind,
            original=span,
            edge=edge,
            result=span,
        )
        self._draw_trim_ghost()

    def _update_trim(self, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        trim = self._trim
        assert trim is not None
        found = self._find_clip(trim.clip_id)
        if found is None:
            return
        track, clip = found

        wanted = clamp_move(
            self._snapped(
                self._scene.x_to_ticks(pos.x(), snap=False),
                modifiers,
                exclude=(trim.clip_id,),
            )
        )
        minimum = frames_to_ticks(1, self._scene.frame_rate())

        if trim.edge == Zone.TRIM_LEFT:
            trim.result = clamp_trim_left(
                trim.original, wanted, _previous_end(track, clip), minimum
            )
        else:
            trim.result = clamp_trim_right(
                trim.original,
                wanted,
                _next_start(track, clip),
                self.media_duration(clip.src),
                minimum,
            )
        self._draw_trim_ghost()

    def _draw_trim_ghost(self) -> None:
        trim = self._trim
        assert trim is not None
        lane = self._scene.lane_for_track(trim.track_id)
        if lane is None:
            return
        left = self._scene.ticks_to_x(trim.result.timeline_start)
        right = self._scene.ticks_to_x(trim.result.timeline_end)
        self._ghost.show_at(
            QRectF(left, lane.top, max(2.0, right - left), lane.height), True
        )

    def _finish_trim(self) -> None:
        trim = self._trim
        self._trim = None
        self._ghost.hide_ghost()
        assert trim is not None

        if trim.result == trim.original:
            return
        self.command_requested.emit(
            TrimClip(
                track_id=trim.track_id,
                clip_id=trim.clip_id,
                new_src_in=trim.result.src_in,
                new_src_out=trim.result.src_out,
                new_timeline_start=trim.result.timeline_start,
            )
        )

    # -- playhead ---------------------------------------------------------

    def _scrub_to(self, pos: QPointF) -> None:
        ticks = self._scene.x_to_ticks(max(0.0, pos.x()))
        self._scene.set_playhead(ticks)
        self.playhead_scrubbed.emit(ticks)

    def _end_scrub(self) -> None:
        """Finish the gesture at wherever the playhead ended up.

        The final position is emitted separately from the per-move ones so
        that whatever aligns the audio can do it once, on release, rather than
        fifty times during the drag.
        """
        self._scrubbing = False
        self.scrub_finished.emit(self._scene.playhead_ticks())

    # -- drag in from the media bin ---------------------------------------

    def begin_media_drag(self, payload: dict) -> None:
        self._media = _MediaDrag(payload=payload, track_id=None, start=0, valid=False)

    def update_media_drag(
        self, pos: QPointF, modifiers: Qt.KeyboardModifier
    ) -> bool:
        """Position the ghost for an in-flight bin drag. True when droppable."""
        media = self._media
        if media is None:
            return False
        project = self._scene.project()
        duration = int(media.payload["duration_ticks"])

        track_id = self._scene.track_at_y(pos.y())
        track = _track_by_id(project, track_id) if track_id else None

        pointer = self._scene.x_to_ticks(pos.x(), snap=False)
        start = clamp_move(self._snapped(pointer, modifiers))

        kind_ok = track is not None and track_accepts_media(
            track.kind, bool(media.payload.get("has_video"))
        )
        fits = kind_ok and free_span(track, start, start + duration)

        media.track_id = track_id
        media.start = start
        media.valid = bool(kind_ok and fits)

        lane = self._scene.lane_for_track(track_id) if track_id else None
        if lane is None:
            self._ghost.hide_ghost()
        else:
            left = self._scene.ticks_to_x(start)
            right = self._scene.ticks_to_x(start + duration)
            self._ghost.show_at(
                QRectF(left, lane.top, max(2.0, right - left), lane.height),
                media.valid,
            )
        return media.valid

    def drop_media(self) -> bool:
        """Commit the in-flight bin drag. True when a command was emitted."""
        media = self._media
        self._media = None
        self._ghost.hide_ghost()
        if media is None:
            return False
        if not media.valid or media.track_id is None:
            self.rejected.emit(self._media_refusal(media))
            return False
        self.command_requested.emit(
            AddClipFromMedia(
                track_id=media.track_id,
                src=Path(media.payload["path"]),
                src_in=0,
                src_out=int(media.payload["duration_ticks"]),
                timeline_start=media.start,
            )
        )
        return True

    def end_media_drag(self) -> None:
        self._media = None
        self._ghost.hide_ghost()

    def _media_refusal(self, media: _MediaDrag) -> str:
        project = self._scene.project()
        track = _track_by_id(project, media.track_id) if media.track_id else None
        if track is None:
            return "Drop a clip onto a track"
        if not track_accepts_media(track.kind, bool(media.payload.get("has_video"))):
            what = "video" if media.payload.get("has_video") else "audio-only"
            return f"Cannot drop a {what} file onto the {track.kind} track {track.name}"
        return "Cannot drop there: it would overlap another clip"

    # -- shared -----------------------------------------------------------

    def _snapped(
        self,
        ticks: int,
        modifiers: Qt.KeyboardModifier,
        exclude: Iterable[str] = (),
    ) -> int:
        if modifiers & Qt.KeyboardModifier.AltModifier:
            # Alt is the escape hatch: place it exactly where the pointer is.
            return self._scene.snap_ticks_to_frame(ticks)
        tolerance = self._scene.x_to_ticks(SNAP_PIXELS, snap=False)
        result = snap_position(
            ticks,
            self._scene.playhead_ticks(),
            snap_targets(self._scene.project(), exclude),
            tolerance,
        )
        if result.target is not None:
            return result.ticks
        return self._scene.snap_ticks_to_frame(result.ticks)

    def _clip_at(self, pos: QPointF):
        for item in self._scene.clip_items():
            if item.sceneBoundingRect().contains(pos):
                return item
        return None

    def _find_clip(self, clip_id: str) -> tuple[Track, Clip] | None:
        project = self._scene.project()
        if project is None:
            return None
        for track in project.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return track, clip
        return None


# -- small model helpers ----------------------------------------------------


def _span_of(clip: Clip) -> Span:
    return Span(clip.src_in, clip.src_out, clip.timeline_start)


def _track_by_id(project: Project | None, track_id: str | None) -> Track | None:
    if project is None or track_id is None:
        return None
    for track in project.tracks:
        if track.id == track_id:
            return track
    return None


def _previous_end(track: Track, clip: Clip) -> int:
    end = 0
    for other in track.clips:
        if other.id == clip.id:
            continue
        if other.timeline_end <= clip.timeline_start:
            end = max(end, other.timeline_end)
    return end


def _next_start(track: Track, clip: Clip) -> int | None:
    starts = [
        other.timeline_start
        for other in track.clips
        if other.id != clip.id and other.timeline_start >= clip.timeline_end
    ]
    return min(starts) if starts else None
