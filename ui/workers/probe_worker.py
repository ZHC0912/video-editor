"""Probing dropped files off the GUI thread.

File > Import Media probes synchronously, which is fine for a handful of files
chosen one dialog at a time. Dropping twenty files is different: each probe
spawns ffprobe, and doing that in a row on the GUI thread freezes the window
for as long as it takes.

So a drop creates every bin row immediately, with just the filename, and this
queue fills them in as the probes come back. Failures are collected per batch
so the window can show one summary rather than a dialog per bad file.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from core.probe import ProbeError, probe
from ui.workers import media_pool, report_safely

__all__ = ["ProbeQueue"]


class _ProbeJob(QRunnable):
    def __init__(self, batch: int, path: Path, report) -> None:
        super().__init__()
        self._batch = batch
        self._path = path
        self._report = report

    def run(self) -> None:
        try:
            info = probe(self._path)
        except ProbeError as exc:
            report_safely(
                self._report, self._batch, str(self._path), None, str(exc).splitlines()[0]
            )
            return
        except Exception as exc:  # noqa: BLE001 - a pool thread must not raise
            report_safely(self._report, self._batch, str(self._path), None, f"{exc}")
            return
        report_safely(self._report, self._batch, str(self._path), info, "")


class ProbeQueue(QObject):
    """Probe files in the background, in batches."""

    #: A file was read. Carries the path and its MediaInfo.
    probed = Signal(str, object)
    #: A file could not be read. Carries the path and a one line reason.
    failed = Signal(str, str)
    #: Every file in a batch has reported. Carries the batch id and the list
    #: of (path, reason) pairs that failed, so one dialog can cover them all.
    batch_finished = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._next_batch = 0
        self._outstanding: dict[int, int] = {}
        self._failures: dict[int, list[tuple[str, str]]] = {}
        self._job_done = _JobDone()
        self._job_done.done.connect(self._on_job_done)

    def submit(self, paths: Iterable[Path]) -> int:
        """Queue a batch of files. Returns the batch id."""
        paths = [Path(p) for p in paths]
        batch = self._next_batch
        self._next_batch += 1
        if not paths:
            self.batch_finished.emit(batch, [])
            return batch

        self._outstanding[batch] = len(paths)
        self._failures[batch] = []
        pool = media_pool()
        for path in paths:
            pool.start(_ProbeJob(batch, path, self._job_done.done.emit))
        return batch

    def is_busy(self) -> bool:
        return bool(self._outstanding)

    @Slot(int, str, object, str)
    def _on_job_done(self, batch: int, path: str, info, error: str) -> None:
        if info is not None:
            self.probed.emit(path, info)
        else:
            self._failures.setdefault(batch, []).append((path, error))
            self.failed.emit(path, error)

        remaining = self._outstanding.get(batch, 0) - 1
        if remaining > 0:
            self._outstanding[batch] = remaining
            return
        self._outstanding.pop(batch, None)
        self.batch_finished.emit(batch, self._failures.pop(batch, []))


class _JobDone(QObject):
    """Carries a finished job from a pool thread to the GUI thread.

    A separate QObject so the signal's owner is never the queue itself, which
    keeps the queued delivery straightforward.
    """

    done = Signal(int, str, object, str)
