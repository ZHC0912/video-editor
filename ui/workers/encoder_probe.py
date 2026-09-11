"""Asking, off the GUI thread, whether hardware encoding works here.

The answer takes two FFmpeg invocations, together well under a tenth of a
second on a healthy machine. That is not long, but "not long" is measured on
the machine that answers quickly: the trial encode initialises the GPU, and a
card that is asleep, busy, or attached to a driver that is about to fail takes
as long as it takes. Nothing that spawns a subprocess belongs on the GUI
thread, and the window has nothing to do with the answer until the user opens
the export dialog anyway.

Until it reports, the answer is no, which means an export dialog opened in the
first moments of a session offers software encoding only. That is the safe way
round: the alternative is offering an option that has not been checked.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal

from core.encoders import hardware_encoder_available
from ui.workers import media_pool, report_safely

__all__ = ["EncoderProbe"]


class _ProbeJob(QRunnable):
    def __init__(self, report) -> None:
        super().__init__()
        self._report = report

    def run(self) -> None:
        try:
            available = hardware_encoder_available()
        except Exception:  # noqa: BLE001 - a pool thread must not raise
            available = False
        report_safely(self._report, available)


class EncoderProbe(QObject):
    """One shot. The result is cached in core.encoders, so this runs once."""

    #: Carries True when hardware encoding is offerable.
    finished = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._available = False
        self._started = False

    def available(self) -> bool:
        return self._available

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        media_pool().start(_ProbeJob(self._on_result))

    def _on_result(self, available: bool) -> None:
        self._available = bool(available)
        self.finished.emit(self._available)
