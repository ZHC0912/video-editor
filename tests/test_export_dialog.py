"""The export dialog, and what the window does with what it returns.

The dialog is a value producer: it has no idea how a render is run, and these
tests read the value rather than watching for side effects. The window tests
at the bottom check the one thing that joins them, which is that what was
chosen is what reaches core.render.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox  # noqa: E402

from core.encoders import NVENC_H264, SOFTWARE_H264  # noqa: E402
from core.model import Clip, Project, Track  # noqa: E402
from core.timebase import TICKS_PER_SECOND as T  # noqa: E402
from ui.export_dialog import (  # noqa: E402
    HARDWARE_TOOLTIP,
    SOURCE_PRESET,
    ExportDialog,
    ExportRequest,
)
from ui.main_window import MainWindow  # noqa: E402
from ui.settings import Settings  # noqa: E402

GONE = Path("C:/nowhere/gone.mp4")


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def a_project(src: Path, width: int = 1920, height: int = 1080) -> Project:
    return Project(
        name="demo",
        width=width,
        height=height,
        tracks=[
            Track(
                name="V1",
                kind="video",
                clips=[Clip(src=src, src_in=0, src_out=4 * T, timeline_start=0)],
            ),
            Track(name="A1", kind="audio"),
        ],
    )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x")
    return a_project(src)


@pytest.fixture
def window(qapp: QApplication, tmp_path: Path) -> MainWindow:
    settings = Settings(
        QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    )
    w = MainWindow(settings)
    yield w
    w._dirty = False
    w.close()
    w.deleteLater()


class TestTheChoices:
    def test_it_starts_on_source_and_medium(
        self, qapp: QApplication, project: Project
    ) -> None:
        dialog = ExportDialog(project)
        assert dialog._resolution.currentText() == SOURCE_PRESET
        assert dialog.out_size() is None
        assert dialog.crf() == 20

    def test_choosing_720p_scales_the_output(
        self, qapp: QApplication, project: Project
    ) -> None:
        dialog = ExportDialog(project)
        dialog._resolution.setCurrentText("720p")
        assert dialog.out_size() == (1280, 720)

    def test_the_quality_names_map_to_crf(
        self, qapp: QApplication, project: Project
    ) -> None:
        dialog = ExportDialog(project)
        for name, crf in (("High (CRF 18)", 18), ("Small (CRF 24)", 24)):
            dialog._quality.setCurrentText(name)
            assert dialog.crf() == crf

    def test_the_summary_names_the_size_and_the_duration(
        self, qapp: QApplication, project: Project
    ) -> None:
        dialog = ExportDialog(project)
        assert "1920x1080" in dialog._summary.text()
        # Estimated output duration, from project.duration.
        assert "00:00:04:00" in dialog._summary.text()

    def test_the_summary_follows_the_resolution_choice(
        self, qapp: QApplication, project: Project
    ) -> None:
        dialog = ExportDialog(project)
        dialog._resolution.setCurrentText("480p")
        assert "854x480" in dialog._summary.text()


class TestTheDestination:
    def test_a_name_without_an_extension_gets_one(
        self, qapp: QApplication, project: Project, tmp_path: Path
    ) -> None:
        dialog = ExportDialog(project)
        dialog._path_edit.setText(str(tmp_path / "holiday"))
        assert dialog.out_path() == tmp_path / "holiday.mp4"

    def test_a_name_with_an_extension_is_left_alone(
        self, qapp: QApplication, project: Project, tmp_path: Path
    ) -> None:
        dialog = ExportDialog(project)
        dialog._path_edit.setText(str(tmp_path / "holiday.mov"))
        assert dialog.out_path() == tmp_path / "holiday.mov"

    def test_no_destination_is_no_request(
        self, qapp: QApplication, project: Project
    ) -> None:
        dialog = ExportDialog(project)
        assert dialog.out_path() is None
        assert dialog.request() is None

    def test_export_is_disabled_until_there_is_somewhere_to_write(
        self, qapp: QApplication, project: Project, tmp_path: Path
    ) -> None:
        dialog = ExportDialog(project)
        button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert button.text() == "Export"
        assert button.isEnabled() is False

        dialog._path_edit.setText(str(tmp_path / "out.mp4"))
        assert button.isEnabled() is True

    def test_a_suggested_path_is_offered(
        self, qapp: QApplication, project: Project, tmp_path: Path
    ) -> None:
        dialog = ExportDialog(project, suggested_path=tmp_path / "s.mp4")
        assert dialog.out_path() == tmp_path / "s.mp4"


class TestHardwareEncoding:
    def test_it_is_absent_when_the_machine_cannot_do_it(
        self, qapp: QApplication, project: Project
    ) -> None:
        # Absent, not disabled: a disabled checkbox invites the user to work
        # out how to enable it, and on a machine with no NVIDIA GPU there is
        # nothing to work out.
        dialog = ExportDialog(project, hardware_available=False)
        assert dialog._hardware.isVisible() is False
        assert dialog.encoder() == SOFTWARE_H264

    def test_it_is_offered_when_the_machine_can(
        self, qapp: QApplication, project: Project
    ) -> None:
        dialog = ExportDialog(project, hardware_available=True)
        dialog.show()
        try:
            assert dialog._hardware.isVisible() is True
        finally:
            dialog.close()

    def test_it_defaults_to_off(self, qapp: QApplication, project: Project) -> None:
        dialog = ExportDialog(project, hardware_available=True)
        assert dialog._hardware.isChecked() is False
        assert dialog.encoder() == SOFTWARE_H264

    def test_ticking_it_selects_nvenc(
        self, qapp: QApplication, project: Project
    ) -> None:
        dialog = ExportDialog(project, hardware_available=True)
        dialog._hardware.setChecked(True)
        assert dialog.encoder() == NVENC_H264
        assert dialog.request() is None  # still needs a destination

    def test_the_tooltip_says_what_the_trade_is(self) -> None:
        tooltip = HARDWARE_TOOLTIP.lower()
        assert "faster" in tooltip
        assert "lower" in tooltip and "quality" in tooltip


class TestTheRequest:
    def test_it_carries_everything_that_was_chosen(
        self, qapp: QApplication, project: Project, tmp_path: Path
    ) -> None:
        dialog = ExportDialog(project, hardware_available=True)
        dialog._path_edit.setText(str(tmp_path / "out.mp4"))
        dialog._resolution.setCurrentText("720p")
        dialog._quality.setCurrentText("High (CRF 18)")
        dialog._hardware.setChecked(True)

        request = dialog.request()

        assert request == ExportRequest(
            out_path=tmp_path / "out.mp4",
            crf=18,
            encoder=NVENC_H264,
            out_size=(1280, 720),
        )
        assert request.hardware is True


class TestTheWindowUsesIt:
    """The join: what was chosen is what reaches the render."""

    @staticmethod
    def capture(window: MainWindow, monkeypatch) -> list:
        started: list = []
        monkeypatch.setattr(
            MainWindow,
            "start_export",
            lambda self, project, request: started.append((project, request)),
        )
        return started

    @staticmethod
    def choose(monkeypatch, request: ExportRequest) -> None:
        class Stub:
            def __init__(self, *args, **kwargs) -> None:
                self.project = args[0] if args else None

            def exec(self) -> int:
                return QDialog.DialogCode.Accepted

            def request(self) -> ExportRequest:
                return request

        monkeypatch.setattr("ui.main_window.ExportDialog", Stub)

    def test_the_request_reaches_the_render(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"x")
        window._set_project(a_project(src), None)

        wanted = ExportRequest(tmp_path / "o.mp4", 18, NVENC_H264, (1280, 720))
        self.choose(monkeypatch, wanted)
        started = self.capture(window, monkeypatch)

        window.export_project()

        assert len(started) == 1
        assert started[0][1] == wanted

    def test_cancelling_the_dialog_starts_nothing(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"x")
        window._set_project(a_project(src), None)

        class Rejecting:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def exec(self) -> int:
                return QDialog.DialogCode.Rejected

            def request(self):
                return None

        monkeypatch.setattr("ui.main_window.ExportDialog", Rejecting)
        started = self.capture(window, monkeypatch)

        window.export_project()

        assert started == []

    def test_missing_clips_are_left_out_of_the_export(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"x")
        project = a_project(src)
        project.tracks[0].clips.append(
            Clip(src=GONE, src_in=0, src_out=T, timeline_start=6 * T)
        )
        window._set_project(project, None)

        self.choose(
            monkeypatch, ExportRequest(tmp_path / "o.mp4", 20, SOFTWARE_H264, None)
        )
        monkeypatch.setattr("ui.main_window.confirm", lambda *a, **k: True)
        started = self.capture(window, monkeypatch)

        window.export_project()

        exported = started[0][0]
        assert [c.src for c in exported.tracks[0].clips] == [src]
        assert len(project.tracks[0].clips) == 2, "the real project was modified"

    def test_the_user_is_told_before_clips_are_dropped(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"x")
        project = a_project(src)
        project.tracks[0].clips.append(
            Clip(src=GONE, src_in=0, src_out=T, timeline_start=6 * T)
        )
        window._set_project(project, None)

        asked: list[tuple] = []
        self.choose(
            monkeypatch, ExportRequest(tmp_path / "o.mp4", 20, SOFTWARE_H264, None)
        )
        monkeypatch.setattr(
            "ui.main_window.confirm", lambda *a: asked.append(a) or False
        )
        started = self.capture(window, monkeypatch)

        window.export_project()

        assert len(asked) == 1
        assert "gone.mp4" in asked[0][2]
        assert started == [], "declining the warning must not export"

    def test_a_project_whose_media_has_all_gone_says_so(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        window._set_project(a_project(GONE), None)

        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error", lambda *args: shown.append(args)
        )
        monkeypatch.setattr(
            "ui.main_window.ExportDialog",
            lambda *a, **k: pytest.fail("nothing is exportable"),
        )

        window.export_project()

        assert len(shown) == 1
        assert "missing source file" in shown[0][2]
        assert "gone.mp4" in shown[0][3]

    def test_a_structural_problem_is_reported_against_the_real_project(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        """Two video tracks is a complaint about the timeline, not the media.

        Validating the trimmed copy first would answer "there are two video
        tracks" with "there are no clips", which is true of the copy and
        useless to the user looking at the timeline.
        """
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"x")
        project = a_project(src)
        project.tracks.append(
            Track(
                name="V2",
                kind="video",
                clips=[Clip(src=src, src_in=0, src_out=T, timeline_start=0)],
            )
        )
        window._set_project(project, None)

        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error", lambda *args: shown.append(args)
        )
        window.export_project()

        assert len(shown) == 1
        assert "one video track" in shown[0][2]

    def test_the_suggested_name_sits_beside_the_project_file(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"x")
        window._set_project(a_project(src), tmp_path / "holiday.vedit")
        assert window._suggested_export_path() == tmp_path / "holiday.mp4"

    def test_an_unsaved_project_suggests_its_name(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        src = tmp_path / "clip.mp4"
        src.write_bytes(b"x")
        window._set_project(a_project(src), None)
        assert window._suggested_export_path() == Path("demo.mp4")
