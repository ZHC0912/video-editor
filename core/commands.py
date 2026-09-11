"""Editing operations, as undoable commands.

Pure Python. Like the rest of ``core`` this module must never import Qt or
anything from ``ui``.

THE RULE THAT MATTERS: every command stores whatever it needs to reverse
itself exactly, captured at the moment it runs. Nothing recomputes an inverse.
:class:`DeleteClip` keeps the whole :class:`~core.model.Clip`, :class:`TrimClip`
keeps the three values it overwrote, :class:`RemoveTrack` keeps the whole
:class:`~core.model.Track` and the index it sat at. A command that derived its
undo from the current state would be correct only until some later command
changed that state, and the failure would appear several undos later with
nothing to connect it to the cause.

Ids are generated in ``__init__``, not in ``do()``. A command that invented a
new id every time it ran would hand back a different clip on redo, and any
command pushed after it would then be pointing at something that no longer
exists.

Every command validates completely before it mutates anything. A command that
raises :class:`CommandError` has changed nothing, so the caller can report the
refusal and carry on with a project that is still consistent.

Time arguments are integer ticks, and are snapped to frame boundaries at the
top of ``do()`` through :func:`snap_all`, which is the only snapping helper
here and is called exactly once per command.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from core.model import Clip, Project, Track
from core.timebase import snap_to_frame

__all__ = [
    "CommandError",
    "Command",
    "CommandStack",
    "MacroCommand",
    "SplitClip",
    "TrimClip",
    "MoveClip",
    "DeleteClip",
    "DuplicateClip",
    "SetClipGain",
    "RelinkMedia",
    "SetTrackMuted",
    "AddClipFromMedia",
    "AddTrack",
    "RemoveTrack",
    "snap_all",
    "free_span",
    "find_track",
    "find_clip",
]


class CommandError(Exception):
    """A command refused to run. The project is untouched."""


# -- helpers ---------------------------------------------------------------


def snap_all(project: Project, *ticks: int) -> tuple[int, ...]:
    """Snap every value to the project's frame grid.

    The one snapping helper in this module. Each command calls it once, at the
    top of ``do()``, so that there is a single place where an edit becomes
    frame aligned and no command can half apply the rule.

    Nearest, not ceiling: these are positions a user picked with a mouse, and
    :func:`core.timebase.snap_to_frame` is the rule for those.
    """
    rate = project.frame_rate
    return tuple(snap_to_frame(int(t), rate) for t in ticks)


def find_track(project: Project, track_id: str) -> Track:
    for track in project.tracks:
        if track.id == track_id:
            return track
    raise CommandError(f"no such track: {track_id}")


def find_clip(track: Track, clip_id: str) -> Clip:
    for clip in track.clips:
        if clip.id == clip_id:
            return clip
    raise CommandError(f"no such clip on {track.name}: {clip_id}")


def _find_clip_anywhere(project: Project, clip_id: str) -> tuple[Track, Clip] | None:
    """The clip with this id, wherever it is. None when it has gone.

    Used by commands that are handed clip ids without the track they sit on.
    """
    for track in project.tracks:
        for clip in track.clips:
            if clip.id == clip_id:
                return track, clip
    return None


def free_span(
    track: Track, start: int, end: int, ignore: tuple[str, ...] = ()
) -> bool:
    """True when ``[start, end)`` overlaps nothing on ``track``.

    ``ignore`` names clips to look past, which is how a move or a trim asks
    whether its own new position is free without colliding with where it
    currently is.
    """
    for clip in track.clips:
        if clip.id in ignore:
            continue
        if start < clip.timeline_end and clip.timeline_start < end:
            return False
    return True


def _insert(track: Track, clip: Clip) -> None:
    """Add a clip and keep the track ordered. Overlap is the caller's problem."""
    track.clips.append(clip)
    track.clips.sort(key=lambda c: c.timeline_start)


def _remove(track: Track, clip_id: str) -> Clip:
    for index, clip in enumerate(track.clips):
        if clip.id == clip_id:
            return track.clips.pop(index)
    raise CommandError(f"no such clip on {track.name}: {clip_id}")


def _require_free(track: Track, start: int, end: int, ignore: tuple[str, ...] = ()) -> None:
    if not free_span(track, start, end, ignore):
        raise CommandError(
            f"that would overlap another clip on {track.name}"
        )


def _new_id() -> str:
    return uuid.uuid4().hex


# -- the base ---------------------------------------------------------------


