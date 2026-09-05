"""Background jobs for the timeline.

Everything here runs off the GUI thread. Two kinds of work live in this
package and they have different shapes:

Thumbnails and waveforms are many short, independent, fire-and-forget ffmpeg
calls, one per thumbnail and one per source file. That is exactly what a thread
pool is for. A QThread per job would spend more time creating and destroying
threads than decoding, and scrolling across a long timeline would try to spawn
hundreds of OS threads at once; the pool bounds it. They are QRunnables on a
shared QThreadPool.

The audio bed is the opposite: one long job, at most one at a time, cancellable
and restartable. It gets its own single-slot pool and its own state machine.
See :mod:`ui.workers.audio_bed_worker`.
"""

from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QThreadPool

__all__ = ["media_pool", "run_ffmpeg_capture", "report_safely", "NO_WINDOW"]

# Keep a console window from flashing on Windows for every job.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Enough concurrency to keep the view filling in, few enough that a scroll does
# not put a dozen ffmpeg processes on the CPU at once.
_MAX_MEDIA_THREADS = 4

_pool: QThreadPool | None = None


def media_pool() -> QThreadPool:
    """The shared pool for thumbnail and waveform jobs."""
    global _pool
    if _pool is None:
        _pool = QThreadPool()
        _pool.setMaxThreadCount(min(_MAX_MEDIA_THREADS, QThreadPool.globalInstance().maxThreadCount()))
    return _pool


def report_safely(emit, *args) -> None:
    """Deliver a job's result, tolerating a receiver that has gone away.

    Pool jobs outlive the object that queued them. Closing the window, or
    rebuilding the scene onto a new project, deletes a cache while its jobs are
    still in the queue; emitting a signal from a deleted QObject raises
    RuntimeError on a pool thread, where there is nobody to catch it. The
    result is simply not wanted any more, so dropping it is correct.
    """
    try:
        emit(*args)
    except RuntimeError:
        pass


def run_ffmpeg_capture(args: list[str], timeout: float = 30.0) -> bytes:
    """Run ffmpeg and return raw stdout, or empty bytes on any failure.

    Media jobs are decoration. A source that will not decode should leave the
    clip looking plain, never raise into a paint event or a pool thread.
    """
    from core.binaries import resolve_binary

    cmd = [str(resolve_binary("ffmpeg")), "-nostdin", "-hide_banner", "-loglevel", "error", *args]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    if completed.returncode != 0:
        return b""
    return completed.stdout
