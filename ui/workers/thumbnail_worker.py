"""Filmstrip thumbnails for video clips.

One ffmpeg call per thumbnail, on the shared pool. Results are cached by
(source, rounded tick) so that scrolling back and forth, or two clips cut from
the same source at the same point, decode once.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot
from PySide6.QtGui import QImage, QPixmap

from core.timebase import TICKS_PER_SECOND, ticks_to_seconds
from ui.workers import media_pool, report_safely, run_ffmpeg_capture

__all__ = [
    "ThumbnailCache",
    "shared_thumbnail_cache",
    "THUMBNAIL_HEIGHT",
    "CACHE_LIMIT",
    "quantise_tick",
]

#: Matches the -vf scale=-1:64 in the ffmpeg call below.
THUMBNAIL_HEIGHT = 64

#: LRU cap, in thumbnails.
CACHE_LIMIT = 500

#: Requests are snapped to this grid before hitting the cache, so a small
#: scroll does not miss every entry by a few ticks.
_QUANTUM = TICKS_PER_SECOND // 4


def quantise_tick(tick: int) -> int:
    """Round a source position onto the cache grid."""
    return max(0, int(tick) // _QUANTUM * _QUANTUM)


class _ThumbnailJob(QRunnable):
    def __init__(self, src: Path, tick: int, report) -> None:
        super().__init__()
        self._src = src
        self._tick = tick
        self._report = report

    def run(self) -> None:
        # -ss before -i is the fast seek: ffmpeg jumps to the nearest keyframe
        # rather than decoding from the start of the file.
        data = run_ffmpeg_capture(
            [
                "-ss", f"{ticks_to_seconds(self._tick):.6f}",
                "-i", str(self._src),
                "-frames:v", "1",
                "-vf", f"scale=-1:{THUMBNAIL_HEIGHT}",
                "-f", "image2pipe",
                "-vcodec", "png",
                "-",
            ]
        )
        image = QImage()
        if data:
            # Decoding to QImage is allowed off the GUI thread. QPixmap is not,
            # so the conversion happens in the slot on the other side.
            image.loadFromData(data, "PNG")
        report_safely(self._report, str(self._src), self._tick, image)


class ThumbnailCache(QObject):
    """Ask for a thumbnail; get one now if it is known, or a signal later."""

    #: A thumbnail arrived for (source path, source tick).
    thumbnail_ready = Signal(str, "qint64")

    _job_done = Signal(str, "qint64", QImage)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: OrderedDict[tuple[str, int], QPixmap] = OrderedDict()
        self._in_flight: set[tuple[str, int]] = set()
        self._job_done.connect(self._on_job_done)

    def request(self, src: Path, tick: int) -> QPixmap | None:
        """The thumbnail nearest ``tick``, or None while it is being made."""
        key = (str(src), quantise_tick(tick))
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if key not in self._in_flight:
            self._in_flight.add(key)
            media_pool().start(_ThumbnailJob(Path(key[0]), key[1], self._job_done.emit))
        return None

    def peek(self, src: Path, tick: int) -> QPixmap | None:
        """Cache lookup with no side effect. Safe to call from paint()."""
        key = (str(src), quantise_tick(tick))
        return self._cache.get(key)

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)

    @Slot(str, "qint64", QImage)
    def _on_job_done(self, src: str, tick: int, image: QImage) -> None:
        key = (src, tick)
        self._in_flight.discard(key)
        if image.isNull():
            # Leave it uncached but do not retry forever: the next request
            # will try again, which is the right behaviour for a file that
            # was briefly unreadable.
            return
        self._cache[key] = QPixmap.fromImage(image)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_LIMIT:
            self._cache.popitem(last=False)
        self.thumbnail_ready.emit(src, tick)


_shared: ThumbnailCache | None = None


def shared_thumbnail_cache() -> ThumbnailCache:
    """The one thumbnail cache in the application.

    The timeline's filmstrips and the media bin's poster frames pull from the
    same LRU. A clip cut from a file already showing in the bin costs no extra
    decode, and there is only one memory budget to reason about.

    Deliberately unparented: it outlives any one scene or window.
    """
    global _shared
    if _shared is None:
        _shared = ThumbnailCache()
    return _shared