class Command(ABC):
    """One reversible edit."""

    label: str = "Edit"

    def __init__(self) -> None:
        #: Whether this edit changes what the audio bed should contain, which
        #: is to say whether it touches an audio track. Commands addressed to
        #: a track cannot know its kind before they have seen the project, so
        #: this starts True and ``do()`` narrows it. Reading it before ``do()``
        #: has run tells you nothing; the stack reads it afterwards.
        self.touches_audio = True

    @abstractmethod
    def do(self, project: Project) -> None:
        """Apply the edit. Raises CommandError, having changed nothing."""

    @abstractmethod
    def undo(self, project: Project) -> None:
        """Put back exactly what ``do`` replaced."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.label!r}>"


# -- clip commands ----------------------------------------------------------


class SplitClip(Command):
    """Cut one clip in two at a timeline position.

    The left half keeps the original clip's id, so a selection or a later
    command referring to it still resolves. The right half is a new clip whose
    id is fixed at construction, so redo reproduces it exactly.
    """

    label = "Split clip"

    def __init__(self, track_id: str, clip_id: str, at_ticks: int) -> None:
        super().__init__()
        self.track_id = track_id
        self.clip_id = clip_id
        self.at_ticks = int(at_ticks)
        self.new_clip_id = _new_id()
        self._old_src_out: int | None = None

    def do(self, project: Project) -> None:
        (at,) = snap_all(project, self.at_ticks)
        track = find_track(project, self.track_id)
        clip = find_clip(track, self.clip_id)

        if not clip.timeline_start < at < clip.timeline_end:
            raise CommandError("the split point is not inside the clip")

        offset = at - clip.timeline_start
        right = Clip(
            id=self.new_clip_id,
            src=clip.src,
            src_in=clip.src_in + offset,
            src_out=clip.src_out,
            timeline_start=at,
            gain_db=clip.gain_db,
        )

        self._old_src_out = clip.src_out
        clip.src_out = clip.src_in + offset
        _insert(track, right)
        self.touches_audio = track.kind == "audio"

    def undo(self, project: Project) -> None:
        track = find_track(project, self.track_id)
        _remove(track, self.new_clip_id)
        clip = find_clip(track, self.clip_id)
        assert self._old_src_out is not None
        clip.src_out = self._old_src_out


class TrimClip(Command):
    """Change a clip's in point, out point and position together.

    The left handle moves ``src_in`` and ``timeline_start`` by the same amount,
    which is what keeps the rest of the clip still while its head is dragged.
    The right handle moves ``src_out`` alone. Both arrive here as three
    absolute values; this command does not know which handle produced them.
    """

    label = "Trim clip"

    def __init__(
        self,
        track_id: str,
        clip_id: str,
        new_src_in: int,
        new_src_out: int,
        new_timeline_start: int,
    ) -> None:
        super().__init__()
        self.track_id = track_id
        self.clip_id = clip_id
        self.new_src_in = int(new_src_in)
        self.new_src_out = int(new_src_out)
        self.new_timeline_start = int(new_timeline_start)
        self._previous: tuple[int, int, int] | None = None

    def do(self, project: Project) -> None:
        src_in, src_out, start = snap_all(
            project, self.new_src_in, self.new_src_out, self.new_timeline_start
        )
        track = find_track(project, self.track_id)
        clip = find_clip(track, self.clip_id)

        if src_in < 0:
            raise CommandError("a clip cannot start before the start of its source")
        if src_out <= src_in:
            raise CommandError("a clip cannot be trimmed to nothing")
        if start < 0:
            raise CommandError("a clip cannot start before the timeline does")
        _require_free(track, start, start + (src_out - src_in), ignore=(clip.id,))

        self._previous = (clip.src_in, clip.src_out, clip.timeline_start)
        clip.src_in = src_in
        clip.src_out = src_out
        clip.timeline_start = start
        track.clips.sort(key=lambda c: c.timeline_start)
        self.touches_audio = track.kind == "audio"

    def undo(self, project: Project) -> None:
        assert self._previous is not None
        track = find_track(project, self.track_id)
        clip = find_clip(track, self.clip_id)
        clip.src_in, clip.src_out, clip.timeline_start = self._previous
        track.clips.sort(key=lambda c: c.timeline_start)


class MoveClip(Command):
    """Move a clip along the timeline, and optionally onto another track.

    Only between tracks of the same kind. A video clip on an audio track has
    no meaning to the filter graph, and letting one land there would turn a
    gesture into an export failure much later.
    """

    label = "Move clip"

    def __init__(
        self,
        clip_id: str,
        from_track_id: str,
        to_track_id: str,
        new_timeline_start: int,
    ) -> None:
        super().__init__()
        self.clip_id = clip_id
        self.from_track_id = from_track_id
        self.to_track_id = to_track_id
        self.new_timeline_start = int(new_timeline_start)
        self._old_start: int | None = None

    def do(self, project: Project) -> None:
        (start,) = snap_all(project, self.new_timeline_start)
        source = find_track(project, self.from_track_id)
        target = find_track(project, self.to_track_id)
        clip = find_clip(source, self.clip_id)

        if source.kind != target.kind:
            raise CommandError(
                f"cannot move a {source.kind} clip onto the {target.kind} "
                f"track {target.name}"
            )
        if start < 0:
            raise CommandError("a clip cannot start before the timeline does")
        _require_free(target, start, start + clip.duration, ignore=(clip.id,))

        self._old_start = clip.timeline_start
        if target is not source:
            _remove(source, clip.id)
            clip.timeline_start = start
            _insert(target, clip)
        else:
            clip.timeline_start = start
            source.clips.sort(key=lambda c: c.timeline_start)
        self.touches_audio = target.kind == "audio"

    def undo(self, project: Project) -> None:
        assert self._old_start is not None
        source = find_track(project, self.from_track_id)
        target = find_track(project, self.to_track_id)
        clip = find_clip(target, self.clip_id)
        if target is not source:
            _remove(target, clip.id)
            clip.timeline_start = self._old_start
            _insert(source, clip)
        else:
            clip.timeline_start = self._old_start
            source.clips.sort(key=lambda c: c.timeline_start)


class DeleteClip(Command):
    """Remove a clip. The whole clip is kept, so undo restores it identically."""

    label = "Delete clip"

    def __init__(self, track_id: str, clip_id: str) -> None:
        super().__init__()
        self.track_id = track_id
        self.clip_id = clip_id
        self._clip: Clip | None = None

    def do(self, project: Project) -> None:
        track = find_track(project, self.track_id)
        self._clip = find_clip(track, self.clip_id)
        _remove(track, self.clip_id)
        self.touches_audio = track.kind == "audio"

    def undo(self, project: Project) -> None:
        assert self._clip is not None
        _insert(find_track(project, self.track_id), self._clip)


class DuplicateClip(Command):
    """Copy a clip in immediately after the original.

    When something already sits in that space the copy goes to the first later
    position on the same track where it fits, rather than being refused. A
    duplicate that failed because the next clip happened to be butted against
    this one would be a puzzling refusal for a gesture whose whole point is
    "another one of these". The chosen position is stored, so undo and redo
    both use the same one.
    """

    label = "Duplicate clip"

    def __init__(self, track_id: str, clip_id: str) -> None:
        super().__init__()
        self.track_id = track_id
        self.clip_id = clip_id
        self.new_clip_id = _new_id()
        self._placed_at: int | None = None

    def do(self, project: Project) -> None:
        track = find_track(project, self.track_id)
        original = find_clip(track, self.clip_id)
        duration = original.duration

        start = self._placed_at
        if start is None:
            start = self._first_free_start(track, original.timeline_end, duration)
            self._placed_at = start

        _require_free(track, start, start + duration)
        _insert(
            track,
            Clip(
                id=self.new_clip_id,
                src=original.src,
                src_in=original.src_in,
                src_out=original.src_out,
                timeline_start=start,
                gain_db=original.gain_db,
            ),
        )
        self.touches_audio = track.kind == "audio"

    @staticmethod
    def _first_free_start(track: Track, preferred: int, duration: int) -> int:
        if free_span(track, preferred, preferred + duration):
            return preferred
        # Walk the clip ends after the preferred position. One of them is the
        # start of a gap wide enough, and the end of the last clip always is.
        candidates = sorted(
            {clip.timeline_end for clip in track.clips if clip.timeline_end > preferred}
        )
        for candidate in candidates:
            if free_span(track, candidate, candidate + duration):
                return candidate
        return track.duration

    def undo(self, project: Project) -> None:
        _remove(find_track(project, self.track_id), self.new_clip_id)


class SetClipGain(Command):
    """Change one clip's level. ``gain_db`` is decibels, not a time value."""

    label = "Set clip gain"

    def __init__(self, track_id: str, clip_id: str, new_gain_db: float) -> None:
        super().__init__()
        self.track_id = track_id
        self.clip_id = clip_id
        self.new_gain_db = float(new_gain_db)
        self._old_gain_db: float | None = None

    def do(self, project: Project) -> None:
        track = find_track(project, self.track_id)
        clip = find_clip(track, self.clip_id)
        self._old_gain_db = clip.gain_db
        clip.gain_db = self.new_gain_db
        self.touches_audio = track.kind == "audio"

    def undo(self, project: Project) -> None:
        assert self._old_gain_db is not None
        clip = find_clip(find_track(project, self.track_id), self.clip_id)
        clip.gain_db = self._old_gain_db


