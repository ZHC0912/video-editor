"""Render tests.

The stderr parsing helpers are tested directly and cheaply. Everything that
actually starts ffmpeg is marked slow.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.model import Clip, Project, Track
from core.probe import probe
from core.encoders import NVENC_H264, SOFTWARE_H264
from core.render import (
    QUALITY_PRESETS,
    RESOLUTION_PRESETS,
    NoAudioError,
    RenderError,
    preset_size,
    render,
    render_audio_bed,
    video_encoder_args,
)
from core.render import _iter_lines, _parse_time
from core.timebase import TICKS_PER_SECOND as SEC
from core.timebase import FrameRate, ticks_to_seconds


def stream_duration(path: Path, kind: str) -> float:
    """Duration of one stream, in seconds, straight from ffprobe.

    core.probe reports the container duration, which is the longest stream.
    Checking that video and audio end together needs each stream separately.
    """
    import subprocess

    from core.binaries import resolve_binary

    completed = subprocess.run(
        [
            str(resolve_binary("ffprobe")),
            "-v",
            "error",
            "-select_streams",
            {"video": "v:0", "audio": "a:0"}[kind],
            "-show_entries",
            "stream=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(completed.stdout.strip())


class _FakeStream:
    """Hands out preset chunks the way a pipe would."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read1(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class TestStderrSplitting:
    def test_carriage_returns_separate_progress_lines(self) -> None:
        # ffmpeg rewrites its stats line with \r, so splitting on \n alone
        # would buffer the whole render into one line and progress would
        # never be reported.
        stream = _FakeStream([b"frame=1 time=00:00:01.00\rframe=2 time=00:00:02.00\r"])
        assert list(_iter_lines(stream)) == [
            "frame=1 time=00:00:01.00",
            "frame=2 time=00:00:02.00",
        ]

    def test_newlines_and_crlf_both_work(self) -> None:
        stream = _FakeStream([b"one\r\ntwo\nthree"])
        assert list(_iter_lines(stream)) == ["one", "two", "three"]

    def test_a_line_split_across_chunks_is_rejoined(self) -> None:
        stream = _FakeStream([b"time=00:0", b"0:05.00\r"])
        assert list(_iter_lines(stream)) == ["time=00:00:05.00"]

    def test_a_multibyte_character_split_across_chunks_survives(self) -> None:
        text = "café\n".encode("utf-8")
        stream = _FakeStream([text[:4], text[4:]])
        assert list(_iter_lines(stream)) == ["café"]

    def test_empty_stream(self) -> None:
        assert list(_iter_lines(_FakeStream([]))) == []


class TestTimeParsing:
    def test_reads_the_progress_field(self) -> None:
        assert _parse_time("frame=30 fps=15 time=00:00:01.00 bitrate=N/A") == 1.0
        assert _parse_time("time=01:02:03.50") == 3723.5

    def test_ignores_lines_without_a_time(self) -> None:
        assert _parse_time("Stream #0:0 -> #0:0 (h264 (native))") is None
        assert _parse_time("time=N/A") is None

    def test_ignores_the_negative_placeholder(self) -> None:
        # ffmpeg emits a huge negative timestamp before the first frame lands.
        assert _parse_time("time=-577014:32:22.77") is None


# --------------------------------------------------------------------------
# Integration. These run ffmpeg.
# --------------------------------------------------------------------------

@pytest.fixture
def two_clip_project(av_file: Path) -> Project:
    """Two one-second cuts from the same source, back to back, with audio."""
    return Project(
        name="integration",
        width=320,
        height=240,
        frame_rate=FrameRate(30, 1),
        sample_rate=48000,
        tracks=[
            Track(
                name="V1",
                kind="video",
                clips=[
                    Clip(src=av_file, src_in=0, src_out=SEC, timeline_start=0),
                    Clip(
                        src=av_file,
                        src_in=SEC,
                        src_out=2 * SEC,
                        timeline_start=SEC,
                    ),
                ],
            ),
            Track(
                name="A1",
                kind="audio",
                clips=[Clip(src=av_file, src_in=0, src_out=SEC, timeline_start=0)],
            ),
        ],
    )


