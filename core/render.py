"""Running FFmpeg.

Two jobs: the full export, and the preview audio bed. Both are long running and
cancellable, both report progress, and neither may be called from the GUI
thread.

The filter graph itself is built in :mod:`core.filtergraph`. This module only
assembles the surrounding command and manages the subprocess.
"""

from __future__ import annotations

import codecs
import re
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path

from core.binaries import resolve_binary
from core.encoders import NVENC_H264, SOFTWARE_H264
from core.filtergraph import (
    NoAudioError,
    RenderError,
    build_audio_only,
    build_full,
)
from core.model import Project
from core.timebase import ticks_to_seconds

__all__ = [
    "RenderError",
    "NoAudioError",
    "render",
    "render_audio_bed",
    "RESOLUTION_PRESETS",
    "QUALITY_PRESETS",
    "preset_size",
    "video_encoder_args",
]

ProgressFn = Callable[[float, float], None]

#: Export sizes offered in the dialog, by the height they name. "Source" is
#: the project's own size and is not in here: it is the absence of a choice.
RESOLUTION_PRESETS: dict[str, int] = {
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
}

#: Quality names and the CRF they mean. Lower is better and bigger.
QUALITY_PRESETS: dict[str, int] = {
    "High": 18,
    "Medium": 20,
    "Small": 24,
}


def preset_size(project: Project, preset: str) -> tuple[int, int] | None:
    """The output size for a resolution preset, or None to leave it alone.

    Width follows from the project's aspect ratio rather than from a table of
    standard widths, so a 4:3 or vertical project exports at its own shape. It
    is rounded to an even number because H.264 chroma subsampling requires it.

    None comes back for "Source", and also for a preset that works out to the
    size the project already is: inserting a scaler to scale by one is a waste
    of a filter and a rounding opportunity.
    """
    height = RESOLUTION_PRESETS.get(preset)
    if height is None or project.height <= 0:
        return None
    # To the NEAREST even number, not down to it. 16:9 at 480 high is 853.33
    # wide, and the standard answer is 854: flooring to even would give 852,
    # which is a slightly wrong aspect ratio for no reason.
    exact = project.width * height / project.height
    width = max(2, 2 * round(exact / 2))
    height -= height % 2
    if (width, height) == (project.width, project.height):
        return None
    return width, height


def video_encoder_args(encoder: str, crf: int) -> list[str]:
    """Codec and rate-control arguments for one encoder at one quality.

    The two encoders do not share a quality scale. libx264's CRF and NVENC's
    CQ are both 0..51 and both mean "constant quality", but the same number
    does not produce the same picture: NVENC at a given number is bigger, or
    softer, or both. The number is passed through unchanged anyway, because
    inventing a translation table would be pretending to a precision that
    does not exist. The dialog says the quality per bitrate is lower.
    """
    if encoder == NVENC_H264:
        return [
            "-c:v", NVENC_H264,
            # p5 is NVENC's "medium": the same place on its speed/quality
            # curve that -preset medium is on x264's.
            "-preset", "p5",
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(crf),
            # 0 means "no target bitrate, obey -cq". Without it NVENC applies
            # a default bitrate cap and the quality setting does nothing.
            "-b:v", "0",
        ]
    return ["-c:v", SOFTWARE_H264, "-preset", "medium", "-crf", str(crf)]