class AddClipFromMedia(Command):
    """Place a span of a source file on a track.

    This is what a drag from the media bin becomes. It does not check that the
    source has a stream of the track's kind, because it is not given the
    probe result; the caller that has the :class:`~core.model.MediaInfo` owns
    that refusal.
    """

    label = "Add clip"

    def __init__(
        self,
        track_id: str,
        src: Path | str,
        src_in: int,
        src_out: int,
        timeline_start: int,
    ) -> None:
        super().__init__()
        self.track_id = track_id
        self.src = Path(src)
        self.src_in = int(src_in)
        self.src_out = int(src_out)
        self.timeline_start = int(timeline_start)
        self.new_clip_id = _new_id()

    def do(self, project: Project) -> None:
        src_in, src_out, start = snap_all(
            project, self.src_in, self.src_out, self.timeline_start
        )
        track = find_track(project, self.track_id)

        if src_out <= src_in:
            raise CommandError("that source span is empty")
        if start < 0:
            raise CommandError("a clip cannot start before the timeline does")
        _require_free(track, start, start + (src_out - src_in))

        _insert(
            track,
            Clip(
                id=self.new_clip_id,
                src=self.src,
                src_in=src_in,
                src_out=src_out,
                timeline_start=start,
            ),
        )
        self.touches_audio = track.kind == "audio"

    def undo(self, project: Project) -> None:
        _remove(find_track(project, self.track_id), self.new_clip_id)


