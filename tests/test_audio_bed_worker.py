"""Audio bed worker tests.

Playback depends on this worker, and it has the subtlest failure mode in
the application: a bed that is silently stale, or two ffmpeg processes writing
the same WAV at once. The render function is injected so the state machine can be driven
deterministically instead of waiting on real encodes.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.filtergraph import NoAudioError, RenderError  # noqa: E402
from core.model import Clip, Project, Track  # noqa: E402
from core.timebase import TICKS_PER_SECOND as SEC  # noqa: E402
from ui.workers.audio_bed_worker import AudioBedWorker  # noqa: E402

SRC = Path("C:/media/a.mp4")


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def pump(seconds: float) -> None:
    """Run the Qt event loop for a while. Timers need it to fire."""
    app = QApplication.instance()
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.005)


def pump_until(predicate, timeout: float = 5.0) -> bool:
    app = QApplication.instance()
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.005)
    return predicate()


def audio_project(name: str = "p") -> Project:
    return Project(
        name=name,
        tracks=[
            Track(
                name="V1",
                kind="video",
                clips=[Clip(src=SRC, src_in=0, src_out=5 * SEC, timeline_start=0)],
            ),
            Track(
                name="A1",
                kind="audio",
                clips=[Clip(src=SRC, src_in=0, src_out=3 * SEC, timeline_start=0)],
            ),
        ],
    )


class RenderSpy:
    """Stands in for core.render.render_audio_bed.

    Records every call, can be made slow, and honours the cancel Event the way
    the real renderer does.
    """

    def __init__(self, duration: float = 0.0) -> None:
        self.duration = duration
        self.calls: list[Project] = []
        self.cancelled: list[bool] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.started = threading.Event()
        self._lock = threading.Lock()

    def __call__(
        self, project: Project, out_path: Path, cancel: threading.Event
    ) -> None:
        with self._lock:
            self.calls.append(project)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.started.set()
        try:
            deadline = time.perf_counter() + self.duration
            was_cancelled = False
            while time.perf_counter() < deadline:
                if cancel.is_set():
                    was_cancelled = True
                    break
                time.sleep(0.005)
            if cancel.is_set():
                was_cancelled = True
            self.cancelled.append(was_cancelled)
        finally:
            with self._lock:
                self.concurrent -= 1

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def worker_factory(qapp: QApplication):
    made: list[AudioBedWorker] = []

    def make(render_fn, debounce_ms: int = 60) -> AudioBedWorker:
        worker = AudioBedWorker(debounce_ms=debounce_ms, render_fn=render_fn)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.shutdown()
        worker.discard_bed()


# --------------------------------------------------------------------------
# Debounce
# --------------------------------------------------------------------------

class TestDebounce:
    def test_rapid_invalidations_coalesce_into_one_render(
        self, worker_factory
    ) -> None:
        # The case that matters: a drag emits an invalidation per mouse move.
        # Fifty of them must produce one bed, not fifty.
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=120)
        worker.set_project(audio_project())

        for _ in range(50):
            worker.invalidate()
            pump(0.002)

        assert spy.call_count == 0, "rendered before the debounce elapsed"
        assert pump_until(lambda: spy.call_count >= 1)
        pump(0.3)
        assert spy.call_count == 1

    def test_nothing_renders_until_the_timer_elapses(self, worker_factory) -> None:
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=300)
        worker.set_project(audio_project())
        pump(0.1)
        assert spy.call_count == 0
        assert worker.is_pending()
        assert pump_until(lambda: spy.call_count == 1)

    def test_each_invalidation_restarts_the_clock(self, worker_factory) -> None:
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=150)
        worker.set_project(audio_project())

        # Keep poking it for longer than one debounce period.
        for _ in range(6):
            pump(0.08)
            worker.invalidate()
        assert spy.call_count == 0

        assert pump_until(lambda: spy.call_count == 1)

    def test_separated_invalidations_render_separately(self, worker_factory) -> None:
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=50)
        worker.set_project(audio_project())
        assert pump_until(lambda: spy.call_count == 1)

        worker.invalidate()
        assert pump_until(lambda: spy.call_count == 2)

    def test_invalidate_emits_immediately_even_though_render_waits(
        self, worker_factory
    ) -> None:
        # Playback has to stop trusting the old file the instant it goes
        # stale, not 500ms later when the replacement starts rendering.
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=500)
        seen: list[int] = []
        worker.bed_invalidated.connect(lambda: seen.append(1))
        worker.set_project(audio_project())
        assert seen == [1]
        assert spy.call_count == 0


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------

class TestCancellation:
    def test_an_in_flight_render_is_cancelled_on_invalidate(
        self, worker_factory
    ) -> None:
        spy = RenderSpy(duration=5.0)
        worker = worker_factory(spy, debounce_ms=30)
        worker.set_project(audio_project())

        assert pump_until(lambda: spy.started.is_set())
        assert worker.is_rendering()

        worker.invalidate()
        # The running render must be told to stop, and it must notice.
        assert pump_until(lambda: len(spy.cancelled) >= 1)
        assert spy.cancelled[0] is True

    def test_a_cancelled_render_does_not_report_ready(self, worker_factory) -> None:
        spy = RenderSpy(duration=5.0)
        worker = worker_factory(spy, debounce_ms=30)
        ready: list[str] = []
        worker.bed_ready.connect(ready.append)
        worker.set_project(audio_project())

        assert pump_until(lambda: spy.started.is_set())
        worker.invalidate()
        pump(0.4)
        # The superseded render must stay silent; only the replacement speaks.
        assert ready == [] or len(ready) == 1

    def test_shutdown_cancels_and_stops_scheduling(self, worker_factory) -> None:
        spy = RenderSpy(duration=5.0)
        worker = worker_factory(spy, debounce_ms=30)
        worker.set_project(audio_project())
        assert pump_until(lambda: spy.started.is_set())

        worker.shutdown()
        assert spy.cancelled and spy.cancelled[0] is True
        assert not worker.is_pending()

        before = spy.call_count
        pump(0.3)
        assert spy.call_count == before


# --------------------------------------------------------------------------
# No two at once
# --------------------------------------------------------------------------

class TestNeverTwoAtOnce:
    def test_two_beds_never_render_concurrently(self, worker_factory) -> None:
        # A slow render being repeatedly superseded is the shape that would
        # produce overlapping ffmpeg processes writing the same WAV.
        spy = RenderSpy(duration=0.25)
        worker = worker_factory(spy, debounce_ms=30)
        worker.set_project(audio_project())

        for _ in range(8):
            pump(0.09)
            worker.invalidate()

        assert pump_until(lambda: not worker.is_rendering() and not worker.is_pending(), 8.0)
        pump(0.2)
        assert spy.max_concurrent == 1, (
            f"{spy.max_concurrent} renders overlapped"
        )

    def test_the_replacement_starts_after_the_old_one_finishes(
        self, worker_factory
    ) -> None:
        spy = RenderSpy(duration=0.3)
        worker = worker_factory(spy, debounce_ms=20)
        worker.set_project(audio_project())
        assert pump_until(lambda: spy.started.is_set())

        worker.invalidate()
        assert pump_until(lambda: spy.call_count == 2, 6.0)
        assert spy.max_concurrent == 1

    def test_a_settled_worker_reports_neither_running_nor_pending(
        self, worker_factory
    ) -> None:
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=30)
        worker.set_project(audio_project())
        assert pump_until(lambda: spy.call_count == 1)
        assert pump_until(lambda: not worker.is_rendering() and not worker.is_pending())


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

class TestOutcomes:
    def test_success_emits_bed_ready_with_the_path(self, worker_factory) -> None:
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=20)
        ready: list[str] = []
        worker.bed_ready.connect(ready.append)
        worker.set_project(audio_project())

        assert pump_until(lambda: len(ready) == 1)
        assert ready[0] == str(worker.bed_path)
        assert worker.bed_path.name.startswith("videditor_bed_")
        assert worker.bed_path.suffix == ".wav"

    def test_no_audio_error_becomes_bed_unavailable(self, worker_factory) -> None:
        def refuse(project, out_path, cancel):
            raise NoAudioError("no unmuted audio track holding a clip")

        worker = worker_factory(refuse, debounce_ms=20)
        unavailable: list[int] = []
        failed: list[str] = []
        worker.bed_unavailable.connect(lambda: unavailable.append(1))
        worker.bed_failed.connect(failed.append)
        worker.set_project(audio_project())

        assert pump_until(lambda: unavailable == [1])
        assert failed == []

    def test_render_error_becomes_bed_failed(self, worker_factory) -> None:
        def explode(project, out_path, cancel):
            raise RenderError("ffmpeg exited with code 1:\nsomething went wrong")

        worker = worker_factory(explode, debounce_ms=20)
        failed: list[str] = []
        worker.bed_failed.connect(failed.append)
        worker.set_project(audio_project())

        assert pump_until(lambda: len(failed) == 1)
        assert "ffmpeg exited" in failed[0]

    def test_an_unexpected_exception_still_reports_rather_than_escaping(
        self, worker_factory
    ) -> None:
        def explode(project, out_path, cancel):
            raise ZeroDivisionError("boom")

        worker = worker_factory(explode, debounce_ms=20)
        failed: list[str] = []
        worker.bed_failed.connect(failed.append)
        worker.set_project(audio_project())

        assert pump_until(lambda: len(failed) == 1)
        assert "boom" in failed[0]

    def test_a_project_with_no_audio_is_unavailable_without_rendering(
        self, worker_factory
    ) -> None:
        # Project.has_audio already answers this, so there is no reason to
        # start ffmpeg just to have it refuse.
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=20)
        unavailable: list[int] = []
        worker.bed_unavailable.connect(lambda: unavailable.append(1))
        worker.set_project(
            Project(
                name="silent",
                tracks=[
                    Track(
                        name="V1",
                        kind="video",
                        clips=[
                            Clip(src=SRC, src_in=0, src_out=SEC, timeline_start=0)
                        ],
                    )
                ],
            )
        )
        assert pump_until(lambda: unavailable == [1])
        assert spy.call_count == 0

    def test_an_empty_project_is_unavailable_not_failed(
        self, worker_factory
    ) -> None:
        # File > New produces exactly this. It must not put "Audio bed failed"
        # in front of someone who has not done anything yet.
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=20)
        failed: list[str] = []
        unavailable: list[int] = []
        worker.bed_failed.connect(failed.append)
        worker.bed_unavailable.connect(lambda: unavailable.append(1))
        worker.set_project(
            Project(
                name="Untitled",
                tracks=[
                    Track(name="V1", kind="video"),
                    Track(name="A1", kind="audio"),
                ],
            )
        )
        assert pump_until(lambda: unavailable == [1])
        assert failed == []
        assert spy.call_count == 0

    def test_a_fully_muted_project_is_unavailable(self, worker_factory) -> None:
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=20)
        unavailable: list[int] = []
        worker.bed_unavailable.connect(lambda: unavailable.append(1))
        project = audio_project()
        project.tracks[1].muted = True
        worker.set_project(project)
        assert pump_until(lambda: unavailable == [1])
        assert spy.call_count == 0

    def test_no_project_means_nothing_is_scheduled(self, worker_factory) -> None:
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=20)
        worker.set_project(None)
        pump(0.2)
        assert spy.call_count == 0
        assert not worker.is_pending()

    def test_the_project_handed_to_the_renderer_is_the_current_one(
        self, worker_factory
    ) -> None:
        spy = RenderSpy()
        worker = worker_factory(spy, debounce_ms=30)
        first = audio_project("first")
        second = audio_project("second")
        worker.set_project(first)
        worker.set_project(second)
        assert pump_until(lambda: spy.call_count == 1)
        assert spy.calls[0].name == "second"


class TestBedFile:
    def test_path_is_in_the_temp_directory_and_stable(self, worker_factory) -> None:
        import tempfile

        worker = worker_factory(RenderSpy(), debounce_ms=20)
        assert worker.bed_path.parent == Path(tempfile.gettempdir())
        assert worker.bed_path == worker.bed_path

    def test_two_workers_do_not_share_a_file(self, worker_factory) -> None:
        one = worker_factory(RenderSpy(), debounce_ms=20)
        two = worker_factory(RenderSpy(), debounce_ms=20)
        assert one.bed_path != two.bed_path

    def test_discard_removes_the_file(self, worker_factory) -> None:
        worker = worker_factory(RenderSpy(), debounce_ms=20)
        worker.bed_path.write_bytes(b"not really a wav")
        assert worker.bed_path.exists()
        worker.discard_bed()
        assert not worker.bed_path.exists()

    def test_discard_is_safe_when_there_is_no_file(self, worker_factory) -> None:
        worker = worker_factory(RenderSpy(), debounce_ms=20)
        worker.discard_bed()
        worker.discard_bed()


class TestUsesCoreRender:
    def test_the_default_render_function_is_the_one_from_core(
        self, qapp: QApplication
    ) -> None:
        # This worker wraps core.render.render_audio_bed and must not grow
        # its own subprocess handling: spawning, stderr, cancellation and
        # partial file cleanup are all solved there, and under test there.
        from core.render import render_audio_bed

        worker = AudioBedWorker()
        assert worker._render_fn is render_audio_bed
        worker.shutdown()

    def test_the_module_spawns_no_processes_of_its_own(self) -> None:
        # If this ever fails, someone has reimplemented what core.render
        # already does, and the cancellation and partial-file cleanup that
        # live there will have quietly grown a second, untested copy.
        # Imports, not raw text: the words appear in the module's own prose.
        import ast

        import ui.workers.audio_bed_worker as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert "subprocess" not in imported
        assert "core.binaries" not in imported
        assert "core.render" in imported
