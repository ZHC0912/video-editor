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
    "clock_text",
    "export_progress",
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


def clock_text(seconds: float) -> str:
    """A wall-clock span for a progress line: 0:07, 2:31, 1:02:03.

    Not :func:`duration_text`, which is for media lengths and says "7.0s".
    A stopwatch that counts 7.0s, 8.0s, 9.0s reads as a measurement; a
    stopwatch that counts 0:07, 0:08, 0:09 reads as time passing.
    """
    if seconds < 0 or seconds != seconds:  # NaN compares false with itself
        return "0:00"
    whole = int(seconds)
    hours, whole = divmod(whole, 3600)
    minutes, secs = divmod(whole, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def export_progress(done_sec: float, total_sec: float, elapsed_sec: float) -> str:
    """The label under the export progress bar.

    Three facts: how much of the timeline is encoded, how long it has been
    running, and roughly how much longer it will take. The estimate is a
    straight extrapolation from the average rate so far, which is honest for
    a constant-bitrate source and wrong at the start of every export; it is
    therefore prefixed with "about" and is not shown at all until there is
    something to extrapolate from.
    """
    encoded = elapsed_of_total(done_sec, total_sec)
    elapsed = f"{clock_text(elapsed_sec)} elapsed"
    if done_sec <= 0 or total_sec <= 0 or elapsed_sec <= 0:
        return f"{encoded}  |  {elapsed}"
    remaining = elapsed_sec * (total_sec - done_sec) / done_sec
    if remaining <= 0:
        return f"{encoded}  |  {elapsed}"
    return f"{encoded}  |  {elapsed}, about {clock_text(remaining)} remaining"