class RelinkMedia(Command):
    """Point clips at files that have moved.

    Addressed by clip id and nothing else. There is no track index and no clip
    index here on purpose: model order and lane order can diverge (removing and
    re-adding the video track leaves ``project.tracks`` as ``['A1', 'V1']``
    while the timeline still draws video on top), so an index taken from a
    dialog and applied to the model can land on the wrong clip, and both clips
    are valid so nothing raises.

    A relink usually covers several clips at once: every clip cut from one
    source shares its path, and the user chose the replacement once.

    Whether the new file exists is not checked. If it does not, the clips stay
    unresolved and keep painting as such, which is the same state they were
    already in and is recoverable by relinking again.
    """

    label = "Relink media"

    def __init__(self, new_sources: dict[str, Path | str]) -> None:
        super().__init__()
        #: clip id -> the path it should point at.
        self.new_sources = {
            clip_id: Path(src) for clip_id, src in new_sources.items()
        }
        self._old_sources: dict[str, Path] | None = None

    def do(self, project: Project) -> None:
        if not self.new_sources:
            raise CommandError("nothing to relink")

        # Resolve every id before touching anything, so a stale id leaves the
        # project untouched rather than half relinked.
        targets: list[tuple[Clip, Path]] = []
        touches_audio = False
        for clip_id, src in self.new_sources.items():
            found = _find_clip_anywhere(project, clip_id)
            if found is None:
                raise CommandError(f"no such clip: {clip_id}")
            track, clip = found
            targets.append((clip, src))
            touches_audio = touches_audio or track.kind == "audio"

        self._old_sources = {clip.id: clip.src for clip, _src in targets}
        for clip, src in targets:
            clip.src = src
        self.touches_audio = touches_audio

    def undo(self, project: Project) -> None:
        assert self._old_sources is not None
        for clip_id, src in self._old_sources.items():
            found = _find_clip_anywhere(project, clip_id)
            if found is not None:
                found[1].src = src


# -- track commands ---------------------------------------------------------


