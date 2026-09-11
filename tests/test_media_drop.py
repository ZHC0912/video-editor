"""Project file extension, and drag-and-drop import.

The extension tests exist because the app once saved with one extension and
filtered the Open dialog for another, which made saved projects invisible.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QCloseEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.model import Project, Track  # noqa: E402
from core.project_io import (  # noqa: E402
    PROJECT_EXTENSION,
    PROJECT_FORMAT_NAME,
    is_project_path,
    save,
    with_project_extension,
)
from ui.main_window import PROJECT_FILTER, MainWindow  # noqa: E402
from ui.media_bin import dropped_paths, has_droppable_files  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp: QApplication) -> MainWindow:
    w = MainWindow()
    yield w
    # Discard any edits a test made: close() now prompts, and a
    # modal dialog in a fixture teardown would hang the suite.
    w._dirty = False
    w.close()
    w.deleteLater()


def pump_until(predicate, timeout: float = 20.0) -> bool:
    app = QApplication.instance()
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.01)
    return predicate()


#: A drag event holds a bare pointer to its QMimeData. Letting the Python
#: object be collected leaves the event pointing at freed memory, which is an
#: access violation rather than an exception. Everything built here is kept
#: alive for the length of the test session.
_KEEP_ALIVE: list[QMimeData] = []


def drop_mime(paths: list[Path]) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    _KEEP_ALIVE.append(mime)
    return mime


def drag_enter_event(paths: list[Path]) -> QDragEnterEvent:
    return QDragEnterEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        drop_mime(paths),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def send_drop(widget, paths: list[Path]) -> None:
    event = QDropEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        drop_mime(paths),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.dropEvent(event)


# --------------------------------------------------------------------------
# The extension
# --------------------------------------------------------------------------

class TestProjectExtension:
    def test_one_constant_feeds_everything(self) -> None:
        # Three literals is how a .vedit project became invisible to a dialog
        # filtering for *.vidproj.
        assert PROJECT_EXTENSION == ".vedit"
        assert PROJECT_EXTENSION in PROJECT_FILTER
        assert PROJECT_FORMAT_NAME in PROJECT_FILTER
        assert PROJECT_FILTER == f"{PROJECT_FORMAT_NAME} (*{PROJECT_EXTENSION});;All files (*)"

    def test_the_filter_keeps_an_all_files_fallback(self) -> None:
        # So a renamed project is still reachable: load() does not care what a
        # file is called.
        assert "All files (*)" in PROJECT_FILTER

    def test_open_and_save_use_the_same_filter(
        self, window: MainWindow, monkeypatch
    ) -> None:
        seen: list[str] = []
        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getOpenFileName",
            lambda *a, **k: (seen.append(a[3]), ("", ""))[1],
        )
        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (seen.append(a[3]), ("", ""))[1],
        )
        window.open_project()
        window.save_project_as()

        assert len(seen) == 2
        assert seen[0] == seen[1] == PROJECT_FILTER

    def test_save_as_appends_the_extension_when_none_is_typed(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        target = tmp_path / "rtest"
        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(target), ""),
        )
        window.save_project_as()

        assert (tmp_path / f"rtest{PROJECT_EXTENSION}").is_file()
        assert not target.exists()

    def test_save_as_leaves_a_typed_extension_alone(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        target = tmp_path / "rtest.backup"
        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(target), ""),
        )
        window.save_project_as()
        assert target.is_file()

    def test_a_saved_project_matches_the_open_filter(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        # The whole defect in one assertion.
        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(tmp_path / "rtest"), ""),
        )
        window.save_project_as()
        written = next(tmp_path.iterdir())

        pattern = PROJECT_FILTER.split("(")[1].split(")")[0]  # "*.vedit"
        assert written.match(pattern)

    def test_with_project_extension(self) -> None:
        assert with_project_extension(Path("a")) == Path("a.vedit")
        assert with_project_extension(Path("a.vedit")) == Path("a.vedit")
        assert with_project_extension(Path("a.backup")) == Path("a.backup")

    def test_is_project_path_is_case_insensitive(self) -> None:
        assert is_project_path(Path("a.vedit"))
        assert is_project_path(Path("a.VEDIT"))
        assert not is_project_path(Path("a.mp4"))
        assert not is_project_path(Path("a"))


# --------------------------------------------------------------------------
# Drop plumbing
# --------------------------------------------------------------------------

class TestDropMime:
    def test_local_files_are_extracted_in_order(self, qapp) -> None:
        paths = [Path("C:/a.mp4"), Path("C:/b.mov")]
        assert [p.name for p in dropped_paths(drop_mime(paths))] == ["a.mp4", "b.mov"]

    def test_a_drop_with_no_urls_is_not_accepted(self, qapp) -> None:
        mime = QMimeData()
        mime.setText("just some text")
        assert has_droppable_files(mime) is False
        assert dropped_paths(mime) == []

    def test_remote_urls_are_ignored(self, qapp) -> None:
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/video.mp4")])
        assert dropped_paths(mime) == []


class TestDropTargets:
    def test_both_the_window_and_the_bin_accept_drops(
        self, window: MainWindow
    ) -> None:
        # Dropping on the wrong panel is the ordinary mistake.
        assert window.acceptDrops() is True
        assert window.media_list.acceptDrops() is True

    def test_drag_over_highlights_the_target(self, window: MainWindow) -> None:
        window.dragEnterEvent(drag_enter_event([Path("C:/a.mp4")]))
        assert window._drop_frame.property("dropActive") is True

        window.dragLeaveEvent(QDragLeaveEvent())
        assert window._drop_frame.property("dropActive") is False

    def test_the_bin_highlights_itself(self, window: MainWindow) -> None:
        window.media_list.dragEnterEvent(drag_enter_event([Path("C:/a.mp4")]))
        assert window.media_list.property("dropActive") is True
        window.media_list.dragLeaveEvent(QDragLeaveEvent())
        assert window.media_list.property("dropActive") is False

    def test_a_text_drag_is_refused(self, window: MainWindow) -> None:
        mime = QMimeData()
        mime.setText("nope")
        _KEEP_ALIVE.append(mime)
        event = QDragEnterEvent(
            QPoint(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.dragEnterEvent(event)
        assert event.isAccepted() is False
        assert window._drop_frame.property("dropActive") in (None, False)


# --------------------------------------------------------------------------
# Importing
# --------------------------------------------------------------------------

@pytest.mark.slow
class TestDroppingMedia:
    def test_multiple_files_in_one_drop(
        self, window: MainWindow, av_file: Path, video_only_file: Path,
        audio_only_file: Path
    ) -> None:
        send_drop(window, [av_file, video_only_file, audio_only_file])

        # Rows exist immediately, before any probe has returned.
        assert window.media_list.count() == 3
        assert all(
            "Reading..." in window.media_list.item(r).text() for r in range(3)
        )

        assert pump_until(lambda: len(window._media) == 3)
        texts = [window.media_list.item(r).text() for r in range(3)]
        assert not any("Reading..." in t for t in texts)
        assert any("48 kHz" in t for t in texts)
        assert any("silent" in t for t in texts)

    def test_the_window_does_not_block_while_probing(
        self, window: MainWindow, av_file: Path, tmp_path: Path
    ) -> None:
        # Twenty copies, dropped at once. The call must return promptly with
        # every row already on screen.
        copies = []
        for index in range(20):
            copy = tmp_path / f"clip{index}.mp4"
            copy.write_bytes(av_file.read_bytes())
            copies.append(copy)

        started = time.perf_counter()
        send_drop(window, copies)
        elapsed = time.perf_counter() - started

        assert window.media_list.count() == 20
        assert elapsed < 1.0, f"the drop blocked for {elapsed:.2f}s"
        assert pump_until(lambda: len(window._media) == 20, timeout=60)

    def test_a_mixed_drop_keeps_the_good_and_reports_the_bad_once(
        self, window: MainWindow, av_file: Path, tmp_path: Path, monkeypatch
    ) -> None:
        junk = tmp_path / "notes.txt"
        junk.write_text("not a movie", encoding="utf-8")
        other = tmp_path / "readme.md"
        other.write_text("# also not a movie", encoding="utf-8")

        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error", lambda *args: shown.append(args)
        )

        send_drop(window, [av_file, junk, other])
        assert window.media_list.count() == 3

        assert pump_until(lambda: len(shown) == 1, timeout=30)
        # One dialog for the whole batch, not one per bad file.
        assert len(shown) == 1
        assert "2 file(s)" in shown[0][2]
        assert "notes.txt" in shown[0][3] and "readme.md" in shown[0][3]

        # The good file stayed, the bad rows were removed.
        assert pump_until(lambda: window.media_list.count() == 1)
        assert len(window._media) == 1

    def test_dropping_the_same_file_twice_adds_one_row(
        self, window: MainWindow, av_file: Path
    ) -> None:
        send_drop(window, [av_file])
        assert pump_until(lambda: len(window._media) == 1)

        send_drop(window, [av_file])
        assert window.media_list.count() == 1

    def test_duplicates_within_one_drop_are_collapsed(
        self, window: MainWindow, av_file: Path
    ) -> None:
        send_drop(window, [av_file, av_file, av_file])
        assert window.media_list.count() == 1

    def test_dropping_on_the_bin_works_too(
        self, window: MainWindow, av_file: Path
    ) -> None:
        send_drop(window.media_list, [av_file])
        assert window.media_list.count() == 1
        assert pump_until(lambda: len(window._media) == 1)


class TestDroppingAProject:
    def test_a_project_file_is_opened_not_imported(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = tmp_path / f"dropped{PROJECT_EXTENSION}"
        save(
            Project(name="Dropped", tracks=[Track(name="V1", kind="video")]),
            path,
        )

        send_drop(window, [path])

        assert window._project.name == "Dropped"
        assert window._project_path == path
        # It did not land in the media bin.
        assert window.media_list.count() == 0

    def test_a_dirty_project_prompts_before_being_replaced(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = tmp_path / f"dropped{PROJECT_EXTENSION}"
        save(Project(name="Dropped"), path)

        window._dirty = True
        asked: list[int] = []
        monkeypatch.setattr(
            MainWindow,
            "_confirm_discard_changes",
            lambda self: (asked.append(1), False)[1],
        )

        send_drop(window, [path])
        assert asked == [1]
        assert window._project.name != "Dropped", "the prompt was ignored"

    def test_cancelling_the_prompt_leaves_the_project_alone(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = tmp_path / f"dropped{PROJECT_EXTENSION}"
        save(Project(name="Dropped"), path)
        before = window._project

        window._dirty = True
        monkeypatch.setattr(MainWindow, "_confirm_discard_changes", lambda self: False)
        send_drop(window, [path])
        assert window._project is before

    def test_a_clean_project_is_replaced_without_asking(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = tmp_path / f"dropped{PROJECT_EXTENSION}"
        save(Project(name="Dropped"), path)
        window._dirty = False
        send_drop(window, [path])
        assert window._project.name == "Dropped"

    def test_a_corrupt_project_reports_rather_than_raising(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = tmp_path / f"broken{PROJECT_EXTENSION}"
        path.write_text("not json", encoding="utf-8")
        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error", lambda *args: shown.append(args)
        )
        send_drop(window, [path])
        assert len(shown) == 1
        assert shown[0][1] == "Could not open project"

    @pytest.mark.slow
    def test_a_project_and_media_dropped_together(
        self, window: MainWindow, tmp_path: Path, av_file: Path
    ) -> None:
        path = tmp_path / f"both{PROJECT_EXTENSION}"
        save(Project(name="Both"), path)

        send_drop(window, [path, av_file])

        assert window._project.name == "Both"
        assert window.media_list.count() == 1
        assert pump_until(lambda: len(window._media) == 1)


class TestClosePrompt:
    """Closing a window with unsaved edits asks first."""

    def test_a_clean_window_closes_without_asking(
        self, qapp: QApplication, monkeypatch
    ) -> None:
        w = MainWindow()
        asked: list[int] = []
        monkeypatch.setattr(
            MainWindow,
            "_confirm_discard_changes",
            lambda self: (asked.append(1), True)[1],
        )
        w._dirty = False
        event = QCloseEvent()
        w.closeEvent(event)
        assert asked == [1]
        assert event.isAccepted() is True

    def test_cancelling_keeps_the_window_open(
        self, qapp: QApplication, monkeypatch
    ) -> None:
        w = MainWindow()
        w._dirty = True
        monkeypatch.setattr(MainWindow, "_confirm_discard_changes", lambda self: False)

        event = QCloseEvent()
        w.closeEvent(event)

        assert event.isAccepted() is False
        # And nothing was torn down: cancelling must leave a working window,
        # which is why the prompt comes before the teardown.
        assert w.audio_bed._pool is not None
        assert w.playback is not None
        w._dirty = False
        w.close()

    def test_discarding_closes_and_tears_down(
        self, qapp: QApplication, monkeypatch
    ) -> None:
        w = MainWindow()
        w._dirty = True
        monkeypatch.setattr(MainWindow, "_confirm_discard_changes", lambda self: True)

        stopped: list[int] = []
        monkeypatch.setattr(
            type(w.audio_bed),
            "shutdown",
            lambda self: stopped.append(1),
        )

        event = QCloseEvent()
        w.closeEvent(event)
        assert event.isAccepted() is True
        assert stopped == [1]

    def test_an_export_in_flight_is_cancelled_on_close(
        self, qapp: QApplication
    ) -> None:
        import threading

        w = MainWindow()
        w._dirty = False
        cancel = threading.Event()
        w._render_cancel = cancel

        w.closeEvent(QCloseEvent())

        assert cancel.is_set(), "the running export was not told to stop"
        assert w._render_cancel is None
        w.deleteLater()

    def test_cancelling_the_close_leaves_the_export_running(
        self, qapp: QApplication, monkeypatch
    ) -> None:
        # The ordering that matters: answering Cancel must not have already
        # killed the render the user is still waiting for.
        import threading

        w = MainWindow()
        w._dirty = True
        cancel = threading.Event()
        w._render_cancel = cancel
        monkeypatch.setattr(MainWindow, "_confirm_discard_changes", lambda self: False)

        w.closeEvent(QCloseEvent())

        assert not cancel.is_set()
        assert w._render_cancel is cancel
        w._render_cancel = None
        w._dirty = False
        w.close()

    def test_the_prompt_is_the_same_one_open_and_new_use(
        self, qapp: QApplication, monkeypatch
    ) -> None:
        # One helper, so the three ways out of a project cannot disagree.
        w = MainWindow()
        calls: list[str] = []
        monkeypatch.setattr(
            MainWindow,
            "_confirm_discard_changes",
            lambda self: (calls.append("asked"), False)[1],
        )
        w.new_project()
        w.open_project()
        w.closeEvent(QCloseEvent())
        assert calls == ["asked"] * 3
        w._dirty = False
        w.close()


class TestDirtyTracking:
    def test_editing_a_track_marks_the_project_dirty(
        self, window: MainWindow
    ) -> None:
        assert window._dirty is False
        window._on_track_muted(window._project.audio_tracks()[0].id, True)
        assert window._dirty is True

    def test_adding_a_track_marks_it_dirty(self, window: MainWindow) -> None:
        window._on_add_track("audio")
        assert window._dirty is True

    def test_mark_dirty_is_the_single_entry_point(
        self, window: MainWindow
    ) -> None:
        # Everything that changes the project arrives here. Nothing else may
        # turn the flag on, or a control added later will forget to.
        assert window.is_dirty() is False
        window.mark_dirty()
        assert window.is_dirty() is True

    def test_no_control_sets_the_flag_directly(self) -> None:
        import ast

        import ui.main_window as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        setters: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Assign):
                    continue
                for target in inner.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "_dirty"
                        and isinstance(inner.value, ast.Constant)
                        and inner.value.value is True
                    ):
                        setters.append(node.name)
        assert setters == ["mark_dirty"], f"_dirty set to True in {setters}"

    def test_saving_clears_it(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        window._on_add_track("audio")
        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(tmp_path / "p"), ""),
        )
        window.save_project_as()
        assert window._dirty is False
