"""Phase 6 in the window: what is remembered, what is recovered, what is swept.

The lifecycle pieces are wired to real files under tmp_path rather than
mocked. An autosave that is written but never readable, or a settings value
that survives one process and not the next, is exactly the class of bug this
phase can introduce, and only a real round trip catches it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from core.commands import AddTrack, RelinkMedia  # noqa: E402
from core.model import Clip, Project, Track  # noqa: E402
from core.project_io import (  # noqa: E402
    autosave_is_newer,
    autosave_path,
    discard_autosave,
    load,
    save,
)
from core.timebase import TICKS_PER_SECOND as T  # noqa: E402
from ui.main_window import AUTOSAVE_INTERVAL_MS, MainWindow  # noqa: E402
from ui.settings import Settings  # noqa: E402
from ui.workers.audio_bed_worker import (  # noqa: E402
    BED_PREFIX,
    BED_SUFFIX,
    purge_stale_beds,
)

GONE = Path("C:/nowhere/gone.mp4")
GONE_AUDIO = Path("C:/nowhere/gone.wav")


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_settings(tmp_path: Path, name: str = "settings.ini") -> Settings:
    return Settings(QSettings(str(tmp_path / name), QSettings.Format.IniFormat))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def window(qapp: QApplication, settings: Settings) -> MainWindow:
    w = MainWindow(settings)
    w.timeline.scene.set_viewport_width(1200)
    yield w
    w._dirty = False
    w.close()
    w.deleteLater()


def media_file(tmp_path: Path) -> Path:
    """A source file that actually exists.

    It matters that this is real. A project loaded with a missing source
    opens the relink dialog, which is modal, which in a test run means a
    suite that stops rather than fails.
    """
    path = tmp_path / "clip.mp4"
    if not path.exists():
        path.write_bytes(b"not really an mp4, but it is on disk")
    return path


def a_project(src: Path) -> Project:
    return Project(
        name="demo",
        tracks=[
            Track(
                name="V1",
                kind="video",
                clips=[Clip(src=src, src_in=0, src_out=2 * T, timeline_start=0)],
            ),
            Track(name="A1", kind="audio"),
        ],
    )


def saved_project(tmp_path: Path, src: Path | None = None) -> Path:
    path = tmp_path / "demo.vedit"
    save(a_project(src if src is not None else media_file(tmp_path)), path)
    return path


# ---------------------------------------------------------------- autosave


class TestAutosave:
    def test_the_interval_is_the_two_minutes_phase_six_asks_for(self) -> None:
        assert AUTOSAVE_INTERVAL_MS == 120_000

    def test_the_timer_runs_from_startup(self, window: MainWindow) -> None:
        # Not restarted per edit: a single shot timer re-armed on every
        # keystroke would autosave two minutes after the last edit, which
        # during a long session is never.
        assert window._autosave_timer.isActive()
        assert window._autosave_timer.interval() == AUTOSAVE_INTERVAL_MS

    def test_it_writes_beside_the_project_file(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = saved_project(tmp_path)
        window.load_project_file(path, prompt=False)
        window.run_command(AddTrack("audio", "A2"))

        assert window.autosave_now() is True
        assert autosave_path(path).is_file()
        assert autosave_path(path).name == "demo.vedit.autosave"

    def test_the_autosave_holds_the_unsaved_work(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = saved_project(tmp_path)
        window.load_project_file(path, prompt=False)
        window.run_command(AddTrack("audio", "A2"))
        window.autosave_now()

        recovered = load(autosave_path(path))
        assert [t.name for t in recovered.tracks] == ["V1", "A1", "A2"]
        assert [t.name for t in load(path).tracks] == ["V1", "A1"]

    def test_a_clean_project_does_not_autosave(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = saved_project(tmp_path)
        window.load_project_file(path, prompt=False)
        assert window.is_dirty() is False
        assert window.autosave_now() is False
        assert not autosave_path(path).exists()

    def test_an_unsaved_project_has_nowhere_to_autosave(
        self, window: MainWindow
    ) -> None:
        # The one real hole in the recovery story, and the one Phase 6
        # specifies: the sidecar lives beside the project file, and an
        # Untitled project does not have one.
        window.new_project()
        window.run_command(AddTrack("audio", "A2"))
        assert window.autosave_path() is None
        assert window.autosave_now() is False

    def test_saving_removes_the_sidecar(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = saved_project(tmp_path)
        window.load_project_file(path, prompt=False)
        window.run_command(AddTrack("audio", "A2"))
        window.autosave_now()
        assert autosave_path(path).is_file()

        window.save_project()

        assert not autosave_path(path).exists(), "a stale autosave would be offered back"

    def test_a_clean_close_removes_the_sidecar(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        path = saved_project(tmp_path)
        w = MainWindow(make_settings(tmp_path))
        w.load_project_file(path, prompt=False)
        w.run_command(AddTrack("audio", "A2"))
        w.autosave_now()
        assert autosave_path(path).is_file()

        w._dirty = False  # answer the close prompt without a dialog
        w.close()

        assert not autosave_path(path).exists()

    def test_a_failing_autosave_is_reported_and_not_raised(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = saved_project(tmp_path)
        window.load_project_file(path, prompt=False)
        window.run_command(AddTrack("audio", "A2"))

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("ui.main_window.save", boom)
        assert window.autosave_now() is False
        assert "Autosave failed" in window.statusBar().currentMessage()


class TestRecovery:
    @staticmethod
    def leave_a_crash(tmp_path: Path) -> Path:
        """A saved project plus a newer autosave, as a killed session leaves."""
        path = saved_project(tmp_path)
        crashed = a_project(media_file(tmp_path))
        crashed.tracks.append(Track(name="A2", kind="audio"))
        time.sleep(0.01)
        save(crashed, autosave_path(path))
        return path

    def test_a_newer_autosave_is_a_recovery_candidate(self, tmp_path: Path) -> None:
        path = self.leave_a_crash(tmp_path)
        assert autosave_is_newer(path) is True

    def test_an_older_autosave_is_not(self, tmp_path: Path) -> None:
        path = saved_project(tmp_path)
        save(a_project(media_file(tmp_path)), autosave_path(path))
        time.sleep(0.01)
        save(a_project(media_file(tmp_path)), path)  # saved after the autosave
        assert autosave_is_newer(path) is False

    def test_no_autosave_is_not(self, tmp_path: Path) -> None:
        assert autosave_is_newer(saved_project(tmp_path)) is False

    def test_an_orphaned_autosave_is_not_offered(self, tmp_path: Path) -> None:
        path = saved_project(tmp_path)
        save(a_project(media_file(tmp_path)), autosave_path(path))
        path.unlink()
        # There is nothing to recover it into.
        assert autosave_is_newer(path) is False

    def test_candidates_come_from_the_recent_list(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = self.leave_a_crash(tmp_path)
        window.settings.remember_recent(path)
        assert window.pending_recoveries() == [path]

    def test_a_project_that_was_never_opened_is_not_a_candidate(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        self.leave_a_crash(tmp_path)
        # Nothing in the recent list, and there is nowhere else to look.
        assert window.pending_recoveries() == []

    def test_accepting_loads_the_autosave(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = self.leave_a_crash(tmp_path)
        window.settings.remember_recent(path)
        monkeypatch.setattr("ui.main_window.confirm", lambda *a, **k: True)

        assert window.offer_autosave_recovery() is True
        assert [t.name for t in window._project.tracks] == ["V1", "A1", "A2"]

    def test_the_recovered_project_points_at_the_real_file(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = self.leave_a_crash(tmp_path)
        window.settings.remember_recent(path)
        monkeypatch.setattr("ui.main_window.confirm", lambda *a, **k: True)
        window.offer_autosave_recovery()

        # Save must overwrite the project, never the sidecar.
        assert window._project_path == path
        window.save_project()
        assert [t.name for t in load(path).tracks] == ["V1", "A1", "A2"]

    def test_a_recovered_project_is_dirty(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = self.leave_a_crash(tmp_path)
        window.settings.remember_recent(path)
        monkeypatch.setattr("ui.main_window.confirm", lambda *a, **k: True)
        window.offer_autosave_recovery()
        # The file on disk is still the old one.
        assert window.is_dirty() is True

    def test_declining_deletes_the_autosave(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = self.leave_a_crash(tmp_path)
        window.settings.remember_recent(path)
        monkeypatch.setattr("ui.main_window.confirm", lambda *a, **k: False)

        assert window.offer_autosave_recovery() is False
        assert not autosave_path(path).exists()

    def test_nothing_to_recover_asks_nothing(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        window.settings.remember_recent(saved_project(tmp_path))
        monkeypatch.setattr(
            "ui.main_window.confirm",
            lambda *a, **k: pytest.fail("asked with nothing to recover"),
        )
        assert window.offer_autosave_recovery() is False

    def test_discard_autosave_tolerates_a_missing_file(self, tmp_path: Path) -> None:
        discard_autosave(tmp_path / "never_existed.vedit")


# ------------------------------------------------------------ recent files


class TestRecentFiles:
    def test_saving_adds_the_project(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = tmp_path / "new.vedit"
        window.new_project()
        window._write_project(path)
        assert window.settings.recent_files() == [str(path)]

    def test_opening_adds_the_project(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        path = saved_project(tmp_path)
        window.load_project_file(path, prompt=False)
        assert window.settings.recent_files() == [str(path)]

    def test_the_menu_lists_them_newest_first(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        for name in ("one", "two"):
            path = tmp_path / f"{name}.vedit"
            save(a_project(media_file(tmp_path)), path)
            window.load_project_file(path, prompt=False)

        titles = [a.text() for a in window.recent_menu.actions() if a.text()]
        assert titles[0].endswith("two.vedit")
        assert titles[1].endswith("one.vedit")

    def test_an_empty_list_says_so_rather_than_showing_nothing(
        self, window: MainWindow
    ) -> None:
        actions = [a for a in window.recent_menu.actions() if a.text()]
        assert len(actions) == 1
        assert actions[0].text() == "No recent projects"
        assert actions[0].isEnabled() is False

    def test_clearing_empties_the_menu(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        window.load_project_file(saved_project(tmp_path), prompt=False)
        window.clear_recent_files()
        assert window.settings.recent_files() == []
        assert [a.text() for a in window.recent_menu.actions() if a.text()] == [
            "No recent projects"
        ]

    def test_a_stale_entry_is_dropped_when_it_is_clicked(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = saved_project(tmp_path)
        window.load_project_file(path, prompt=False)
        path.unlink()

        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error", lambda *args: shown.append(args)
        )

        assert window.open_recent(path) is False
        assert window.settings.recent_files() == []
        assert len(shown) == 1

    def test_the_list_survives_into_the_next_session(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        path = saved_project(tmp_path)

        first = MainWindow(make_settings(tmp_path))
        first.load_project_file(path, prompt=False)
        first._dirty = False
        first.close()

        second = MainWindow(make_settings(tmp_path))
        try:
            assert second.settings.recent_files() == [str(path)]
        finally:
            second._dirty = False
            second.close()


# ------------------------------------------------------------ window shape


class TestRememberedLayout:
    def test_a_saved_geometry_is_restored(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            pytest.skip("no screen under this platform plugin")
        available = screen.availableGeometry()
        wanted = QRect(available.x() + 60, available.y() + 60, 1024, 700)

        settings = make_settings(tmp_path)
        settings.set_geometry(wanted)

        w = MainWindow(settings)
        try:
            assert w.geometry() == wanted
        finally:
            w._dirty = False
            w.close()

    def test_an_unreachable_geometry_falls_back_to_the_validator(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        """Amendment 2: the existing is_reachable() decides, not a new check."""
        from PySide6.QtGui import QGuiApplication

        from ui import window_geometry

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            pytest.skip("no screen under this platform plugin")

        settings = make_settings(tmp_path)
        # A monitor that is no longer plugged in.
        settings.set_geometry(QRect(-9000, -9000, 1200, 700))

        w = MainWindow(settings)
        try:
            assert w.geometry() == window_geometry.default_geometry(
                screen.availableGeometry()
            )
        finally:
            w._dirty = False
            w.close()

    def test_splitter_sizes_are_handed_back_to_the_splitter(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        """The saved sizes reach setSizes; what Qt then does with them is Qt's.

        A splitter clamps what it is given to the width it actually has and to
        its children's minimums, so asserting on the pixels that come back out
        would be testing Qt's layout arithmetic against the size of whatever
        screen the suite is running on.
        """
        settings = make_settings(tmp_path)
        settings.set_splitter(Settings.SPLIT_TOP, [500, 900])
        settings.set_splitter(Settings.SPLIT_MAIN, [600, 300])

        w = MainWindow(settings)
        applied: list[list[int]] = []
        w._top_splitter.setSizes = lambda sizes: applied.append(list(sizes))
        w._main_splitter.setSizes = lambda sizes: applied.append(list(sizes))
        w.show()
        try:
            assert applied == [[500, 900], [600, 300]]
        finally:
            w._dirty = False
            w.close()

    def test_the_proportion_survives_a_round_trip(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        w = MainWindow(settings)
        w.show()
        w._top_splitter.setSizes([500, 900])
        saved = w._top_splitter.sizes()
        w._dirty = False
        w.close()

        assert make_settings(tmp_path).splitter(Settings.SPLIT_TOP) == saved

    def test_the_layout_is_restored_once_and_not_on_every_show(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        """Otherwise un-minimising would undo the user's own drag."""
        settings = make_settings(tmp_path)
        settings.set_splitter(Settings.SPLIT_TOP, [500, 900])

        w = MainWindow(settings)
        applied: list[list[int]] = []
        w._top_splitter.setSizes = lambda sizes: applied.append(list(sizes))
        w.show()
        w.hide()
        w.show()
        try:
            assert applied == [[500, 900]], "the layout was restored twice"
        finally:
            w._dirty = False
            w.close()

    def test_the_zoom_is_restored(self, qapp: QApplication, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.set_zoom(37.5)

        w = MainWindow(settings)
        w.show()
        try:
            assert w.timeline.zoom() == pytest.approx(37.5)
        finally:
            w._dirty = False
            w.close()

    def test_closing_writes_the_shape_back(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        w = MainWindow(settings)
        w.show()
        w.timeline.set_zoom(21.0)
        w._dirty = False
        w.close()

        reread = make_settings(tmp_path)
        assert reread.zoom() == pytest.approx(21.0)
        assert reread.geometry() is not None
        assert reread.splitter(Settings.SPLIT_MAIN)

    def test_a_maximised_window_saves_its_normal_geometry(
        self, qapp: QApplication, tmp_path: Path
    ) -> None:
        """Otherwise it reopens edge to edge forever, with no way back."""
        settings = make_settings(tmp_path)
        w = MainWindow(settings)
        w.show()
        normal = w.normalGeometry()
        w.showMaximized()
        w._dirty = False
        w.close()

        reread = make_settings(tmp_path)
        assert reread.geometry() == normal
        assert reread.maximised() is True


# ----------------------------------------------------------- temp hygiene


class TestTempFileHygiene:
    def test_an_old_bed_is_swept(self, tmp_path: Path) -> None:
        old = tmp_path / f"{BED_PREFIX}old{BED_SUFFIX}"
        old.write_bytes(b"x")
        os.utime(old, (time.time() - 48 * 3600, time.time() - 48 * 3600))

        assert purge_stale_beds(directory=tmp_path) == 1
        assert not old.exists()

    def test_a_recent_bed_is_left_alone(self, tmp_path: Path) -> None:
        fresh = tmp_path / f"{BED_PREFIX}fresh{BED_SUFFIX}"
        fresh.write_bytes(b"x")
        assert purge_stale_beds(directory=tmp_path) == 0
        assert fresh.exists()

    def test_nothing_else_in_temp_is_touched(self, tmp_path: Path) -> None:
        other = tmp_path / "someone_elses.wav"
        other.write_bytes(b"x")
        os.utime(other, (0, 0))
        assert purge_stale_beds(directory=tmp_path) == 0
        assert other.exists()

    def test_an_unreadable_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert purge_stale_beds(directory=tmp_path / "does_not_exist") == 0

    def test_the_session_bed_is_named_to_the_pattern(
        self, window: MainWindow
    ) -> None:
        name = window.audio_bed.bed_path.name
        assert name.startswith(BED_PREFIX)
        assert name.endswith(BED_SUFFIX)

    def test_startup_sweeps_before_the_window_exists(self) -> None:
        """A sweep after the window is built could race its own bed."""
        source = (Path(__file__).resolve().parent.parent / "app.py").read_text(
            encoding="utf-8"
        )
        assert "purge_stale_beds()" in source
        assert source.index("purge_stale_beds()") < source.index("MainWindow()")


# ---------------------------------------------------------- missing media


class TestMissingMedia:
    @staticmethod
    def with_missing(window: MainWindow) -> Project:
        project = Project(
            name="broken",
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(src=GONE, src_in=0, src_out=T, timeline_start=0),
                        Clip(src=GONE, src_in=0, src_out=T, timeline_start=2 * T),
                    ],
                ),
                Track(
                    name="A1",
                    kind="audio",
                    clips=[
                        Clip(src=GONE_AUDIO, src_in=0, src_out=T, timeline_start=0)
                    ],
                ),
            ],
        )
        window._set_project(project, None)
        return project

    def test_the_window_finds_them_on_load(self, window: MainWindow) -> None:
        self.with_missing(window)
        assert [m.name for m in window._missing_sources()] == ["gone.mp4", "gone.wav"]

    def test_every_clip_of_a_missing_file_is_marked(
        self, window: MainWindow
    ) -> None:
        project = self.with_missing(window)
        expected = {c.id for t in project.tracks for c in t.clips}
        assert window.timeline.scene.missing_clip_ids() == expected

    def test_the_timeline_paints_them_as_unresolved(
        self, window: MainWindow
    ) -> None:
        project = self.with_missing(window)
        clip_id = project.tracks[0].clips[0].id
        item = window.timeline.scene.clip_item(clip_id)
        assert item is not None
        assert item.is_unresolved() is True

    def test_a_present_file_is_not_marked(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        real = tmp_path / "there.mp4"
        real.write_bytes(b"x")
        window._set_project(a_project(real), None)
        clip_id = window._project.tracks[0].clips[0].id
        assert window.timeline.scene.clip_item(clip_id).is_unresolved() is False

    def test_an_unresolved_clip_asks_for_no_thumbnails(
        self, window: MainWindow, monkeypatch
    ) -> None:
        """Every request would spawn an ffmpeg that fails, on every layout."""
        project = self.with_missing(window)
        item = window.timeline.scene.clip_item(project.tracks[0].clips[0].id)
        asked: list = []
        # monkeypatch, not assignment: the thumbnail cache is shared with the
        # media bin and with every other scene, so a stub left on it would
        # follow the rest of the suite around.
        monkeypatch.setattr(
            window.timeline.scene.thumbnails, "request", lambda *a: asked.append(a)
        )
        item.refresh_media()
        assert asked == []

    def test_the_menu_says_how_many(self, window: MainWindow) -> None:
        self.with_missing(window)
        assert window.relink_action.isEnabled() is True
        assert "(2)" in window.relink_action.text()

    def test_the_menu_entry_is_dead_when_nothing_is_missing(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        real = tmp_path / "there.mp4"
        real.write_bytes(b"x")
        window._set_project(a_project(real), None)
        assert window.relink_action.isEnabled() is False
        assert window.relink_action.text() == "&Relink missing media..."

    def test_relinking_repairs_every_clip_of_that_file(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        project = self.with_missing(window)
        replacement = tmp_path / "found.mp4"
        replacement.write_bytes(b"x")

        monkeypatch.setattr(
            "ui.main_window.RelinkDialog",
            lambda missing, parent: _StubRelink({GONE: replacement}),
        )

        assert window.relink_missing_media() is True
        assert [c.src for c in project.tracks[0].clips] == [replacement, replacement]
        assert project.tracks[1].clips[0].src == GONE_AUDIO

    def test_a_relink_is_undoable(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        project = self.with_missing(window)
        replacement = tmp_path / "found.mp4"
        replacement.write_bytes(b"x")
        monkeypatch.setattr(
            "ui.main_window.RelinkDialog",
            lambda missing, parent: _StubRelink({GONE: replacement}),
        )
        window.relink_missing_media()

        window.undo()

        assert [c.src for c in project.tracks[0].clips] == [GONE, GONE]
        assert window.timeline.scene.clip_item(
            project.tracks[0].clips[0].id
        ).is_unresolved() is True

    def test_a_relink_clears_the_marks(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        project = self.with_missing(window)
        replacement = tmp_path / "found.mp4"
        replacement.write_bytes(b"x")
        monkeypatch.setattr(
            "ui.main_window.RelinkDialog",
            lambda missing, parent: _StubRelink({GONE: replacement}),
        )
        window.relink_missing_media()

        video_ids = {c.id for c in project.tracks[0].clips}
        assert window.timeline.scene.missing_clip_ids().isdisjoint(video_ids)
        assert [m.name for m in window._missing_sources()] == ["gone.wav"]

    def test_cancelling_changes_nothing(
        self, window: MainWindow, monkeypatch
    ) -> None:
        project = self.with_missing(window)
        monkeypatch.setattr(
            "ui.main_window.RelinkDialog",
            lambda missing, parent: _StubRelink({}, accepted=False),
        )
        assert window.relink_missing_media() is False
        assert project.tracks[0].clips[0].src == GONE
        assert window.commands.can_undo() is False

    def test_relinking_with_nothing_missing_says_so(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        real = tmp_path / "there.mp4"
        real.write_bytes(b"x")
        window._set_project(a_project(real), None)
        assert window.relink_missing_media() is False
        assert "No missing media" in window.statusBar().currentMessage()

    def test_loading_a_project_with_missing_media_offers_the_dialog(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        path = saved_project(tmp_path, src=GONE)
        opened: list[int] = []

        def stub(missing, parent):
            opened.append(len(missing))
            return _StubRelink({}, accepted=False)

        monkeypatch.setattr("ui.main_window.RelinkDialog", stub)
        window.load_project_file(path, prompt=False)

        assert opened == [1], "a project whose media has moved must say so on load"

    def test_loading_a_healthy_project_opens_nothing(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        real = tmp_path / "there.mp4"
        real.write_bytes(b"x")
        path = saved_project(tmp_path, src=real)
        monkeypatch.setattr(
            "ui.main_window.RelinkDialog",
            lambda *a, **k: pytest.fail("nothing is missing"),
        )
        assert window.load_project_file(path, prompt=False) is True


class _StubRelink:
    """Stands in for RelinkDialog: answers with what the test decided."""

    def __init__(self, replacements: dict, accepted: bool = True) -> None:
        self._replacements = replacements
        self._accepted = accepted

    def exec(self) -> int:
        return (
            QDialog.DialogCode.Accepted
            if self._accepted
            else QDialog.DialogCode.Rejected
        )

    def replacements(self) -> dict:
        return dict(self._replacements)


class TestRelinkCommandIsAddressedById:
    """Amendment 4, at the window level.

    The model order here is ['A1', 'V1'], which is what removing and
    re-adding the video track leaves behind, while the timeline still draws
    video on top. A relink that counted positions would repair the audio clip
    with the video file and never raise.
    """

    def test_the_right_clip_is_repaired_when_the_order_is_inverted(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        video_clip = Clip(src=GONE, src_in=0, src_out=T, timeline_start=0)
        audio_clip = Clip(src=GONE_AUDIO, src_in=0, src_out=T, timeline_start=0)
        project = Project(
            name="inverted",
            tracks=[
                Track(name="A1", kind="audio", clips=[audio_clip]),
                Track(name="V1", kind="video", clips=[video_clip]),
            ],
        )
        window._set_project(project, None)
        assert [t.name for t in project.tracks] == ["A1", "V1"]

        new_video = tmp_path / "new_video.mp4"
        new_video.write_bytes(b"x")
        window.run_command(RelinkMedia({video_clip.id: new_video}))

        assert video_clip.src == new_video
        assert audio_clip.src == GONE_AUDIO
