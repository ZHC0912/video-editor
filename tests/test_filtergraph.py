"""Filter graph tests.

These assert on the generated string, never on rendered output. Rendering is
covered once, slowly, in test_render.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.filtergraph import (
    NoAudioError,
    RenderError,
    build_audio_only,
    build_full,
)
from core.model import Clip, Project, Track
from core.timebase import TICKS_PER_SECOND as SEC
from core.timebase import FrameRate

W, H = 1920, 1080
SR = 48000


def clip(
    src: str = "a.mp4",
    start: int = 0,
    dur: int = SEC,
    src_in: int = 0,
    gain_db: float = 0.0,
) -> Clip:
    return Clip(
        src=Path(src),
        src_in=src_in,
        src_out=src_in + dur,
        timeline_start=start,
        gain_db=gain_db,
    )


def project(*tracks: Track, **kwargs) -> Project:
    return Project(name="p", tracks=list(tracks), **kwargs)


def chains(filter_complex: str) -> list[str]:
    return filter_complex.split(";")


def video_chains(filter_complex: str) -> list[str]:
    return [c for c in chains(filter_complex) if "[v" in c or "[vout]" in c]


def find(filter_complex: str, needle: str) -> list[str]:
    return [c for c in chains(filter_complex) if needle in c]


def inputs_of(input_args: list[str]) -> list[str]:
    assert input_args[0::2] == ["-i"] * (len(input_args) // 2)
    return input_args[1::2]


# --------------------------------------------------------------------------
# Video branch shape
# --------------------------------------------------------------------------

class TestVideoBranch:
    def test_single_clip_no_gaps(self) -> None:
        p = project(Track(name="V1", kind="video", clips=[clip(dur=SEC)]))
        args, fc, maps = build_full(p)

        assert inputs_of(args) == ["a.mp4"]
        assert maps == ["-map", "[vout]"]
        assert chains(fc) == [
            "[0:v]trim=start=0.000000:end=1.000000,setpts=PTS-STARTPTS,"
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
            "fps=30/1,format=yuv420p,setsar=1[v0]",
            "[v0]concat=n=1:v=1:a=0[vout]",
        ]

    def test_concat_of_one_is_kept_rather_than_special_cased(self) -> None:
        # The uniform code path matters more than the saved filter.
        p = project(Track(name="V1", kind="video", clips=[clip()]))
        _, fc, _ = build_full(p)
        assert "concat=n=1:v=1:a=0[vout]" in fc

    def test_two_clips_back_to_back(self) -> None:
        p = project(
            Track(
                name="V1",
                kind="video",
                clips=[clip("a.mp4", 0, SEC), clip("b.mp4", SEC, SEC)],
            )
        )
        _, fc, _ = build_full(p)
        assert "color=" not in fc
        assert "[0:v]" in fc and "[1:v]" in fc
        assert "[v0][v1]concat=n=2:v=1:a=0[vout]" in chains(fc)

    def test_gap_between_clips_becomes_black(self) -> None:
        p = project(
            Track(
                name="V1",
                kind="video",
                clips=[clip("a.mp4", 0, SEC), clip("b.mp4", 3 * SEC, SEC)],
            )
        )
        _, fc, _ = build_full(p)
        assert (
            f"color=c=black:s={W}x{H}:d=2.000000:r=30/1,format=yuv420p,setsar=1[v1]"
            in chains(fc)
        )
        assert "[v0][v1][v2]concat=n=3:v=1:a=0[vout]" in chains(fc)

    def test_leading_gap_becomes_black(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 2 * SEC, SEC)])
        )
        _, fc, _ = build_full(p)
        segments = video_chains(fc)
        # The black filler is first, the clip second.
        assert segments[0].startswith(f"color=c=black:s={W}x{H}:d=2.000000")
        assert segments[0].endswith("[v0]")
        assert segments[1].startswith("[0:v]")
        assert "[v0][v1]concat=n=2:v=1:a=0[vout]" in chains(fc)

    def test_project_geometry_and_rate_reach_the_string(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 2 * SEC, SEC)]),
            width=1280,
            height=720,
            frame_rate=FrameRate(30000, 1001),
        )
        _, fc, _ = build_full(p)
        assert "scale=1280:720:force_original_aspect_ratio=decrease" in fc
        assert "pad=1280:720:(ow-iw)/2:(oh-ih)/2" in fc
        assert "fps=30000/1001" in fc
        assert "color=c=black:s=1280x720" in fc
        assert ":r=30000/1001," in fc


# --------------------------------------------------------------------------
# The video branch runs to the project duration, not to the last video clip
# --------------------------------------------------------------------------

class TestTrailingVideoPad:
    def test_muted_audio_track_extends_the_black_tail(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 30 * SEC)]),
            Track(
                name="A1",
                kind="audio",
                muted=True,
                clips=[clip("music.wav", 0, 60 * SEC)],
            ),
        )
        assert p.duration == 60 * SEC

        _, fc, maps = build_full(p)
        assert (
            f"color=c=black:s={W}x{H}:d=30.000000:r=30/1,format=yuv420p,setsar=1[v1]"
            in chains(fc)
        )
        # The filler is a concat input, not a dangling chain.
        assert "[v0][v1]concat=n=2:v=1:a=0[vout]" in chains(fc)
        # And the muted track contributes no audio.
        assert maps == ["-map", "[vout]"]

    def test_unmuted_longer_audio_track_also_extends_the_tail(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 10 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("music.wav", 0, 25 * SEC)]),
        )
        _, fc, _ = build_full(p)
        assert f"color=c=black:s={W}x{H}:d=15.000000" in fc
        assert "concat=n=2:v=1:a=0[vout]" in fc

    def test_no_filler_when_video_already_reaches_the_end(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 10 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("music.wav", 0, 4 * SEC)]),
        )
        _, fc, _ = build_full(p)
        assert "color=" not in fc


# --------------------------------------------------------------------------
# Audio branch shape
# --------------------------------------------------------------------------

class TestAudioBranch:
    def test_one_audio_track_aliases_with_anull(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 2 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("music.wav", 0, 2 * SEC)]),
        )
        _, fc, maps = build_full(p)
        assert (
            f"[1:a]atrim=start=0.000000:end=2.000000,asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates={SR}:channel_layouts=stereo,volume=0.00dB[a0]"
            in chains(fc)
        )
        assert "[a0]concat=n=1:v=0:a=1[atrack0]" in chains(fc)
        assert "[atrack0]anull,apad=whole_dur=2.000000[aout]" in chains(fc)
        assert "amix" not in fc
        assert maps == ["-map", "[vout]", "-map", "[aout]"]

    def test_two_audio_tracks_mix_without_normalising(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 2 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("music.wav", 0, 2 * SEC)]),
            Track(name="A2", kind="audio", clips=[clip("vo.wav", 0, SEC)]),
        )
        _, fc, _ = build_full(p)
        assert (
            "[atrack0][atrack1]amix=inputs=2:normalize=0,"
            "apad=whole_dur=2.000000[aout]" in chains(fc)
        )
        assert "anull" not in fc

    def test_audio_segment_labels_do_not_collide_across_tracks(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 4 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", SEC, SEC)]),
            Track(name="A2", kind="audio", clips=[clip("vo.wav", 2 * SEC, SEC)]),
        )
        _, fc, _ = build_full(p)
        assert "[a0][a1]concat=n=2:v=0:a=1[atrack0]" in chains(fc)
        assert "[a2][a3]concat=n=2:v=0:a=1[atrack1]" in chains(fc)

    def test_audio_gap_uses_anullsrc(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 5 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 2 * SEC, SEC)]),
        )
        _, fc, _ = build_full(p)
        assert (
            f"anullsrc=r={SR}:cl=stereo,atrim=duration=2.000000,"
            "asetpts=PTS-STARTPTS[a0]" in chains(fc)
        )

    def test_audio_track_is_not_padded_to_its_own_tail(self) -> None:
        # The trailing silence of a short audio track is amix's job in an
        # export and apad's job in a bed, never a per track anullsrc.
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 10 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, SEC)]),
        )
        _, fc, _ = build_full(p)
        assert "anullsrc" not in fc

    def test_gain_reaches_the_volume_filter(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, SEC)]),
            Track(
                name="A1",
                kind="audio",
                clips=[clip("m.wav", 0, SEC, gain_db=-6.5)],
            ),
        )
        _, fc, _ = build_full(p)
        assert "volume=-6.50dB[a0]" in fc


# --------------------------------------------------------------------------
# Muting
# --------------------------------------------------------------------------

class TestMuting:
    def test_muted_audio_track_is_excluded_from_the_mix(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 2 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("keep.wav", 0, 2 * SEC)]),
            Track(
                name="A2",
                kind="audio",
                muted=True,
                clips=[clip("drop.wav", 0, 2 * SEC)],
            ),
        )
        args, fc, _ = build_full(p)
        assert inputs_of(args) == ["a.mp4", "keep.wav"]
        assert "drop.wav" not in " ".join(args)
        # One surviving track, so it aliases rather than mixes.
        assert "[atrack0]anull,apad=whole_dur=2.000000[aout]" in chains(fc)
        assert "atrack1" not in fc

    def test_muted_video_track_is_excluded_too(self) -> None:
        p = project(
            Track(
                name="V1",
                kind="video",
                muted=True,
                clips=[clip("hidden.mp4", 0, 2 * SEC)],
            ),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, 2 * SEC)]),
        )
        with pytest.raises(RenderError, match="audio-only"):
            build_full(p)

    def test_empty_audio_track_is_not_mixed(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, SEC)]),
            Track(name="A2", kind="audio"),
        )
        _, fc, _ = build_full(p)
        assert "amix" not in fc


# --------------------------------------------------------------------------
# Every audio track muted
# --------------------------------------------------------------------------

class TestAllAudioMuted:
    @pytest.fixture
    def silent(self) -> Project:
        return project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 10 * SEC)]),
            Track(
                name="A1",
                kind="audio",
                muted=True,
                clips=[clip("m.wav", 0, 10 * SEC)],
            ),
        )

    def test_build_full_produces_a_video_only_file(self, silent: Project) -> None:
        args, fc, maps = build_full(silent)
        assert maps == ["-map", "[vout]"]
        assert "[aout]" not in fc
        assert "amix" not in fc and "anull" not in fc
        assert inputs_of(args) == ["a.mp4"]

    def test_build_audio_only_refuses(self, silent: Project) -> None:
        assert silent.has_audio is False
        with pytest.raises(NoAudioError):
            build_audio_only(silent)

    def test_no_audio_error_is_a_render_error(self, silent: Project) -> None:
        # Playback catches the specific one so it can fall back to a wall
        # clock; a plain export caller can catch the base class.
        with pytest.raises(RenderError):
            build_audio_only(silent)


class TestVideoWithNoAudioAtAll:
    @pytest.fixture
    def video_only(self) -> Project:
        return project(Track(name="V1", kind="video", clips=[clip("a.mp4", 0, SEC)]))

    def test_build_full_omits_the_audio_map(self, video_only: Project) -> None:
        _, fc, maps = build_full(video_only)
        assert maps == ["-map", "[vout]"]
        assert "aout" not in fc

    def test_build_audio_only_refuses(self, video_only: Project) -> None:
        with pytest.raises(NoAudioError):
            build_audio_only(video_only)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

class TestInputs:
    def test_one_source_used_three_times_is_one_input(self) -> None:
        p = project(
            Track(
                name="V1",
                kind="video",
                clips=[
                    clip("a.mp4", 0, SEC),
                    clip("a.mp4", SEC, SEC),
                    clip("a.mp4", 2 * SEC, SEC),
                ],
            )
        )
        args, fc, _ = build_full(p)
        assert inputs_of(args) == ["a.mp4"]
        assert fc.count("[0:v]") == 3
        assert "[1:v]" not in fc

    def test_a_source_shared_by_video_and_audio_is_one_input(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("av.mp4", 0, SEC)]),
            Track(name="A1", kind="audio", clips=[clip("av.mp4", 0, SEC)]),
        )
        args, fc, _ = build_full(p)
        assert inputs_of(args) == ["av.mp4"]
        assert "[0:v]" in fc and "[0:a]" in fc

    def test_input_order_follows_first_use(self) -> None:
        p = project(
            Track(
                name="V1",
                kind="video",
                clips=[clip("second.mp4", 0, SEC), clip("first.mp4", SEC, SEC)],
            )
        )
        args, _, _ = build_full(p)
        assert inputs_of(args) == ["second.mp4", "first.mp4"]

    def test_audio_bed_never_opens_a_video_only_source(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("silent.mp4", 0, 5 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, 5 * SEC)]),
        )
        args, fc, maps = build_audio_only(p)
        assert inputs_of(args) == ["m.wav"]
        assert maps == ["-map", "[aout]"]
        assert ":v]" not in fc
        assert "scale=" not in fc


# --------------------------------------------------------------------------
# The bed's trailing pad
# --------------------------------------------------------------------------

class TestAudioPad:
    def test_apad_spans_the_whole_project(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 60 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, 5 * SEC)]),
        )
        _, fc, _ = build_audio_only(p)
        assert "[atrack0]anull,apad=whole_dur=60.000000[aout]" in chains(fc)

    def test_apad_follows_the_mix_with_several_tracks(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 60 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, 5 * SEC)]),
            Track(name="A2", kind="audio", clips=[clip("vo.wav", 0, 2 * SEC)]),
        )
        _, fc, _ = build_audio_only(p)
        assert (
            "[atrack0][atrack1]amix=inputs=2:normalize=0,"
            "apad=whole_dur=60.000000[aout]" in chains(fc)
        )

    def test_apad_counts_a_muted_track_because_project_duration_does(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 10 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, 4 * SEC)]),
            Track(
                name="A2",
                kind="audio",
                muted=True,
                clips=[clip("long.wav", 0, 90 * SEC)],
            ),
        )
        _, fc, _ = build_audio_only(p)
        assert "apad=whole_dur=90.000000[aout]" in fc

    def test_a_full_export_pads_its_audio_too(self) -> None:
        # 60s of video over 3s of audio. Without the pad the exported audio
        # stream would stop at 3s while the bed ran to 60s, and the bed would
        # no longer be a preview of the export.
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 60 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, 3 * SEC)]),
        )
        _, fc, _ = build_full(p)
        assert "apad=whole_dur=60.000000" in fc

    def test_both_builders_pad_to_the_same_value(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 47 * SEC)]),
            Track(name="A1", kind="audio", clips=[clip("m.wav", 0, 3 * SEC)]),
            Track(name="A2", kind="audio", clips=[clip("vo.wav", 5 * SEC, SEC)]),
        )
        _, full_fc, _ = build_full(p)
        _, bed_fc, _ = build_audio_only(p)

        assert find(full_fc, "apad=") == find(bed_fc, "apad=")
        assert find(full_fc, "apad=") == [
            "[atrack0][atrack1]amix=inputs=2:normalize=0,"
            "apad=whole_dur=47.000000[aout]"
        ]

    def test_no_pad_when_there_is_no_audio_at_all(self) -> None:
        p = project(Track(name="V1", kind="video", clips=[clip("a.mp4", 0, 60 * SEC)]))
        _, fc, maps = build_full(p)
        assert "apad" not in fc
        assert maps == ["-map", "[vout]"]


# --------------------------------------------------------------------------
# Frame snapping
# --------------------------------------------------------------------------

class TestSnapping:
    RATE = FrameRate(30, 1)  # 4000 ticks per frame

    def test_trim_points_round_to_the_nearest_frame(self) -> None:
        # 2001 ticks is just past the half frame mark, so it snaps up to frame 1
        # at 4000 ticks. Truncation would have produced 0.000000.
        p = project(
            Track(
                name="V1",
                kind="video",
                clips=[clip("a.mp4", 0, SEC, src_in=2001)],
            )
        )
        _, fc, _ = build_full(p)
        assert "trim=start=0.033333:" in fc

    def test_trim_points_round_down_below_the_half_frame(self) -> None:
        p = project(
            Track(
                name="V1",
                kind="video",
                clips=[clip("a.mp4", 0, SEC, src_in=1999)],
            )
        )
        _, fc, _ = build_full(p)
        assert "trim=start=0.000000:" in fc

    def test_gap_lengths_are_computed_from_snapped_ends(self) -> None:
        # The gap runs 4000 .. 6001 ticks. 6001 is past the half frame mark so
        # it snaps up to 8000, making the gap one whole frame, not half of one.
        p = project(
            Track(
                name="V1",
                kind="video",
                clips=[clip("a.mp4", 0, 4000), clip("b.mp4", 6001, SEC)],
            )
        )
        _, fc, _ = build_full(p)
        assert "d=0.033333:" in fc

    def test_adjacent_segments_still_tile_after_snapping(self) -> None:
        # Snapping each end independently would let a gap and its neighbours
        # drift apart. Both ends come from the same snapped values, so the
        # emitted lengths sum back to the snapped project duration.
        p = project(
            Track(
                name="V1",
                kind="video",
                clips=[clip("a.mp4", 0, 4000), clip("b.mp4", 6001, 4000)],
            )
        )
        _, fc, _ = build_full(p)
        assert "d=0.033333:" in fc  # gap 4000 .. 8000
        assert "trim=start=0.000000:end=0.033333" in fc  # first clip, one frame

    def test_seconds_are_formatted_to_six_places(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, SEC // 3 * 3)])
        )
        _, fc, _ = build_full(p)
        for token in fc.split(","):
            if token.startswith("trim=start="):
                value = token.split("=")[2].split(":")[0]
                assert len(value.split(".")[1]) == 6


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

class TestRefusals:
    def test_no_tracks(self) -> None:
        with pytest.raises(RenderError, match="no tracks"):
            build_full(project())
        with pytest.raises(RenderError, match="no tracks"):
            build_audio_only(project())

    def test_tracks_but_no_clips(self) -> None:
        p = project(Track(name="V1", kind="video"), Track(name="A1", kind="audio"))
        with pytest.raises(RenderError, match="no clips"):
            build_full(p)

    def test_audio_without_video_is_refused(self) -> None:
        p = project(Track(name="A1", kind="audio", clips=[clip("m.wav", 0, SEC)]))
        with pytest.raises(RenderError, match="audio-only"):
            build_full(p)
        with pytest.raises(RenderError, match="audio-only"):
            build_audio_only(p)

    def test_two_video_tracks_are_refused_rather_than_silently_dropped(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, SEC)]),
            Track(name="V2", kind="video", clips=[clip("b.mp4", 0, SEC)]),
        )
        with pytest.raises(RenderError, match="one video track"):
            build_full(p)

    def test_a_second_video_track_that_is_muted_is_fine(self) -> None:
        p = project(
            Track(name="V1", kind="video", clips=[clip("a.mp4", 0, SEC)]),
            Track(name="V2", kind="video", muted=True, clips=[clip("b.mp4", 0, SEC)]),
        )
        args, _, _ = build_full(p)
        assert inputs_of(args) == ["a.mp4"]
