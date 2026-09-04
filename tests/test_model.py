"""Model tests: validation refuses bad edits, geometry helpers agree with it."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.model import (
    Clip,
    MediaInfo,
    Project,
    Track,
    clip_at,
    gaps_in,
    next_clip_after,
)
from core.timebase import FrameRate

SRC = Path("C:/media/a.mp4")


def clip(start: int, length: int, *, src: Path = SRC, ident: str | None = None) -> Clip:
    kwargs = {"id": ident} if ident else {}
    return Clip(src=src, src_in=0, src_out=length, timeline_start=start, **kwargs)


class TestClip:
    def test_derived_spans(self) -> None:
        c = Clip(src=SRC, src_in=1000, src_out=5000, timeline_start=2000)
        assert c.duration == 4000
        assert c.timeline_end == 6000

    def test_ids_are_unique_by_default(self) -> None:
        assert clip(0, 10).id != clip(0, 10).id

    def test_rejects_zero_length(self) -> None:
        with pytest.raises(ValueError):
            Clip(src=SRC, src_in=1000, src_out=1000, timeline_start=0)

    def test_rejects_reversed_span(self) -> None:
        with pytest.raises(ValueError):
            Clip(src=SRC, src_in=5000, src_out=1000, timeline_start=0)

    def test_rejects_negative_timeline_start(self) -> None:
        with pytest.raises(ValueError):
            Clip(src=SRC, src_in=0, src_out=100, timeline_start=-1)

    def test_rejects_negative_src_in(self) -> None:
        with pytest.raises(ValueError):
            Clip(src=SRC, src_in=-1, src_out=100, timeline_start=0)

    def test_gain_defaults_to_unity(self) -> None:
        assert clip(0, 100).gain_db == 0.0


class TestTrackValidation:
    def test_rejects_overlap(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            Track(name="V1", kind="video", clips=[clip(0, 1000), clip(999, 1000)])

    def test_rejects_overlap_regardless_of_input_order(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            Track(name="V1", kind="video", clips=[clip(999, 1000), clip(0, 1000)])

    def test_rejects_full_containment(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            Track(name="V1", kind="video", clips=[clip(0, 5000), clip(1000, 100)])

    def test_butt_joined_clips_are_legal(self) -> None:
        track = Track(name="V1", kind="video", clips=[clip(0, 1000), clip(1000, 1000)])
        assert track.duration == 2000

    def test_clips_are_sorted_on_validation(self) -> None:
        track = Track(
            name="V1",
            kind="video",
            clips=[clip(4000, 100), clip(0, 100), clip(2000, 100)],
        )
        assert [c.timeline_start for c in track.clips] == [0, 2000, 4000]

    def test_empty_track_has_zero_duration(self) -> None:
        assert Track(name="V1", kind="video").duration == 0

    def test_kind_is_constrained(self) -> None:
        with pytest.raises(ValueError):
            Track(name="X", kind="midi")  # type: ignore[arg-type]


class TestLookups:
    @pytest.fixture
    def track(self) -> Track:
        return Track(
            name="V1",
            kind="video",
            clips=[
                clip(1000, 1000, ident="a"),  # 1000 .. 2000
                clip(5000, 2000, ident="b"),  # 5000 .. 7000
                clip(7000, 1000, ident="c"),  # 7000 .. 8000
            ],
        )

    def test_clip_at_is_half_open(self, track: Track) -> None:
        assert clip_at(track, 999) is None
        assert clip_at(track, 1000).id == "a"
        assert clip_at(track, 1999).id == "a"
        assert clip_at(track, 2000) is None
        assert clip_at(track, 7000).id == "c"

    def test_clip_at_past_the_end(self, track: Track) -> None:
        assert clip_at(track, 100000) is None

    def test_clip_at_on_an_empty_track(self) -> None:
        assert clip_at(Track(name="V1", kind="video"), 0) is None

    def test_next_clip_after_is_strict(self, track: Track) -> None:
        assert next_clip_after(track, 0).id == "a"
        assert next_clip_after(track, 1000).id == "b"
        assert next_clip_after(track, 4999).id == "b"
        assert next_clip_after(track, 5000).id == "c"
        assert next_clip_after(track, 7000) is None

    def test_next_clip_after_on_an_empty_track(self) -> None:
        assert next_clip_after(Track(name="V1", kind="video"), 0) is None


class TestGaps:
    def test_leading_gap_is_included(self) -> None:
        track = Track(name="V1", kind="video", clips=[clip(1000, 1000)])
        assert gaps_in(track, 2000) == [(0, 1000)]

    def test_middle_and_trailing_gaps(self) -> None:
        track = Track(name="V1", kind="video", clips=[clip(0, 1000), clip(3000, 1000)])
        assert gaps_in(track, 6000) == [(1000, 3000), (4000, 6000)]

    def test_leading_middle_and_trailing_together(self) -> None:
        track = Track(name="V1", kind="video", clips=[clip(500, 500), clip(3000, 1000)])
        assert gaps_in(track, 5000) == [(0, 500), (1000, 3000), (4000, 5000)]

    def test_no_gaps_when_the_track_is_solid(self) -> None:
        track = Track(name="V1", kind="video", clips=[clip(0, 1000), clip(1000, 1000)])
        assert gaps_in(track, 2000) == []

    def test_empty_track_is_one_whole_gap(self) -> None:
        assert gaps_in(Track(name="V1", kind="video"), 5000) == [(0, 5000)]

    def test_zero_length_request_yields_nothing(self) -> None:
        assert gaps_in(Track(name="V1", kind="video"), 0) == []

    def test_gaps_and_clips_tile_the_span_exactly(self) -> None:
        track = Track(name="V1", kind="video", clips=[clip(500, 500), clip(3000, 1000)])
        until = 5000
        covered = sum(c.duration for c in track.clips)
        covered += sum(end - start for start, end in gaps_in(track, until))
        assert covered == until


class TestProject:
    def test_defaults(self) -> None:
        project = Project(name="Untitled")
        assert (project.width, project.height) == (1920, 1080)
        assert project.frame_rate == FrameRate(30, 1)
        assert project.sample_rate == 48000
        assert project.tracks == []

    def test_duration_is_the_longest_track(self) -> None:
        project = Project(
            name="p",
            tracks=[
                Track(name="V1", kind="video", clips=[clip(0, 1000)]),
                Track(name="A1", kind="audio", clips=[clip(2000, 3000)]),
            ],
        )
        assert project.duration == 5000

    def test_duration_of_an_empty_project(self) -> None:
        assert Project(name="p").duration == 0
        assert Project(name="p", tracks=[Track(name="V1", kind="video")]).duration == 0

    def test_duration_counts_muted_tracks(self) -> None:
        # Muting affects the mix, not how long the timeline is.
        project = Project(
            name="p",
            tracks=[Track(name="A1", kind="audio", muted=True, clips=[clip(0, 9000)])],
        )
        assert project.duration == 9000

    def test_has_audio(self) -> None:
        assert not Project(name="p").has_audio
        video_only = Project(
            name="p", tracks=[Track(name="V1", kind="video", clips=[clip(0, 100)])]
        )
        assert not video_only.has_audio

    def test_has_audio_ignores_muted_and_empty_tracks(self) -> None:
        muted = Project(
            name="p",
            tracks=[Track(name="A1", kind="audio", muted=True, clips=[clip(0, 100)])],
        )
        assert not muted.has_audio
        empty = Project(name="p", tracks=[Track(name="A1", kind="audio")])
        assert not empty.has_audio
        live = Project(
            name="p", tracks=[Track(name="A1", kind="audio", clips=[clip(0, 100)])]
        )
        assert live.has_audio

    def test_track_kind_helpers(self) -> None:
        project = Project(
            name="p",
            tracks=[
                Track(name="V1", kind="video"),
                Track(name="A1", kind="audio"),
                Track(name="A2", kind="audio"),
            ],
        )
        assert [t.name for t in project.video_tracks()] == ["V1"]
        assert [t.name for t in project.audio_tracks()] == ["A1", "A2"]


class TestMediaInfo:
    def test_audio_only_shape(self) -> None:
        info = MediaInfo(
            path=SRC, duration_ticks=120000, has_audio=True, sample_rate=48000
        )
        assert info.has_video is False
        assert info.width is None
        assert info.frame_rate is None

    def test_frame_rate_stays_rational(self) -> None:
        info = MediaInfo(
            path=SRC,
            duration_ticks=120000,
            has_video=True,
            frame_rate=FrameRate(30000, 1001),
        )
        assert info.frame_rate.num == 30000
        assert info.frame_rate.den == 1001
