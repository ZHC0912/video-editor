"""Display-side conversion.

This is the only module under ``ui`` allowed to turn ticks into strings. If a
widget needs a number rendered for a human, it calls in here. Nothing in this
file imports Qt, so it is testable without a QApplication.
"""

from __future__ import annotations

from pathlib import Path

from core.model import MediaInfo
from core.timebase import TICKS_PER_SECOND, FrameRate, ticks_to_seconds
from core.timebase import ticks_to_timecode as _ticks_to_timecode

__all__ = [
    "timecode",
    "duration_text",
    "frame_rate_text",
    "resolution_text",
    "file_size_text",
    "media_summary",
    "elapsed_of_total",
]


def timecode(ticks: int, rate: FrameRate) -> str:
    """Non-drop HH:MM:SS:FF, for every timecode field in the application."""
    return _ticks_to_timecode(ticks, rate)


def duration_text(ticks: int) -> str:
    """A length for reading rather than for editing: 4.2s, 1:23, 1:02:03."""
    if ticks < 0:
        return "-" + duration_text(-ticks)

    total = ticks_to_seconds(ticks)
    if ticks < 60 * TICKS_PER_SECOND:
        return f"{total:.1f}s"

    whole = int(total)
    seconds = whole % 60
    minutes = (whole // 60) % 60
    hours = whole // 3600
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def frame_rate_text(rate: FrameRate | None) -> str:
    """30 fps for the whole rates, 29.97 fps for the NTSC ones."""
    if rate is None:
        return "no video"
    if rate.den == 1:
        return f"{rate.num} fps"
    return f"{rate.as_float:.2f} fps"


def resolution_text(width: int | None, height: int | None) -> str:
    if not width or not height:
        return "no video"
    return f"{width}x{height}"


def file_size_text(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def media_summary(info: MediaInfo) -> str:
    """The secondary line under a filename in the media bin."""
    parts = [duration_text(info.duration_ticks)]
    if info.has_video:
        parts.append(resolution_text(info.width, info.height))
        parts.append(frame_rate_text(info.frame_rate))
    if info.has_audio and info.sample_rate:
        parts.append(f"{info.sample_rate / 1000:g} kHz")
    if not info.has_audio:
        parts.append("silent")
    return "  ".join(parts)


def media_title(info: MediaInfo) -> str:
    return Path(info.path).name


def elapsed_of_total(done_sec: float, total_sec: float) -> str:
    """The label under an export progress bar."""
    done_ticks = round(done_sec * TICKS_PER_SECOND)
    total_ticks = round(total_sec * TICKS_PER_SECOND)
    return f"{duration_text(done_ticks)} of {duration_text(total_ticks)}"