@pytest.mark.slow
class TestRenderIntegration:
    def test_two_clip_render_lands_within_one_frame(
        self, two_clip_project: Project, tmp_path: Path
    ) -> None:
        out = tmp_path / "out.mp4"
        render(two_clip_project, out, threading.Event(), crf=28)

        assert out.is_file() and out.stat().st_size > 0
        info = probe(out)
        assert info.has_video and info.has_audio
        assert (info.width, info.height) == (320, 240)

        one_frame = int(two_clip_project.frame_rate.ticks_per_frame)
        assert abs(info.duration_ticks - two_clip_project.duration) <= one_frame

    def test_progress_is_reported_and_ends_at_the_total(
        self, two_clip_project: Project, tmp_path: Path
    ) -> None:
        seen: list[tuple[float, float]] = []
        render(
            two_clip_project,
            tmp_path / "out.mp4",
            threading.Event(),
            crf=28,
            on_progress=lambda done, total: seen.append((done, total)),
        )
        assert seen, "no progress was reported"
        total = ticks_to_seconds(two_clip_project.duration)
        assert all(t == total for _, t in seen)
        assert all(0.0 <= d <= total for d, _ in seen)
        assert seen[-1][0] == total

    def test_video_only_project_produces_a_file_without_audio(
        self, video_only_file: Path, tmp_path: Path
    ) -> None:
        p = Project(
            name="silent",
            width=320,
            height=240,
            frame_rate=FrameRate(25, 1),
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(
                            src=video_only_file,
                            src_in=0,
                            src_out=SEC,
                            timeline_start=0,
                        )
                    ],
                )
            ],
        )
        out = tmp_path / "silent.mp4"
        render(p, out, threading.Event(), crf=28)
        info = probe(out)
        assert info.has_video is True
        assert info.has_audio is False

    def test_a_gap_renders_as_black_and_extends_the_duration(
        self, av_file: Path, tmp_path: Path
    ) -> None:
        p = Project(
            name="gappy",
            width=320,
            height=240,
            frame_rate=FrameRate(30, 1),
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(src=av_file, src_in=0, src_out=SEC, timeline_start=0),
                        Clip(
                            src=av_file,
                            src_in=0,
                            src_out=SEC,
                            timeline_start=2 * SEC,
                        ),
                    ],
                )
            ],
        )
        out = tmp_path / "gappy.mp4"
        render(p, out, threading.Event(), crf=28)
        one_frame = int(p.frame_rate.ticks_per_frame)
        assert abs(probe(out).duration_ticks - 3 * SEC) <= one_frame

    def test_short_audio_under_long_video_still_fills_the_stream(
        self, av_file: Path, tmp_path: Path
    ) -> None:
        # 10s of video over 2s of audio. The audio branch pads, so the two
        # streams end together and the export matches what the bed will play.
        p = Project(
            name="padded",
            width=320,
            height=240,
            frame_rate=FrameRate(30, 1),
            sample_rate=48000,
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(
                            src=av_file,
                            src_in=0,
                            src_out=2 * SEC,
                            timeline_start=0,
                        ),
                        Clip(
                            src=av_file,
                            src_in=0,
                            src_out=2 * SEC,
                            timeline_start=8 * SEC,
                        ),
                    ],
                ),
                Track(
                    name="A1",
                    kind="audio",
                    clips=[
                        Clip(src=av_file, src_in=0, src_out=2 * SEC, timeline_start=0)
                    ],
                ),
            ],
        )
        assert p.duration == 10 * SEC

        out = tmp_path / "padded.mp4"
        render(p, out, threading.Event(), crf=28)

        video_sec = stream_duration(out, "video")
        audio_sec = stream_duration(out, "audio")
        one_frame = 1.0 / 30
        assert abs(video_sec - audio_sec) <= one_frame, (
            f"video ends at {video_sec}s but audio ends at {audio_sec}s"
        )
        assert abs(audio_sec - 10.0) <= one_frame

    def test_failure_carries_the_ffmpeg_stderr(
        self, tmp_path: Path, av_file: Path
    ) -> None:
        missing = tmp_path / "not-here.mp4"
        p = Project(
            name="broken",
            width=320,
            height=240,
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(src=missing, src_in=0, src_out=SEC, timeline_start=0)
                    ],
                )
            ],
        )
        out = tmp_path / "broken.mp4"
        with pytest.raises(RenderError) as excinfo:
            render(p, out, threading.Event())
        assert "ffmpeg exited with code" in str(excinfo.value)
        assert not out.exists()

    def test_cancelling_before_the_start_leaves_no_file(
        self, two_clip_project: Project, tmp_path: Path
    ) -> None:
        cancel = threading.Event()
        cancel.set()
        out = tmp_path / "cancelled.mp4"
        render(two_clip_project, out, cancel, crf=28)
        assert not out.exists()

    def test_crf_is_validated(self, two_clip_project: Project, tmp_path: Path) -> None:
        with pytest.raises(RenderError, match="crf"):
            render(two_clip_project, tmp_path / "x.mp4", threading.Event(), crf=99)


