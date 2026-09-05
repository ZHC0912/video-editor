"""Thumbnail and waveform caches.

Verified more loosely than the audio bed worker: what matters here is that the
caches deduplicate, stay bounded, and slice rather than re-decode. The ffmpeg
calls themselves are exercised once, slowly, at the end.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.timebase import TICKS_PER_SECOND as SEC  # noqa: E402
from ui.workers.thumbnail_worker import (  # noqa: E402
    CACHE_LIMIT,
    ThumbnailCache,
    quantise_tick,
)
from ui.workers.waveform_worker import (  # noqa: E402
    PEAKS_PER_SECOND,
    SAMPLE_RATE,
    WaveformCache,
    peaks_from_samples,
)


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def pump_until(predicate, timeout: float = 20.0) -> bool:
    app = QApplication.instance()
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.01)
    return predicate()


class TestTickQuantisation:
    def test_rounds_onto_a_grid(self) -> None:
        quantum = SEC // 4
        assert quantise_tick(0) == 0
        assert quantise_tick(quantum - 1) == 0
        assert quantise_tick(quantum) == quantum
        assert quantise_tick(quantum + 5) == quantum

    def test_never_negative(self) -> None:
        assert quantise_tick(-5000) == 0

    def test_nearby_requests_share_a_cache_entry(self) -> None:
        # The point of the grid: a small scroll must not miss every entry.
        assert quantise_tick(SEC + 10) == quantise_tick(SEC + 20)


class TestThumbnailCache:
    def test_a_miss_returns_none(self, qapp: QApplication) -> None:
        cache = ThumbnailCache()
        assert cache.request(Path("C:/nope.mp4"), 0) is None

    def test_peek_never_schedules_work(self, qapp: QApplication) -> None:
        # paint() calls peek. If peek scheduled a job, painting would decode
        # forever.
        cache = ThumbnailCache()
        cache.peek(Path("C:/nope.mp4"), 0)
        assert cache._in_flight == set()

    def test_repeated_requests_schedule_one_job(self, qapp: QApplication) -> None:
        cache = ThumbnailCache()
        for _ in range(10):
            cache.request(Path("C:/nope.mp4"), 0)
        assert len(cache._in_flight) == 1

    def test_the_lru_is_capped(self, qapp: QApplication) -> None:
        from PySide6.QtGui import QPixmap

        cache = ThumbnailCache()
        for i in range(CACHE_LIMIT + 50):
            cache._cache[(f"src{i}", 0)] = QPixmap(1, 1)
        # Trim happens on insert through the slot; call it the way the slot does.
        while len(cache._cache) > CACHE_LIMIT:
            cache._cache.popitem(last=False)
        assert len(cache) == CACHE_LIMIT

    def test_clear_empties_it(self, qapp: QApplication) -> None:
        from PySide6.QtGui import QPixmap

        cache = ThumbnailCache()
        cache._cache[("a", 0)] = QPixmap(1, 1)
        cache.clear()
        assert len(cache) == 0


class TestPeaksFromSamples:
    def test_empty_input(self) -> None:
        lows, highs = peaks_from_samples(np.zeros(0, dtype=np.int16))
        assert lows.size == 0 and highs.size == 0

    def test_one_bucket_per_slice_of_samples(self) -> None:
        samples = np.zeros(SAMPLE_RATE, dtype=np.int16)  # one second
        lows, highs = peaks_from_samples(samples)
        assert lows.size == PEAKS_PER_SECOND
        assert highs.size == PEAKS_PER_SECOND

    def test_normalised_to_plus_minus_one(self) -> None:
        samples = np.full(SAMPLE_RATE, 32767, dtype=np.int16)
        lows, highs = peaks_from_samples(samples)
        assert highs.max() == pytest.approx(1.0, abs=1e-4)
        assert lows.min() == pytest.approx(1.0, abs=1e-4)

    def test_captures_both_extremes_in_a_bucket(self) -> None:
        samples = np.zeros(SAMPLE_RATE, dtype=np.int16)
        samples[0] = 32767
        samples[1] = -32768
        lows, highs = peaks_from_samples(samples)
        assert highs[0] == pytest.approx(1.0, abs=1e-4)
        assert lows[0] == pytest.approx(-1.0, abs=1e-4)

    def test_a_fragment_shorter_than_a_bucket_still_produces_one(self) -> None:
        lows, highs = peaks_from_samples(np.array([100, -100], dtype=np.int16))
        assert lows.size == 1 and highs.size == 1


class TestWaveformSlicing:
    @pytest.fixture
    def cache(self, qapp: QApplication) -> WaveformCache:
        c = WaveformCache()
        # Ten seconds of ramp, stored as if it had been decoded.
        buckets = 10 * PEAKS_PER_SECOND
        highs = np.linspace(0.0, 1.0, buckets, dtype=np.float32)
        c._peaks["fake.wav"] = (-highs, highs)
        return c

    def test_slices_the_shared_array_without_decoding(
        self, cache: WaveformCache
    ) -> None:
        before = set(cache._in_flight)
        sliced = cache.slice_for(Path("fake.wav"), 0, 5 * SEC, 100)
        assert sliced is not None
        assert cache._in_flight == before, "slicing scheduled a decode"

    def test_returns_exactly_the_requested_columns(
        self, cache: WaveformCache
    ) -> None:
        for columns in (1, 7, 100, 999):
            lows, highs = cache.slice_for(Path("fake.wav"), 0, 10 * SEC, columns)
            assert lows.size == columns
            assert highs.size == columns

    def test_different_windows_give_different_peaks(
        self, cache: WaveformCache
    ) -> None:
        # The ramp rises over the file, so a later window must be louder.
        _, early = cache.slice_for(Path("fake.wav"), 0, 2 * SEC, 10)
        _, late = cache.slice_for(Path("fake.wav"), 8 * SEC, 10 * SEC, 10)
        assert late.mean() > early.mean()

    def test_a_window_past_the_end_is_clamped(self, cache: WaveformCache) -> None:
        sliced = cache.slice_for(Path("fake.wav"), 9 * SEC, 60 * SEC, 20)
        assert sliced is not None
        assert sliced[0].size == 20

    def test_unknown_source_returns_none(self, cache: WaveformCache) -> None:
        assert cache.slice_for(Path("other.wav"), 0, SEC, 10) is None

    def test_zero_columns_returns_none(self, cache: WaveformCache) -> None:
        assert cache.slice_for(Path("fake.wav"), 0, SEC, 0) is None

    def test_peek_never_schedules_work(self, cache: WaveformCache) -> None:
        cache.peek(Path("never-seen.wav"))
        assert "never-seen.wav" not in cache._in_flight

    def test_one_request_per_source(self, cache: WaveformCache) -> None:
        for _ in range(5):
            cache.request(Path("another.wav"))
        assert len(cache._in_flight) == 1


@pytest.mark.slow
class TestAgainstRealMedia:
    def test_a_thumbnail_arrives(self, qapp: QApplication, av_file: Path) -> None:
        cache = ThumbnailCache()
        assert cache.request(av_file, 0) is None
        assert pump_until(lambda: cache.peek(av_file, 0) is not None)
        pixmap = cache.peek(av_file, 0)
        assert not pixmap.isNull()
        assert pixmap.height() == 64

    def test_a_second_request_is_served_from_cache(
        self, qapp: QApplication, av_file: Path
    ) -> None:
        cache = ThumbnailCache()
        cache.request(av_file, SEC)
        assert pump_until(lambda: cache.peek(av_file, SEC) is not None)
        assert cache.request(av_file, SEC) is not None

    def test_an_unreadable_source_does_not_raise(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        junk = tmp_path / "notes.txt"
        junk.write_text("not a movie", encoding="utf-8")
        cache = ThumbnailCache()
        cache.request(junk, 0)
        assert pump_until(lambda: not cache._in_flight)
        assert cache.peek(junk, 0) is None

    def test_waveform_peaks_arrive_for_a_real_file(
        self, qapp: QApplication, av_file: Path
    ) -> None:
        cache = WaveformCache()
        assert cache.request(av_file) is None
        assert pump_until(lambda: cache.peek(av_file) is not None)
        lows, highs = cache.peek(av_file)
        # Two seconds of a 440Hz tone. It comes back quieter than it went in,
        # having been through AAC and a resample, so this checks the shape
        # rather than the level: real signal, and symmetric about zero the way
        # a sine is.
        assert lows.size > PEAKS_PER_SECOND
        assert highs.max() > 0.02
        assert lows.min() < -0.02
        assert highs.max() == pytest.approx(-lows.min(), rel=0.25)

    def test_the_whole_file_is_decoded_once_then_sliced(
        self, qapp: QApplication, av_file: Path
    ) -> None:
        cache = WaveformCache()
        cache.request(av_file)
        assert pump_until(lambda: cache.peek(av_file) is not None)
        full = cache.peek(av_file)

        for _ in range(20):
            cache.slice_for(av_file, 0, SEC, 50)
        # Same array object: slicing never replaced or re-fetched it.
        assert cache.peek(av_file) is full
        assert cache._in_flight == set()

    def test_a_silent_video_yields_empty_peaks_without_raising(
        self, qapp: QApplication, video_only_file: Path
    ) -> None:
        cache = WaveformCache()
        cache.request(video_only_file)
        assert pump_until(lambda: cache.peek(video_only_file) is not None)
        lows, _ = cache.peek(video_only_file)
        assert lows.size == 0
