"""Waveform peaks for audio clips.

Decoded once per SOURCE FILE, not once per clip. Three clips cut from the same
recording share one decode; each slices the part it needs out of the full file
peak array. Re-decoding per clip would make a timeline of many small cuts from
one source pathologically slow.

The peak array is min/max pairs at a fixed resolution, which is all a waveform
drawing needs and is two orders of magnitude smaller than the samples.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from core.timebase import TICKS_PER_SECOND
from ui.workers import media_pool, report_safely, run_ffmpeg_capture

__all__ = ["WaveformCache", "PEAKS_PER_SECOND", "SAMPLE_RATE", "peaks_from_samples"]

#: What the ffmpeg call below resamples to.
SAMPLE_RATE = 8000

#: Buckets per second in the stored peak array. 5ms per bucket is finer than
#: any zoom level can show and keeps a ten minute file around a megabyte.
PEAKS_PER_SECOND = 200

_SAMPLES_PER_BUCKET = SAMPLE_RATE // PEAKS_PER_SECOND


def peaks_from_samples(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Reduce mono int16 samples to (minimum, maximum) per bucket, as float.

    Values come back in -1.0 .. 1.0.
    """
    if samples.size == 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)

    usable = samples.size - (samples.size % _SAMPLES_PER_BUCKET)
    if usable == 0:
        # Shorter than one bucket. Still show something rather than nothing.
        block = samples.astype(np.float32) / 32768.0
        return (
            np.array([block.min()], dtype=np.float32),
            np.array([block.max()], dtype=np.float32),
        )

    blocks = samples[:usable].reshape(-1, _SAMPLES_PER_BUCKET).astype(np.float32)
    blocks /= 32768.0
    return blocks.min(axis=1), blocks.max(axis=1)


class _WaveformJob(QRunnable):
    def __init__(self, src: Path, report) -> None:
        super().__init__()
        self._src = src
        self._report = report

    def run(self) -> None:
        raw = run_ffmpeg_capture(
            [
                "-i", str(self._src),
                "-ac", "1",
                "-filter:a", f"aresample={SAMPLE_RATE}",
                "-map", "0:a",
                "-c:a", "pcm_s16le",
                "-f", "data",
                "-",
            ],
            timeout=120.0,
        )
        if not raw:
            report_safely(
                self._report, str(self._src), np.zeros(0, np.float32), np.zeros(0, np.float32)
            )
            return
        # Trim a trailing odd byte rather than letting frombuffer raise.
        samples = np.frombuffer(raw[: len(raw) - (len(raw) % 2)], dtype="<i2")
        lows, highs = peaks_from_samples(samples)
        report_safely(self._report, str(self._src), lows, highs)


class WaveformCache(QObject):
    """Full file peak arrays, keyed by source path."""

    #: Peaks arrived for this source path.
    waveform_ready = Signal(str)

    _job_done = Signal(str, object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._peaks: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._in_flight: set[str] = set()
        self._job_done.connect(self._on_job_done)

    def request(self, src: Path) -> tuple[np.ndarray, np.ndarray] | None:
        """Peaks for a whole source file, or None while they are being made."""
        key = str(src)
        peaks = self._peaks.get(key)
        if peaks is not None:
            return peaks
        if key not in self._in_flight:
            self._in_flight.add(key)
            media_pool().start(_WaveformJob(Path(key), self._job_done.emit))
        return None

    def peek(self, src: Path) -> tuple[np.ndarray, np.ndarray] | None:
        """Cache lookup with no side effect. Safe to call from paint()."""
        return self._peaks.get(str(src))

    def slice_for(
        self, src: Path, src_in: int, src_out: int, columns: int
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """The peaks covering a clip, reduced to ``columns`` drawing columns.

        This is the per-clip step: it slices the shared full file array rather
        than decoding anything.
        """
        peaks = self.peek(src)
        if peaks is None or columns <= 0:
            return None
        lows, highs = peaks
        if lows.size == 0:
            return None

        start = int(src_in / TICKS_PER_SECOND * PEAKS_PER_SECOND)
        stop = int(src_out / TICKS_PER_SECOND * PEAKS_PER_SECOND)
        start = max(0, min(start, lows.size))
        stop = max(start + 1, min(stop, lows.size))

        window_low = lows[start:stop]
        window_high = highs[start:stop]
        if window_low.size == 0:
            return None

        # Bucket the window down to one column per pixel.
        edges = np.linspace(0, window_low.size, columns + 1).astype(int)
        out_low = np.empty(columns, dtype=np.float32)
        out_high = np.empty(columns, dtype=np.float32)
        for i in range(columns):
            a, b = edges[i], max(edges[i] + 1, edges[i + 1])
            b = min(b, window_low.size)
            out_low[i] = window_low[a:b].min()
            out_high[i] = window_high[a:b].max()
        return out_low, out_high

    def clear(self) -> None:
        self._peaks.clear()

    @Slot(str, object, object)
    def _on_job_done(self, src: str, lows: np.ndarray, highs: np.ndarray) -> None:
        self._in_flight.discard(src)
        self._peaks[src] = (lows, highs)
        self.waveform_ready.emit(src)
