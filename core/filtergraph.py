"""Turning a Project into an FFmpeg filter graph.

Two entry points, one builder. :func:`build_full` produces the graph for an
export; :func:`build_audio_only` produces the graph for the preview audio bed
that Phase 5 uses as its master clock. They differ in exactly one way: the
video branch is omitted. The audio branch is identical in both, down to the
trailing pad, because the bed is meant to be the export's audio.

Everything in this module is string construction. Nothing here runs FFmpeg.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.model import Clip, Project, Track, gaps_in
from core.timebase import FrameRate, snap_to_frame, ticks_to_seconds

__all__ = ["RenderError", "NoAudioError", "build_full", "build_audio_only"]


class RenderError(RuntimeError):
    """The project cannot be turned into a command, or FFmpeg refused it."""


class NoAudioError(RenderError):
    """No unmuted audio track holds a clip, so there is no bed to render.

    A subclass of :class:`RenderError` so a caller that only wants to know
    whether the render worked can catch the base class. Phase 5 catches this
    specifically and falls back to a wall clock.
    """


# --------------------------------------------------------------------------
# Tick to argument conversion
# --------------------------------------------------------------------------

def _snap(ticks: int, rate: FrameRate) -> int:
    """The one place a tick value is quantised to the frame grid.

    Round to nearest, not truncate: these are trim points, and a trim point
    more than half a frame past a boundary belongs to the next frame.
    """
    return snap_to_frame(ticks, rate)


def _secs(ticks: int, rate: FrameRate) -> str:
    """A tick position as a seconds argument. The last possible moment."""
    return f"{ticks_to_seconds(_snap(ticks, rate)):.6f}"


def _span_secs(start: int, end: int, rate: FrameRate) -> str:
    """The length of the span from start to end, as a seconds argument.

    Both ends are snapped before the subtraction, so adjacent segments still
    tile the timeline exactly after quantisation.
    """
    return f"{ticks_to_seconds(_snap(end, rate) - _snap(start, rate)):.6f}"


def _db(gain_db: float) -> str:
    return f"{gain_db:.2f}"


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

class _Inputs:
    """The ordered, deduplicated input list.

    A source used by three clips is one input referenced three times. Files are
    registered as they are first needed, so the list never carries an input
    that no filter references.
    """

    def __init__(self) -> None:
        self._order: list[Path] = []
        self._index: dict[str, int] = {}

    def index_of(self, path: Path) -> int:
        key = os.path.normcase(str(path))
        if key not in self._index:
            self._index[key] = len(self._order)
            self._order.append(path)
        return self._index[key]

    def args(self) -> list[str]:
        args: list[str] = []
        for path in self._order:
            args += ["-i", str(path)]
        return args


# --------------------------------------------------------------------------
# Segment layout
# --------------------------------------------------------------------------

_Segment = tuple[int, "Clip | None", int]


def _segments(track: Track, until: int) -> list[_Segment]:
    """Clips and gaps in timeline order, tiling the span from 0 to until.

    Each item is (start, clip or None, end). Clips and the gaps between them
    are disjoint and cover the whole span, so sorting on the start tick is
    unambiguous.
    """
    items: list[_Segment] = [(c.timeline_start, c, c.timeline_end) for c in track.clips]
    items += [(start, None, end) for start, end in gaps_in(track, until)]
    items.sort(key=lambda item: item[0])
    return items


# --------------------------------------------------------------------------
# Branches
# --------------------------------------------------------------------------

def _video_chains(track: Track, project: Project, inputs: _Inputs) -> list[str]:
    rate = project.frame_rate
    w, h = project.width, project.height
    chains: list[str] = []
    labels: list[str] = []

    # until=project.duration, not track.duration: a muted audio track still
    # extends the timeline, and the video branch has to reach the same end or
    # the export finishes with an audio stream playing over nothing.
    for n, (start, clip, end) in enumerate(_segments(track, project.duration)):
        label = f"v{n}"
        if clip is None:
            chains.append(
                f"color=c=black:s={w}x{h}:d={_span_secs(start, end, rate)}"
                f":r={rate.num}/{rate.den},format=yuv420p,setsar=1[{label}]"
            )
        else:
            idx = inputs.index_of(clip.src)
            chains.append(
                f"[{idx}:v]trim=start={_secs(clip.src_in, rate)}"
                f":end={_secs(clip.src_out, rate)},"
                f"setpts=PTS-STARTPTS,"
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={rate.num}/{rate.den},"
                f"format=yuv420p,setsar=1[{label}]"
            )
        labels.append(label)

    joined = "".join(f"[{label}]" for label in labels)
    chains.append(f"{joined}concat=n={len(labels)}:v=1:a=0[vout]")
    return chains


def _audio_chains(tracks: list[Track], project: Project, inputs: _Inputs) -> list[str]:
    rate = project.frame_rate
    sr = project.sample_rate
    chains: list[str] = []
    track_labels: list[str] = []
    counter = 0

    for track_n, track in enumerate(tracks):
        seg_labels: list[str] = []
        # until=track.duration: a track only has to reach its own last clip.
        # amix carries the shorter tracks, and apad handles the tail.
        for start, clip, end in _segments(track, track.duration):
            label = f"a{counter}"
            counter += 1
            if clip is None:
                chains.append(
                    f"anullsrc=r={sr}:cl=stereo,"
                    f"atrim=duration={_span_secs(start, end, rate)},"
                    f"asetpts=PTS-STARTPTS[{label}]"
                )
            else:
                idx = inputs.index_of(clip.src)
                chains.append(
                    f"[{idx}:a]atrim=start={_secs(clip.src_in, rate)}"
                    f":end={_secs(clip.src_out, rate)},"
                    f"asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates={sr}:channel_layouts=stereo,"
                    f"volume={_db(clip.gain_db)}dB[{label}]"
                )
            seg_labels.append(label)

        track_label = f"atrack{track_n}"
        joined = "".join(f"[{label}]" for label in seg_labels)
        chains.append(f"{joined}concat=n={len(seg_labels)}:v=0:a=1[{track_label}]")
        track_labels.append(track_label)

    # Both builders pad to the full project duration, and they must, because
    # the bed is meant to be the export's audio. Phase 5 plays the bed as its
    # master clock so it has to span trailing silence; if the export stopped
    # its audio at the last audio clip instead, preview and export would be
    # different lengths and the bed would stop being a preview of anything.
    tail = f",apad=whole_dur={_secs(project.duration, rate)}"
    joined = "".join(f"[{label}]" for label in track_labels)
    if len(track_labels) == 1:
        chains.append(f"{joined}anull{tail}[aout]")
    else:
        chains.append(f"{joined}amix=inputs={len(track_labels)}:normalize=0{tail}[aout]")
    return chains


# --------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------

def _build(project: Project, include_video: bool) -> tuple[list[str], str, list[str]]:
    if not project.tracks:
        raise RenderError(
            "project has no tracks: add a video track holding at least one clip"
        )

    video_tracks = [t for t in project.video_tracks() if t.clips and not t.muted]
    audio_tracks = [t for t in project.audio_tracks() if t.clips and not t.muted]

    if not video_tracks:
        if audio_tracks:
            raise RenderError(
                "audio-only projects are not supported in v1: "
                "add a video track holding at least one clip"
            )
        raise RenderError("project has no clips on any unmuted track")
    if len(video_tracks) > 1:
        names = ", ".join(repr(t.name) for t in video_tracks)
        raise RenderError(
            f"v1 renders one video track, found {len(video_tracks)} carrying clips "
            f"({names}). Compositing needs an overlay chain, which does not exist yet."
        )

    # has_audio is the model's own answer to "is there anything to mix", and it
    # is what the caller checked before asking for a bed. Stay consistent with it.
    if not include_video and not project.has_audio:
        raise NoAudioError(
            "project has no unmuted audio track holding a clip, so there is no "
            "audio bed to render"
        )

    inputs = _Inputs()
    chains: list[str] = []
    maps: list[str] = []

    if include_video:
        chains += _video_chains(video_tracks[0], project, inputs)
        maps += ["-map", "[vout]"]

    if audio_tracks:
        chains += _audio_chains(audio_tracks, project, inputs)
        maps += ["-map", "[aout]"]

    return inputs.args(), ";".join(chains), maps


def build_full(project: Project) -> tuple[list[str], str, list[str]]:
    """Input args, filter_complex and map args for a full export.

    A project with no audio on any unmuted track produces a video-only file:
    the audio branch and its map are omitted entirely rather than mixing
    silence.
    """
    return _build(project, include_video=True)


def build_audio_only(project: Project) -> tuple[list[str], str, list[str]]:
    """Input args, filter_complex and map args for the preview audio bed.

    The video branch is not built and the video streams are never referenced,
    so only sources actually carrying audio become inputs. Raises
    :class:`NoAudioError` when there is nothing to mix.
    """
    return _build(project, include_video=False)