@pytest.mark.slow
class TestAudioBedIntegration:
    def test_bed_spans_the_whole_timeline_including_silence(
        self, av_file: Path, tmp_path: Path
    ) -> None:
        # One second of audio under a ten second video. The bed is the master
        # clock, so it has to be ten seconds long, not one.
        p = Project(
            name="bed",
            width=320,
            height=240,
            frame_rate=FrameRate(30, 1),
            sample_rate=48000,
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(
                            src=av_file,
                            src_in=0,
                            src_out=2 * SEC,
                            timeline_start=0,
                        ),
                        Clip(
                            src=av_file,
                            src_in=0,
                            src_out=2 * SEC,
                            timeline_start=8 * SEC,
                        ),
                    ],
                ),
                Track(
                    name="A1",
                    kind="audio",
                    clips=[
                        Clip(src=av_file, src_in=0, src_out=SEC, timeline_start=0)
                    ],
                ),
            ],
        )
        assert p.duration == 10 * SEC

        out = tmp_path / "bed.wav"
        render_audio_bed(p, out, threading.Event())

        info = probe(out)
        assert info.has_audio and not info.has_video
        assert info.sample_rate == 48000
        one_frame = int(p.frame_rate.ticks_per_frame)
        assert abs(info.duration_ticks - 10 * SEC) <= one_frame

    def test_a_sixty_second_bed_renders_in_under_three_seconds(
        self, av_file: Path, tmp_path: Path
    ) -> None:
        p = Project(
            name="long",
            width=1920,
            height=1080,
            frame_rate=FrameRate(30, 1),
            sample_rate=48000,
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(
                            src=av_file,
                            src_in=0,
                            src_out=2 * SEC,
                            timeline_start=58 * SEC,
                        )
                    ],
                ),
                Track(
                    name="A1",
                    kind="audio",
                    clips=[
                        Clip(
                            src=av_file,
                            src_in=0,
                            src_out=2 * SEC,
                            timeline_start=0,
                        )
                    ],
                ),
            ],
        )
        assert p.duration == 60 * SEC

        out = tmp_path / "long.wav"
        started = time.perf_counter()
        render_audio_bed(p, out, threading.Event())
        elapsed = time.perf_counter() - started

        assert elapsed < 3.0, f"audio bed took {elapsed:.2f}s"
        assert abs(probe(out).duration_ticks - 60 * SEC) <= 4000

    def test_bed_refuses_a_project_with_nothing_to_mix(
        self, video_only_file: Path, tmp_path: Path
    ) -> None:
        p = Project(
            name="silent",
            width=320,
            height=240,
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(
                            src=video_only_file,
                            src_in=0,
                            src_out=SEC,
                            timeline_start=0,
                        )
                    ],
                )
            ],
        )
        with pytest.raises(NoAudioError):
            render_audio_bed(p, tmp_path / "bed.wav", threading.Event())
        assert not (tmp_path / "bed.wav").exists()


class TestExportOptions:
    """Resolution presets and encoder arguments. Pure string and arithmetic."""

    @staticmethod
    def project(width: int, height: int) -> Project:
        return Project(name="p", width=width, height=height)

    def test_source_is_the_absence_of_a_choice(self) -> None:
        assert preset_size(self.project(1920, 1080), "Source") is None

    def test_an_unknown_preset_changes_nothing(self) -> None:
        assert preset_size(self.project(1920, 1080), "4K") is None

    def test_a_preset_that_matches_the_project_inserts_no_scaler(self) -> None:
        # Scaling by one is a wasted filter and a rounding opportunity.
        assert preset_size(self.project(1920, 1080), "1080p") is None

    def test_720p_from_a_1080p_project(self) -> None:
        assert preset_size(self.project(1920, 1080), "720p") == (1280, 720)

    def test_480p_from_a_1080p_project(self) -> None:
        assert preset_size(self.project(1920, 1080), "480p") == (854, 480)

    def test_width_follows_the_projects_own_shape(self) -> None:
        # A vertical project exports vertical, not letterboxed into 16:9.
        # 405 exactly, which is a tie between two even numbers; either is
        # right and the rounding rule picks the lower.
        assert preset_size(self.project(1080, 1920), "720p") == (404, 720)

    def test_a_four_by_three_project_keeps_its_shape(self) -> None:
        assert preset_size(self.project(640, 480), "720p") == (960, 720)

    def test_every_dimension_is_even(self) -> None:
        # H.264 chroma subsampling requires it; an odd width is a hard failure
        # from the encoder rather than a rounded picture.
        for width, height in ((1920, 1080), (1080, 1920), (1440, 1080), (1366, 768)):
            for preset in RESOLUTION_PRESETS:
                size = preset_size(self.project(width, height), preset)
                if size is not None:
                    assert size[0] % 2 == 0 and size[1] % 2 == 0, (width, height, preset)

    def test_the_quality_presets_are_the_three_offered(self) -> None:
        assert QUALITY_PRESETS == {"High": 18, "Medium": 20, "Small": 24}

    def test_the_software_encoder_uses_crf(self) -> None:
        args = video_encoder_args(SOFTWARE_H264, 20)
        assert args == ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]

    def test_the_hardware_encoder_uses_constant_quality_not_crf(self) -> None:
        args = video_encoder_args(NVENC_H264, 20)
        assert "-crf" not in args, "NVENC has no CRF; -cq is its equivalent"
        assert args[:2] == ["-c:v", "h264_nvenc"]
        assert "-cq" in args and args[args.index("-cq") + 1] == "20"

    def test_the_hardware_encoder_is_told_not_to_target_a_bitrate(self) -> None:
        # Without this NVENC applies a default bitrate cap and the quality
        # setting silently does nothing.
        args = video_encoder_args(NVENC_H264, 18)
        assert args[args.index("-b:v") + 1] == "0"

    def test_an_unknown_encoder_falls_back_to_software(self) -> None:
        assert video_encoder_args("h264_qsv", 20)[1] == SOFTWARE_H264
