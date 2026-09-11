"""The preview audio bed.

Playback plays one pre-rendered WAV of the whole audio timeline and uses its
position as the master clock. This worker is what keeps that file current: any
edit touching an audio track invalidates it, and 500ms after the edits stop a
new one is rendered.

The rendering itself is :func:`core.render.render_audio_bed`. This module does
not know how to run ffmpeg and must not learn: subprocess spawning, stderr
reading, cancellation and partial file cleanup are all already handled there,
correctly and under test. What lives here is the part core cannot do, because
core knows nothing about Qt: debouncing, cancelling, and guaranteeing that two
renders never overlap.

The state machine, which is the whole point of the file:

    invalidate()  ->  emit bed_invalidated
                      cancel whatever is running
                      (re)start the debounce timer

    timer fires   ->  if a render is still winding down, remember to start
                      again when it finishes; otherwise start one now

    render ends   ->  if it was cancelled or superseded, drop the result
                      silently; if a restart is pending, start it now
"""

from __future__ import annotations

import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from core.filtergraph import NoAudioError, RenderError
from core.model import Project
from core.render import render_audio_bed
from ui.workers import report_safely

__all__ = [
    "AudioBedWorker",
    "DEFAULT_DEBOUNCE_MS",
    "BED_PREFIX",
    "BED_SUFFIX",
    "bed_directory",
    "purge_stale_beds",
]

DEFAULT_DEBOUNCE_MS = 500

#: Every bed this application writes is named to this pattern, so a later run
#: can recognise its own leftovers and nothing else's.
BED_PREFIX = "videditor_bed_"
BED_SUFFIX = ".wav"

#: How old a stray bed has to be before a startup sweep deletes it. A day,
#: which is comfortably longer than any session and comfortably shorter than
#: leaving gigabytes of WAV in temp forever.
STALE_BED_AGE_SECONDS = 24 * 3600


def bed_directory() -> Path:
    return Path(tempfile.gettempdir())