class SetTrackMuted(Command):
    label = "Mute track"

    def __init__(self, track_id: str, muted: bool) -> None:
        super().__init__()
        self.track_id = track_id
        self.muted = bool(muted)
        self._was_muted: bool | None = None
        self.label = "Mute track" if muted else "Unmute track"

    def do(self, project: Project) -> None:
        track = find_track(project, self.track_id)
        self._was_muted = track.muted
        track.muted = self.muted
        self.touches_audio = track.kind == "audio"

    def undo(self, project: Project) -> None:
        assert self._was_muted is not None
        find_track(project, self.track_id).muted = self._was_muted


class AddTrack(Command):
    """Add an empty track.

    v1 has no compositing. :mod:`core.filtergraph` raises when more than one
    video track carries clips, so a second video track is refused here, at the
    moment the user asks for it, rather than becoming a failure discovered at
    export time. Audio tracks are unlimited.

    The Track is built in ``__init__`` so that redo restores the same track id
    and any command pushed against it still resolves.
    """

    label = "Add track"

    def __init__(self, kind: Literal["video", "audio"], name: str) -> None:
        super().__init__()
        self.kind = kind
        self.name = name
        self.track = Track(name=name, kind=kind)
        self.track_id = self.track.id
        self.touches_audio = kind == "audio"

    def do(self, project: Project) -> None:
        if self.kind == "video" and project.video_tracks():
            raise CommandError(
                "Multiple video tracks require compositing, "
                "not supported in this version."
            )
        project.tracks.append(self.track)

    def undo(self, project: Project) -> None:
        for index, track in enumerate(project.tracks):
            if track.id == self.track_id:
                project.tracks.pop(index)
                return


class RemoveTrack(Command):
    """Delete a track, keeping the whole thing and the index it sat at."""

    label = "Remove track"

    def __init__(self, track_id: str) -> None:
        super().__init__()
        self.track_id = track_id
        self._track: Track | None = None
        self._index: int | None = None

    def do(self, project: Project) -> None:
        track = find_track(project, self.track_id)
        self._track = track
        self._index = project.tracks.index(track)
        project.tracks.pop(self._index)
        self.touches_audio = track.kind == "audio"

    def undo(self, project: Project) -> None:
        assert self._track is not None and self._index is not None
        project.tracks.insert(self._index, self._track)


class MacroCommand(Command):
    """Several commands applied, and reversed, as one.

    The multi-select shortcuts need this. Deleting three selected clips is one
    thing the user did, and one Ctrl+Z has to put all three back; three
    separate commands would leave two of them deleted and look like a bug.

    If any part refuses, the parts already applied are rolled back before the
    error is re-raised, so the all-or-nothing promise holds for failures too.
    """

    def __init__(self, label: str, commands: "list[Command]") -> None:
        super().__init__()
        if not commands:
            raise ValueError("a macro needs at least one command")
        self.label = label
        self.commands = list(commands)

    def do(self, project: Project) -> None:
        applied: list[Command] = []
        try:
            for command in self.commands:
                command.do(project)
                applied.append(command)
        except CommandError:
            for command in reversed(applied):
                command.undo(project)
            raise
        self.touches_audio = any(c.touches_audio for c in self.commands)

    def undo(self, project: Project) -> None:
        for command in reversed(self.commands):
            command.undo(project)


# -- the stack --------------------------------------------------------------


class CommandStack:
    """Undo and redo history. No Qt: the window wraps this and emits signals.

    Pushing runs the command. If it raises, nothing is recorded, so a refused
    edit leaves the history exactly as it was.
    """

    def __init__(self, limit: int = 200) -> None:
        self._done: list[Command] = []
        self._undone: list[Command] = []
        self._limit = limit

    def push(self, command: Command, project: Project) -> Command:
        command.do(project)
        self._done.append(command)
        # A redo branch is only meaningful while nothing has been done since;
        # a new edit is a new branch and the old one is unreachable.
        self._undone.clear()
        if len(self._done) > self._limit:
            del self._done[: len(self._done) - self._limit]
        return command

    def undo(self, project: Project) -> Command | None:
        if not self._done:
            return None
        command = self._done.pop()
        command.undo(project)
        self._undone.append(command)
        return command

    def redo(self, project: Project) -> Command | None:
        if not self._undone:
            return None
        command = self._undone.pop()
        command.do(project)
        self._done.append(command)
        return command

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()

    def can_undo(self) -> bool:
        return bool(self._done)

    def can_redo(self) -> bool:
        return bool(self._undone)

    def undo_label(self) -> str:
        return self._done[-1].label if self._done else ""

    def redo_label(self) -> str:
        return self._undone[-1].label if self._undone else ""

    def depth(self) -> int:
        return len(self._done)