# Keep the console window from flashing on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_STDERR_TAIL = 40
_TIME_RE = re.compile(r"time=\s*(-?)(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def _iter_lines(stream) -> Iterator[str]:
    """Yield FFmpeg's stderr line by line.

    Progress lines are terminated with a carriage return rather than a
    newline, so a plain iteration over the stream would buffer the entire
    render into one line and progress would never arrive. Read whatever is
    available and split on either terminator.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            break
        pending += decoder.decode(chunk)
        pending = pending.replace("\r\n", "\n").replace("\r", "\n")
        *lines, pending = pending.split("\n")
        yield from lines
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


def _parse_time(line: str) -> float | None:
    """Seconds encoded by FFmpeg's ``time=HH:MM:SS.ss`` field, if present."""
    match = _TIME_RE.search(line)
    if match is None or match.group(1) == "-":
        return None
    hours, minutes, seconds = match.group(2), match.group(3), match.group(4)
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # The file is still held open somewhere. A stale partial is not worth
        # failing an otherwise complete cancellation over.
        pass


def _run(
    cmd: list[str],
    out_path: Path,
    cancel: threading.Event,
    total_sec: float,
    on_progress: ProgressFn | None,
) -> None:
    """Run one FFmpeg command to completion, cancellation or failure."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    tail: deque[str] = deque(maxlen=_STDERR_TAIL)
    cancelled = False

    try:
        for line in _iter_lines(proc.stderr):
            tail.append(line)
            if on_progress is not None:
                done = _parse_time(line)
                if done is not None:
                    on_progress(min(max(done, 0.0), total_sec), total_sec)
            if cancel.is_set():
                cancelled = True
                break
    except BaseException:
        _terminate(proc)
        _discard(out_path)
        raise
    finally:
        if proc.stderr is not None:
            proc.stderr.close()

    if cancelled:
        _terminate(proc)
        _discard(out_path)
        return

    if proc.wait() != 0:
        _discard(out_path)
        raise RenderError(
            "ffmpeg exited with code "
            + str(proc.returncode)
            + ":\n"
            + "\n".join(tail)
        )

    if on_progress is not None:
        on_progress(total_sec, total_sec)


def render(
    project: Project,
    out_path: Path,
    cancel: threading.Event,
    crf: int = 20,
    on_progress: ProgressFn | None = None,
    encoder: str = SOFTWARE_H264,
    out_size: tuple[int, int] | None = None,
) -> None:
    """Export the project to an H.264 / AAC MP4 at ``out_path``.

    Always re-encodes. Returns normally and deletes the partial file if
    ``cancel`` is set while running; raises :class:`RenderError` if FFmpeg
    fails.

    ``encoder`` is ``libx264`` or ``h264_nvenc``; see
    :func:`video_encoder_args`. ``out_size`` scales the finished video on the
    way out, which is a resolution preset; the filter graph always composes at
    the project's own size, so this is one scaler at the end rather than a
    different graph.
    """
    if not 0 <= crf <= 51:
        raise RenderError(f"crf must be between 0 and 51, got {crf}")

    inputs, filter_complex, maps = build_full(project)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(resolve_binary("ffmpeg")),
        "-hide_banner",
        "-nostdin",
        "-y",
        *inputs,
        "-filter_complex",
        filter_complex,
        *maps,
        *video_encoder_args(encoder, crf),
        "-pix_fmt",
        "yuv420p",
    ]
    if out_size is not None:
        cmd += ["-s", f"{out_size[0]}x{out_size[1]}"]
    if "[aout]" in maps:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd.append(str(out_path))

    _run(cmd, out_path, cancel, ticks_to_seconds(project.duration), on_progress)


def render_audio_bed(
    project: Project,
    out_wav: Path,
    cancel: threading.Event,
    on_progress: ProgressFn | None = None,
) -> None:
    """Render the whole audio timeline to one uncompressed WAV.

    Phase 5 plays this file as the master clock, so it spans the entire project
    duration including trailing silence. No video stream is decoded or
    referenced, which is what keeps it fast. Raises :class:`NoAudioError` when
    the project has nothing to mix.
    """
    inputs, filter_complex, maps = build_audio_only(project)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(resolve_binary("ffmpeg")),
        "-hide_banner",
        "-nostdin",
        "-y",
        *inputs,
        "-filter_complex",
        filter_complex,
        *maps,
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(project.sample_rate),
        "-ac",
        "2",
        str(out_wav),
    ]

    _run(cmd, out_wav, cancel, ticks_to_seconds(project.duration), on_progress)
