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
from core.filtergraph import (
    NoAudioError,
    RenderError,
    build_audio_only,
    build_full,
)
from core.model import Project
from core.timebase import ticks_to_seconds

__all__ = ["RenderError", "NoAudioError", "render", "render_audio_bed"]

ProgressFn = Callable[[float, float], None]

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
) -> None:
    """Export the project to an H.264 / AAC MP4 at ``out_path``.

    Always re-encodes. Returns normally and deletes the partial file if
    ``cancel`` is set while running; raises :class:`RenderError` if FFmpeg
    fails.
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
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
    ]
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
