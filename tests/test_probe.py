"""Probe tests. These shell out to the vendored ffmpeg/ffprobe, so they are slow.

Run `pytest -m "not slow"` to skip them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.binaries import BinaryNotFoundError, resolve_binary
from core.probe import ProbeError, probe
from core.timebase import TICKS_PER_SECOND, FrameRate

pytestmark = pytest.mark.slow


class TestResolveBinary:
    def test_finds_the_vendored_pair(self) -> None:
        for name in ("ffmpeg", "ffprobe"):
            path = resolve_binary(name)
            assert path.is_file()
            assert path.parent.name == "bin"

    def test_suffix_is_optional(self) -> None:
        assert resolve_binary("ffprobe") == resolve_binary("ffprobe.exe")

    def test_missing_binary_names_what_it_searched(self) -> None:
        with pytest.raises(BinaryNotFoundError, match="Searched"):
            resolve_binary("definitely-not-a-real-binary")


class TestProbeCombined:
    def test_reports_both_streams(self, av_file: Path) -> None:
        info = probe(av_file)
        assert info.has_video is True
        assert info.has_audio is True

    def test_geometry_and_rate(self, av_file: Path) -> None:
        info = probe(av_file)
        assert (info.width, info.height) == (320, 240)
        assert info.frame_rate == FrameRate(30, 1)
        assert info.sample_rate == 48000

    def test_duration_arrives_as_ticks(self, av_file: Path) -> None:
        info = probe(av_file)
        assert isinstance(info.duration_ticks, int)
        # Two seconds, give or take container rounding of a frame or two.
        assert abs(info.duration_ticks - 2 * TICKS_PER_SECOND) < TICKS_PER_SECOND // 4

    def test_path_is_absolute(self, av_file: Path) -> None:
        assert probe(av_file).path.is_absolute()


class TestProbeVideoOnly:
    def test_no_audio_stream_is_reported_honestly(self, video_only_file: Path) -> None:
        info = probe(video_only_file)
        assert info.has_video is True
        assert info.has_audio is False
        assert info.sample_rate is None

    def test_rate_is_rational(self, video_only_file: Path) -> None:
        info = probe(video_only_file)
        assert info.frame_rate == FrameRate(25, 1)
        assert info.frame_rate.ticks_per_frame == 4800
        assert (info.width, info.height) == (640, 480)


class TestProbeAudioOnly:
    def test_no_video_stream(self, audio_only_file: Path) -> None:
        info = probe(audio_only_file)
        assert info.has_audio is True
        assert info.has_video is False
        assert info.width is None
        assert info.height is None
        assert info.frame_rate is None

    def test_sample_rate_and_duration(self, audio_only_file: Path) -> None:
        info = probe(audio_only_file)
        assert info.sample_rate == 48000
        assert info.duration_ticks == TICKS_PER_SECOND


class TestProbeFailures:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ProbeError, match="no such media file"):
            probe(tmp_path / "nope.mp4")

    def test_file_that_is_not_media(self, tmp_path: Path) -> None:
        junk = tmp_path / "notes.txt"
        junk.write_text("this is not a movie", encoding="utf-8")
        with pytest.raises(ProbeError) as excinfo:
            probe(junk)
        assert excinfo.value.stderr is not None
