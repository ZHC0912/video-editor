"""Reading media metadata with the vendored ffprobe.

This is one of the two import boundaries where seconds become ticks. ffprobe
reports durations as decimal seconds; :func:`probe` converts once, here, and
everything downstream is integer ticks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from core.binaries import resolve_binary
from core.model import MediaInfo
from core.timebase import FrameRate, seconds_to_ticks

__all__ = ["ProbeError", "probe"]

# Keep the console window from flashing on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class ProbeError(RuntimeError):
    """ffprobe failed, or returned something unusable."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(f"{message}\n{stderr}".rstrip())
        self.stderr = stderr


def _run_ffprobe(path: Path) -> dict[str, Any]:
    ffprobe = resolve_binary("ffprobe")
    cmd = [
        str(ffprobe),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise ProbeError(f"ffprobe failed for {path}", completed.stderr or "")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(
            f"ffprobe returned output that is not JSON for {path}: {exc}",
            completed.stderr or "",
        ) from exc


def _first_video_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        # Cover art in an audio file presents as a one frame video stream.
        if stream.get("disposition", {}).get("attached_pic"):
            continue
        return stream
    return None


def _first_audio_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stream in streams:
        if stream.get("codec_type") == "audio":
            return stream
    return None


def _duration_seconds(data: dict[str, Any], streams: list[dict[str, Any]]) -> float:
    """Longest duration ffprobe reports, container first then streams."""
    candidates: list[float] = []
    container = data.get("format", {}).get("duration")
    if container not in (None, "N/A"):
        try:
            candidates.append(float(container))
        except (TypeError, ValueError):
            pass
    for stream in streams:
        value = stream.get("duration")
        if value in (None, "N/A"):
            continue
        try:
            candidates.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(candidates) if candidates else 0.0


def _int_or_none(value: Any) -> int | None:
    if value in (None, "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe(path: Path) -> MediaInfo:
    """Inspect a media file. Handles video only, audio only and combined files."""
    path = Path(path)
    if not path.is_file():
        raise ProbeError(f"no such media file: {path}")

    data = _run_ffprobe(path)
    streams: list[dict[str, Any]] = data.get("streams") or []
    if not streams:
        raise ProbeError(f"ffprobe found no streams in {path}")

    video = _first_video_stream(streams)
    audio = _first_audio_stream(streams)
    if video is None and audio is None:
        raise ProbeError(f"{path} holds neither a video nor an audio stream")

    frame_rate: FrameRate | None = None
    if video is not None:
        raw_rate = video.get("r_frame_rate") or video.get("avg_frame_rate")
        if raw_rate and raw_rate not in ("0/0", "N/A"):
            try:
                frame_rate = FrameRate.from_ffprobe(raw_rate)
            except ValueError:
                frame_rate = None

    return MediaInfo(
        path=path.resolve(),
        duration_ticks=seconds_to_ticks(_duration_seconds(data, streams)),
        width=_int_or_none(video.get("width")) if video else None,
        height=_int_or_none(video.get("height")) if video else None,
        frame_rate=frame_rate,
        has_video=video is not None,
        has_audio=audio is not None,
        sample_rate=_int_or_none(audio.get("sample_rate")) if audio else None,
    )
