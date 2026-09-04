"""Display formatting. No Qt, so these run without a QApplication."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.model import MediaInfo
from core.timebase import TICKS_PER_SECOND as SEC
from core.timebase import FrameRate
from ui import format_utils


class TestTimecode:
    def test_delegates_to_core(self) -> None:
        assert format_utils.timecode(0, FrameRate(25, 1)) == "00:00:00:00"
        assert format_utils.timecode(SEC, FrameRate(25, 1)) == "00:00:01:00"

    def test_ntsc_rate(self) -> None:
        assert format_utils.timecode(SEC, FrameRate(30000, 1001)) == "00:00:00:29"


class TestDurationText:
    @pytest.mark.parametrize(
        "ticks,expected",
        [
            (0, "0.0s"),
            (SEC // 2, "0.5s"),
            (SEC, "1.0s"),
            (42 * SEC, "42.0s"),
            (60 * SEC, "1:00"),
            (83 * SEC, "1:23"),
            (3600 * SEC, "1:00:00"),
            (3723 * SEC, "1:02:03"),
        ],
    )
    def test_reads_naturally(self, ticks: int, expected: str) -> None:
        assert format_utils.duration_text(ticks) == expected

    def test_negative(self) -> None:
        assert format_utils.duration_text(-SEC) == "-1.0s"


class TestFrameRateText:
    def test_whole_rates_have_no_decimals(self) -> None:
        assert format_utils.frame_rate_text(FrameRate(30, 1)) == "30 fps"
        assert format_utils.frame_rate_text(FrameRate(24, 1)) == "24 fps"

    def test_ntsc_rates_show_two_decimals(self) -> None:
        assert format_utils.frame_rate_text(FrameRate(30000, 1001)) == "29.97 fps"
        assert format_utils.frame_rate_text(FrameRate(24000, 1001)) == "23.98 fps"

    def test_none(self) -> None:
        assert format_utils.frame_rate_text(None) == "no video"


class TestResolutionText:
    def test_pair(self) -> None:
        assert format_utils.resolution_text(1920, 1080) == "1920x1080"

    def test_missing(self) -> None:
        assert format_utils.resolution_text(None, None) == "no video"
        assert format_utils.resolution_text(1920, None) == "no video"


class TestFileSizeText:
    @pytest.mark.parametrize(
        "size,expected",
        [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
    )
    def test_units(self, size: int, expected: str) -> None:
        assert format_utils.file_size_text(size) == expected


class TestMediaSummary:
    def test_combined_file(self) -> None:
        info = MediaInfo(
            path=Path("a.mp4"),
            duration_ticks=90 * SEC,
            width=1920,
            height=1080,
            frame_rate=FrameRate(30000, 1001),
            has_video=True,
            has_audio=True,
            sample_rate=48000,
        )
        summary = format_utils.media_summary(info)
        assert "1:30" in summary
        assert "1920x1080" in summary
        assert "29.97 fps" in summary
        assert "48 kHz" in summary

    def test_silent_video_says_so(self) -> None:
        info = MediaInfo(
            path=Path("a.mp4"),
            duration_ticks=SEC,
            width=640,
            height=480,
            frame_rate=FrameRate(25, 1),
            has_video=True,
            has_audio=False,
        )
        assert "silent" in format_utils.media_summary(info)

    def test_audio_only(self) -> None:
        info = MediaInfo(
            path=Path("a.wav"),
            duration_ticks=SEC,
            has_audio=True,
            sample_rate=44100,
        )
        summary = format_utils.media_summary(info)
        assert "44.1 kHz" in summary
        assert "x" not in summary

    def test_title_is_the_filename(self) -> None:
        info = MediaInfo(path=Path("C:/media/take one.mp4"), duration_ticks=SEC)
        assert format_utils.media_title(info) == "take one.mp4"


class TestElapsedOfTotal:
    def test_reads_as_a_sentence_fragment(self) -> None:
        assert format_utils.elapsed_of_total(12.0, 60.0) == "12.0s of 1:00"
