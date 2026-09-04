"""The project data model.

Every time field in this module is an integer count of ticks
(see :mod:`core.timebase`). There is no time field here expressed as a decimal
number of seconds, and there must never be one. If a change seems to need one,
stop and raise it rather than adding it.

The one non-integer field in the file is ``gain_db``, which is a level in
decibels, not a time value.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.timebase import FrameRate

__all__ = [
    "MediaInfo",
    "Clip",
    "Track",
    "Project",
    "clip_at",
    "next_clip_after",
    "gaps_in",
]


def _new_id() -> str:
    return uuid.uuid4().hex


class MediaInfo(BaseModel):
    """What :mod:`core.probe` learned about one media file on disk."""

    model_config = ConfigDict(frozen=True)

    path: Path
    duration_ticks: int
    width: int | None = None
    height: int | None = None
    frame_rate: FrameRate | None = None
    has_video: bool = False
    has_audio: bool = False
    sample_rate: int | None = None


class Clip(BaseModel):
    """One trimmed span of a source file, placed at a position on the timeline."""

    id: str = Field(default_factory=_new_id)
    src: Path
    src_in: int
    src_out: int
    timeline_start: int
    gain_db: float = 0.0

    @field_validator("src_in", "timeline_start")
    @classmethod
    def _not_negative(cls, v: int, info) -> int:
        if v < 0:
            raise ValueError(f"{info.field_name} must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def _check_span(self) -> "Clip":
        if self.src_out <= self.src_in:
            raise ValueError(
                f"src_out must be greater than src_in, got "
                f"src_in={self.src_in} src_out={self.src_out}"
            )
        return self

    @property
    def duration(self) -> int:
        return self.src_out - self.src_in

    @property
    def timeline_end(self) -> int:
        return self.timeline_start + self.duration


class Track(BaseModel):
    """An ordered, non-overlapping lane of clips."""

    id: str = Field(default_factory=_new_id)
    name: str
    kind: Literal["video", "audio"]
    clips: list[Clip] = Field(default_factory=list)
    muted: bool = False

    @model_validator(mode="after")
    def _sort_and_check_overlap(self) -> "Track":
        self.clips.sort(key=lambda c: c.timeline_start)
        for earlier, later in zip(self.clips, self.clips[1:]):
            if later.timeline_start < earlier.timeline_end:
                raise ValueError(
                    f"clips overlap on track {self.name!r}: "
                    f"{earlier.id} ends at {earlier.timeline_end} but "
                    f"{later.id} starts at {later.timeline_start}"
                )
        return self

    @property
    def duration(self) -> int:
        return self.clips[-1].timeline_end if self.clips else 0


class Project(BaseModel):
    """A whole edit. Format settings are fixed at creation."""

    name: str
    width: int = 1920
    height: int = 1080
    frame_rate: FrameRate = FrameRate(30, 1)
    sample_rate: int = 48000
    tracks: list[Track] = Field(default_factory=list)

    @property
    def duration(self) -> int:
        """Ticks from zero to the end of the last clip on any track."""
        ends = [track.duration for track in self.tracks if track.clips]
        return max(ends) if ends else 0

    @property
    def has_audio(self) -> bool:
        """True when at least one unmuted audio track holds at least one clip."""
        return any(
            track.kind == "audio" and not track.muted and track.clips
            for track in self.tracks
        )

    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "video"]

    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "audio"]


def clip_at(track: Track, t: int) -> Clip | None:
    """The clip covering tick ``t``, half open: start <= t < end."""
    for clip in track.clips:
        if clip.timeline_start <= t < clip.timeline_end:
            return clip
        if clip.timeline_start > t:
            break
    return None


def next_clip_after(track: Track, t: int) -> Clip | None:
    """The first clip starting strictly after ``t``.

    Phase 5 uses this to preload the following clip into the idle player.
    """
    for clip in track.clips:
        if clip.timeline_start > t:
            return clip
    return None


def gaps_in(track: Track, until: int) -> list[tuple[int, int]]:
    """Half open ``(start, end)`` spans of ``[0, until)`` that no clip covers.

    Includes a leading gap when the first clip does not start at zero, and a
    trailing gap when the last clip ends before ``until``. Empty spans are
    never returned.
    """
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for clip in track.clips:
        if clip.timeline_start >= until:
            break
        if clip.timeline_start > cursor:
            gaps.append((cursor, clip.timeline_start))
        cursor = max(cursor, clip.timeline_end)
    if cursor < until:
        gaps.append((cursor, until))
    return gaps
