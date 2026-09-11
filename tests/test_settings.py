"""What the application remembers between runs.

The pure helpers are tested without Qt at all. The store is tested against a
real QSettings pointed at a temporary INI file, because the failures worth
catching here are format failures: a value that survives in the registry and
comes back as something else out of a file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QSettings  # noqa: E402

from ui.settings import (  # noqa: E402
    MAX_RECENT_FILES,
    Settings,
    format_ints,
    format_rect,
    parse_ints,
    parse_rect,
    recent_with,
)


@pytest.fixture
def store(tmp_path: Path) -> Settings:
    return Settings(
        QSettings(str(tmp_path / "videditor.ini"), QSettings.Format.IniFormat)
    )


class TestPureHelpers:
    def test_ints_round_trip(self) -> None:
        assert parse_ints(format_ints([360, 1040])) == [360, 1040]

    def test_a_rect_round_trips(self) -> None:
        rect = QRect(100, 50, 1400, 860)
        assert parse_rect(format_rect(rect)) == rect

    @pytest.mark.parametrize(
        "value", [None, "", "nonsense", "1,2,three", 42, "1,2,3", "1,2,3,4,5"]
    )
    def test_rubbish_never_becomes_a_rectangle(self, value) -> None:
        assert parse_rect(value) is None

    def test_an_empty_rectangle_is_refused(self) -> None:
        # A window restored with no size cannot be grabbed to give it one.
        assert parse_rect("10,10,0,0") is None

    def test_recent_puts_the_newest_first(self) -> None:
        assert recent_with(["b", "c"], "a") == ["a", "b", "c"]

    def test_reopening_a_file_moves_it_up_rather_than_duplicating(self) -> None:
        assert recent_with(["a", "b", "c"], "b") == ["b", "a", "c"]

    def test_the_same_path_in_two_capitalisations_is_one_file(self) -> None:
        assert recent_with([r"C:\Movies\a.vedit"], r"c:\movies\A.VEDIT") == [
            r"c:\movies\A.VEDIT"
        ]

    def test_the_list_is_trimmed_to_the_limit(self) -> None:
        existing = [str(n) for n in range(MAX_RECENT_FILES)]
        assert len(recent_with(existing, "new")) == MAX_RECENT_FILES

    def test_the_limit_is_the_eight_phase_six_asks_for(self) -> None:
        assert MAX_RECENT_FILES == 8


class TestTheStore:
    def test_an_empty_store_answers_with_defaults(self, store: Settings) -> None:
        assert store.geometry() is None
        assert store.zoom() is None
        assert store.recent_files() == []
        assert store.splitter(Settings.SPLIT_TOP) == []
        assert store.maximised() is False

    def test_geometry_survives_a_round_trip_through_a_file(
        self, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "s.ini")
        first = Settings(QSettings(path, QSettings.Format.IniFormat))
        first.set_geometry(QRect(20, 30, 1200, 700))
        first.sync()

        second = Settings(QSettings(path, QSettings.Format.IniFormat))
        assert second.geometry() == QRect(20, 30, 1200, 700)

    def test_splitter_sizes_survive(self, store: Settings) -> None:
        store.set_splitter(Settings.SPLIT_MAIN, [560, 260])
        assert store.splitter(Settings.SPLIT_MAIN) == [560, 260]

    def test_a_collapsed_splitter_is_not_restored(self, store: Settings) -> None:
        # Qt reports zeros for a splitter that has never been laid out, and
        # restoring those collapses the panel with no obvious way back.
        store.set_splitter(Settings.SPLIT_MAIN, [0, 0])
        assert store.splitter(Settings.SPLIT_MAIN) == []

    def test_zoom_survives_as_a_float(self, store: Settings) -> None:
        store.set_zoom(12.5)
        assert store.zoom() == pytest.approx(12.5)

    def test_a_zoom_of_zero_is_no_answer(self, store: Settings) -> None:
        store.set_zoom(0.0)
        assert store.zoom() is None

    def test_maximised_survives(self, store: Settings) -> None:
        store.set_maximised(True)
        assert store.maximised() is True
        store.set_maximised(False)
        assert store.maximised() is False

    def test_one_recent_file_comes_back_as_a_list_of_one(
        self, tmp_path: Path
    ) -> None:
        """The pitfall this storage format exists to avoid.

        A QStringList of one element written to an INI file comes back as a
        bare string, so the natural implementation breaks for exactly the user
        who has opened one project.
        """
        path = str(tmp_path / "s.ini")
        first = Settings(QSettings(path, QSettings.Format.IniFormat))
        first.remember_recent(Path(r"C:\Movies\only.vedit"))
        first.sync()

        second = Settings(QSettings(path, QSettings.Format.IniFormat))
        assert second.recent_files() == [str(Path(r"C:\Movies\only.vedit"))]

    def test_recent_files_survive_in_order(self, store: Settings) -> None:
        for name in ("a", "b", "c"):
            store.remember_recent(Path(f"C:/{name}.vedit"))
        assert [Path(p).name for p in store.recent_files()] == [
            "c.vedit",
            "b.vedit",
            "a.vedit",
        ]

    def test_forgetting_removes_one_and_leaves_the_rest(
        self, store: Settings
    ) -> None:
        for name in ("a", "b"):
            store.remember_recent(Path(f"C:/{name}.vedit"))
        store.forget_recent(Path("C:/a.vedit"))
        assert [Path(p).name for p in store.recent_files()] == ["b.vedit"]

    def test_the_list_never_grows_past_the_limit(self, store: Settings) -> None:
        for index in range(20):
            store.remember_recent(Path(f"C:/p{index}.vedit"))
        assert len(store.recent_files()) == MAX_RECENT_FILES

    def test_a_corrupt_recent_list_is_survivable(self, store: Settings) -> None:
        # A settings file is user-writable and survives upgrades. Nothing in
        # it may be able to stop the application starting.
        store.store.setValue(Settings.RECENT, "{not json")
        assert store.recent_files() == []

    def test_a_corrupt_geometry_is_survivable(self, store: Settings) -> None:
        store.store.setValue(Settings.GEOMETRY, "garbage")
        assert store.geometry() is None