def purge_stale_beds(
    older_than_seconds: float = STALE_BED_AGE_SECONDS,
    directory: Path | None = None,
    now: float | None = None,
) -> int:
    """Delete beds left behind by runs that did not exit cleanly.

    A bed is a decompressed WAV of a whole project's audio, which is tens of
    megabytes a minute, and a session killed in Task Manager leaves its one
    behind. Called at startup, where the current session's bed does not exist
    yet, so the age test is belt and braces rather than the thing keeping it
    from deleting a bed that is in use.

    Returns how many were removed. Never raises: a temp directory that cannot
    be read is not a reason to fail to start.
    """
    directory = bed_directory() if directory is None else Path(directory)
    cutoff = (time.time() if now is None else now) - older_than_seconds
    removed = 0
    try:
        candidates = list(directory.glob(f"{BED_PREFIX}*{BED_SUFFIX}"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:
            # Held open by another running copy of the application, or gone
            # already. Either way, not ours to worry about.
            continue
        removed += 1
    return removed

RenderFn = Callable[[Project, Path, threading.Event], None]


class _BedJob(QRunnable):
    """One render attempt. Reports back through a plain callback."""

    def __init__(
        self,
        generation: int,
        project: Project,
        out_path: Path,
        cancel: threading.Event,
        render_fn: RenderFn,
        report: Callable[[int, str, str], None],
    ) -> None:
        super().__init__()
        self._generation = generation
        self._project = project
        self._out_path = out_path
        self._cancel = cancel
        self._render_fn = render_fn
        self._report = report

    def run(self) -> None:
        try:
            self._render_fn(self._project, self._out_path, self._cancel)
        except NoAudioError:
            report_safely(self._report, self._generation, "unavailable", "")
            return
        except RenderError as exc:
            report_safely(self._report, self._generation, "failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a pool thread must not raise
            report_safely(self._report, self._generation, "failed", f"{exc}")
            return

        if self._cancel.is_set():
            report_safely(self._report, self._generation, "cancelled", "")
            return
        report_safely(self._report, self._generation, "ready", str(self._out_path))


class AudioBedWorker(QObject):
    """Keeps one audio bed WAV in step with the project.

    ``render_fn`` exists so tests can drive the state machine without spending
    a second on ffmpeg for every case. It defaults to the real renderer.
    """

    #: A bed finished rendering. Carries the path.
    bed_ready = Signal(str)
    #: A bed could not be rendered. Carries a message for the user.
    bed_failed = Signal(str)
    #: The current bed is stale. Emitted immediately on every invalidate, so
    #: playback can stop trusting the file it is holding.
    bed_invalidated = Signal()
    #: The project has no unmuted audio track with clips, so there is nothing
    #: to render. Playback falls back to a wall clock.
    bed_unavailable = Signal()

    #: Internal, carries a finished job back to the GUI thread.
    _job_finished = Signal(int, str, str)

    def __init__(
        self,
        parent: QObject | None = None,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        render_fn: RenderFn | None = None,
    ) -> None:
        super().__init__(parent)
        self._render_fn: RenderFn = render_fn or render_audio_bed
        self._project: Project | None = None

        # One slot, so the pool itself cannot run two renders at once even if
        # the state machine below were ever wrong.
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._on_debounce_elapsed)

        self._generation = 0
        self._running = False
        self._restart_pending = False
        self._cancel: threading.Event | None = None

        # One bed per session, deleted on clean exit; strays from runs that
        # were not clean are swept at startup by purge_stale_beds. The Project
        # model has no id of its own, so the worker supplies one rather than
        # growing a field on a core model.
        self._bed_path = (
            bed_directory() / f"{BED_PREFIX}{uuid.uuid4().hex}{BED_SUFFIX}"
        )

        self._job_finished.connect(self._on_job_finished)

    # -- public API -------------------------------------------------------

    @property
    def bed_path(self) -> Path:
        """Where this session's bed is written. Stable for the session."""
        return self._bed_path

    def is_rendering(self) -> bool:
        return self._running

    def is_pending(self) -> bool:
        """A render is debouncing or queued behind one that is winding down."""
        return self._timer.isActive() or self._restart_pending

    def set_project(self, project: Project | None) -> None:
        """Point the worker at a project and schedule a bed for it."""
        self._project = project
        self.invalidate()

    def invalidate(self) -> None:
        """The bed is stale. Cancel anything running and re-arm the timer.

        Called for every edit that touches an audio track, and on project load.
        Rapid calls coalesce: the timer restarts each time, so a drag that
        fires fifty invalidations still renders once.
        """
        self.bed_invalidated.emit()
        self._generation += 1
        self._cancel_running()
        if self._project is None:
            self._timer.stop()
            return
        self._timer.start()

    def shutdown(self) -> None:
        """Stop everything. Safe to call more than once."""
        self._timer.stop()
        self._restart_pending = False
        self._generation += 1
        self._cancel_running()
        self._pool.waitForDone(5000)

    def discard_bed(self) -> None:
        """Delete this session's bed file if it exists."""
        try:
            self._bed_path.unlink(missing_ok=True)
        except OSError:
            pass

    # -- internals --------------------------------------------------------

    def _cancel_running(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    @Slot()
    def _on_debounce_elapsed(self) -> None:
        if self._project is None:
            return
        if not self._project.has_audio:
            # Nothing to mix. Project.has_audio is the model's own answer to
            # that question, and it is cheaper and clearer to ask it here than
            # to spawn ffmpeg so it can refuse. An empty project reaches this
            # too, and "there is no bed" is the truth about it, not a failure.
            self.bed_unavailable.emit()
            return
        if self._running:
            # The previous render has been told to stop but has not yet
            # noticed. Start the new one when it reports back rather than
            # alongside it.
            self._restart_pending = True
            return
        self._start()

    def _start(self) -> None:
        if self._project is None or self._running:
            return
        self._running = True
        self._restart_pending = False
        self._cancel = threading.Event()
        job = _BedJob(
            generation=self._generation,
            project=self._project,
            out_path=self._bed_path,
            cancel=self._cancel,
            render_fn=self._render_fn,
            report=self._job_finished.emit,
        )
        self._pool.start(job)

    @Slot(int, str, str)
    def _on_job_finished(self, generation: int, outcome: str, payload: str) -> None:
        self._running = False
        self._cancel = None

        stale = generation != self._generation
        if self._restart_pending:
            self._restart_pending = False
            self._start()

        if stale or outcome == "cancelled":
            # Superseded. bed_invalidated already went out when it happened;
            # saying anything more here would be reporting on work nobody is
            # waiting for.
            return

        if outcome == "ready":
            self.bed_ready.emit(payload)
        elif outcome == "unavailable":
            self.bed_unavailable.emit()
        else:
            self.bed_failed.emit(payload)
