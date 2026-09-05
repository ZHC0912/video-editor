"""UI smoke tests.

These construct real widgets on the offscreen platform plugin. They cover the
things that are easy to get silently wrong: the frame step arithmetic, the
scrubber's mapping out of the tick domain, the export error surface, and the
rule that the video players stay silent.

They do not attempt playback. Decoding under the offscreen plugin is not
something to build assertions on.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.filtergraph import RenderError, build_full  # noqa: E402
from core.model import Clip, Project, Track  # noqa: E402
from core.timebase import TICKS_PER_SECOND as SEC  # noqa: E402
from core.timebase import FrameRate  # noqa: E402
from ui import theme  # noqa: E402
from ui.dialogs import split_error  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.preview_panel import PreviewController, PreviewPanel  # noqa: E402
from ui.single_player_controller import SinglePlayerController  # noqa: E402
from ui.video_stage import VideoStage  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app)
    return app


@pytest.fixture
def panel(qapp: QApplication) -> PreviewPanel:
    widget = PreviewPanel()
    widget.set_controller(SinglePlayerController(widget.stage))
    yield widget
    widget.deleteLater()


class TestTheme:
    def test_stylesheet_substitutes_every_placeholder(self, qapp) -> None:
        css = theme.stylesheet()
        assert "$" not in css
        assert theme.ACCENT in css
        assert theme.BACKGROUND in css

    def test_no_corner_is_rounder_than_four_pixels(self, qapp) -> None:
        import re

        radii = re.findall(r"border-radius:\s*(\d+)px", theme.stylesheet())
        assert radii, "expected some rounded corners"
        assert max(int(r) for r in radii) <= 4

    def test_mono_font_prefers_cascadia(self, qapp) -> None:
        assert theme.mono_font().families()[0] == "Cascadia Mono"


class TestVideoStageIsSilent:
    def test_neither_player_has_an_audio_output(self, qapp) -> None:
        # Phase 5's master clock is the audio bed. A QAudioOutput here would
        # be a second, competing clock.
        stage = VideoStage()
        assert stage.active_player().audioOutput() is None
        assert stage.standby_player().audioOutput() is None
        stage.deleteLater()

    def test_the_two_players_are_distinct(self, qapp) -> None:
        stage = VideoStage()
        assert stage.active_player() is not stage.standby_player()
        stage.deleteLater()

    def test_present_standby_swaps_the_roles(self, qapp) -> None:
        stage = VideoStage()
        before_active = stage.active_player()
        before_standby = stage.standby_player()
        stage.present_standby()
        assert stage.active_player() is before_standby
        assert stage.standby_player() is before_active
        stage.deleteLater()

    def test_starts_on_black(self, qapp) -> None:
        stage = VideoStage()
        assert stage.is_black() is True
        stage.deleteLater()

    def test_show_active_leaves_black(self, qapp) -> None:
        stage = VideoStage()
        stage.show_active()
        assert stage.is_black() is False
        stage.show_black()
        assert stage.is_black() is True
        stage.deleteLater()


class TestFrameStep:
    """The acceptance case: one frame, at 30fps and at 29.97."""

    def test_thirty_fps(self, panel: PreviewPanel) -> None:
        panel.set_project(Project(name="p", frame_rate=FrameRate(30, 1)))
        assert panel.frame_step_ticks() == 4000

    def test_twenty_nine_ninety_seven(self, panel: PreviewPanel) -> None:
        panel.set_project(Project(name="p", frame_rate=FrameRate(30000, 1001)))
        assert panel.frame_step_ticks() == 4004

    @pytest.mark.parametrize(
        "rate,expected",
        [
            (FrameRate(24, 1), 5000),
            (FrameRate(25, 1), 4800),
            (FrameRate(50, 1), 2400),
            (FrameRate(60, 1), 2000),
            (FrameRate(24000, 1001), 5005),
            (FrameRate(60000, 1001), 2002),
        ],
    )
    def test_every_supported_rate(
        self, panel: PreviewPanel, rate: FrameRate, expected: int
    ) -> None:
        panel.set_project(Project(name="p", frame_rate=rate))
        assert panel.frame_step_ticks() == expected

    def test_stepping_accumulates_without_drift(self, panel: PreviewPanel) -> None:
        # Thirty steps at 29.97 must land on exactly thirty frames, which is
        # where a millisecond based transport would already be wrong.
        panel.set_project(Project(name="p", frame_rate=FrameRate(30000, 1001)))
        panel.report_duration(60 * SEC)
        for _ in range(30):
            panel.step_forward()
        assert panel.position_ticks() == 30 * 4004

    def test_step_back_stops_at_zero(self, panel: PreviewPanel) -> None:
        panel.set_project(Project(name="p", frame_rate=FrameRate(30, 1)))
        panel.report_duration(10 * SEC)
        panel.step_back()
        assert panel.position_ticks() == 0


class TestSteppingDoesNotAccumulate:
    """Stepping converts through the frame index instead of adding.

    At a rate whose frames are not a whole number of ticks long, adding a per
    frame constant drifts. These are the cases that catch it.
    """

    VFR = FrameRate(24249, 1000)

    @pytest.fixture
    def panel(self, qapp: QApplication) -> PreviewPanel:
        from core.timebase import frames_to_ticks

        widget = PreviewPanel()
        widget.set_controller(SinglePlayerController(widget.stage))
        widget.set_project(Project(name="vfr", frame_rate=self.VFR))
        widget.report_duration(frames_to_ticks(100_000, self.VFR))
        yield widget
        widget.deleteLater()

    def test_two_thousand_forward_steps_land_on_the_right_frame(
        self, panel: PreviewPanel
    ) -> None:
        from core.timebase import frames_to_ticks

        panel.seek(0)
        for _ in range(2000):
            panel.step_forward()
        assert panel.position_ticks() == frames_to_ticks(2000, self.VFR)

    def test_two_thousand_alternating_steps_return_to_the_start(
        self, panel: PreviewPanel
    ) -> None:
        from core.timebase import frames_to_ticks

        start = frames_to_ticks(5000, self.VFR)
        panel.seek(start)
        for _ in range(1000):
            panel.step_forward()
            panel.step_back()
        assert panel.position_ticks() == start

    def test_adding_a_constant_would_have_drifted(self) -> None:
        # What the old implementation did, kept as the reason this class
        # exists: 2000 presses of a rounded per frame constant miss by enough
        # to be several frames out.
        from core.timebase import frames_to_ticks

        accumulated = 2000 * frames_to_ticks(1, self.VFR)
        exact = frames_to_ticks(2000, self.VFR)
        assert abs(accumulated - exact) > 600

    def test_a_position_arriving_mid_frame_self_corrects(
        self, panel: PreviewPanel
    ) -> None:
        # Positions come back from the player in milliseconds, so they land
        # between boundaries. One step forward must still end on a boundary.
        from core.timebase import frames_to_ticks, ticks_to_frames

        panel.seek(frames_to_ticks(100, self.VFR) + 1234)
        panel.step_forward()
        position = panel.position_ticks()
        assert position == frames_to_ticks(101, self.VFR)
        assert ticks_to_frames(position, self.VFR) == 101

    def test_stepping_at_a_broadcast_rate_is_unchanged(
        self, qapp: QApplication
    ) -> None:
        widget = PreviewPanel()
        widget.set_controller(SinglePlayerController(widget.stage))
        widget.set_project(Project(name="p", frame_rate=FrameRate(30000, 1001)))
        widget.report_duration(600 * SEC)
        widget.seek(0)
        for _ in range(30):
            widget.step_forward()
        assert widget.position_ticks() == 30 * 4004
        widget.deleteLater()


class TestPreviewPanel:
    def test_timecode_label_tracks_the_position(self, panel: PreviewPanel) -> None:
        panel.set_project(Project(name="p", frame_rate=FrameRate(25, 1)))
        panel.report_duration(10 * SEC)
        panel.report_position(2 * SEC)
        assert panel._timecode.text() == "00:00:02:00"

    def test_position_changed_is_emitted(self, panel: PreviewPanel) -> None:
        seen: list[int] = []
        panel.position_changed.connect(seen.append)
        panel.set_project(Project(name="p"))
        panel.report_duration(10 * SEC)
        panel.report_position(4000)
        assert seen == [4000]

    def test_scrubber_survives_a_timeline_longer_than_an_int32(
        self, panel: PreviewPanel
    ) -> None:
        # 24 hours is over ten billion ticks. Feeding that to a QSlider range
        # would overflow, so the scrubber has a fixed resolution instead.
        day = 24 * 3600 * SEC
        panel.set_project(Project(name="p"))
        panel.report_duration(day)
        panel.report_position(day // 2)
        assert panel._scrubber.maximum() == 100_000
        assert panel._scrubber.value() == pytest.approx(50_000, abs=1)

    def test_transport_is_disabled_until_there_is_something_to_play(
        self, panel: PreviewPanel
    ) -> None:
        panel.set_project(Project(name="p"))
        assert panel._play_pause.isEnabled() is False
        panel.report_duration(5 * SEC)
        assert panel._play_pause.isEnabled() is True

    def test_the_controller_satisfies_the_protocol(self, panel: PreviewPanel) -> None:
        # Phase 5 swaps this out. The protocol is the contract it has to meet.
        assert isinstance(panel.controller(), PreviewController)

    def test_status_line_starts_empty(self, panel: PreviewPanel) -> None:
        assert panel._status.text() == ""
        panel.set_status("Rendering audio bed")
        assert panel._status.text() == "Rendering audio bed"


class TestErrorSurface:
    def test_render_error_splits_into_headline_and_detail(self) -> None:
        exc = RenderError("ffmpeg exited with code 1:\nline one\nline two")
        headline, detail = split_error(exc)
        assert headline == "ffmpeg exited with code 1:"
        assert detail == "line one\nline two"

    def test_a_single_line_error_has_no_detail(self) -> None:
        headline, detail = split_error(RenderError("project has no tracks"))
        assert headline == "project has no tracks"
        assert detail == ""

    def test_probe_error_stderr_is_not_duplicated(self) -> None:
        from core.probe import ProbeError

        exc = ProbeError("ffprobe failed for a.mp4", "Invalid data found")
        headline, detail = split_error(exc)
        assert headline == "ffprobe failed for a.mp4"
        assert detail.count("Invalid data found") == 1


class TestMainWindow:
    @pytest.fixture
    def window(self, qapp: QApplication) -> MainWindow:
        w = MainWindow()
        yield w
        # Discard any edits a test made: close() now prompts, and a
        # modal dialog in a fixture teardown would hang the suite.
        w._dirty = False
        w.close()
        w.deleteLater()

    def test_opens_with_an_empty_project(self, window: MainWindow) -> None:
        assert window._project is not None
        assert window._project.name == "Untitled"
        assert [t.name for t in window._project.tracks] == ["V1", "A1"]

    def test_file_menu_has_every_action(self, window: MainWindow) -> None:
        titles = [
            a.text().replace("&", "")
            for a in window.menuBar().actions()[0].menu().actions()
            if a.text()
        ]
        assert titles == [
            "New",
            "Open...",
            "Save",
            "Save As...",
            "Import Media...",
            "Export...",
            "Exit",
        ]

    def test_opens_inside_the_available_screen_area(
        self, window: MainWindow, qapp: QApplication
    ) -> None:
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            pytest.skip("no screen")
        available = screen.availableGeometry()
        assert available.contains(window.geometry()), (
            f"{window.geometry()} escapes {available}"
        )

    def test_minimum_size_fits_a_small_laptop(self, window: MainWindow) -> None:
        assert window.minimumWidth() <= 1024
        assert window.minimumHeight() <= 640

    def test_a_geometry_from_a_vanished_monitor_is_not_restored(
        self, window: MainWindow, qapp: QApplication
    ) -> None:
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            pytest.skip("no screen")

        window.apply_geometry(QRect(9000, -4000, 1400, 860))
        assert screen.availableGeometry().contains(window.geometry())

    def test_one_setsource_per_load(
        self, window: MainWindow, qapp: QApplication, monkeypatch
    ) -> None:
        # Every extra setSource is another demux of the same file. Phase 5's
        # preloading assumes exactly one per intended load.
        from pathlib import Path

        calls: list[str] = []
        original = QMediaPlayer.setSource

        def counted(self, url):
            calls.append(url.fileName())
            return original(self, url)

        monkeypatch.setattr(QMediaPlayer, "setSource", counted)
        window.player.load(Path("nonexistent.mp4"))
        assert len(calls) == 1

    def test_the_preview_declares_itself_silent(self, window: MainWindow) -> None:
        assert window.preview._volume.isEnabled() is False

    def test_the_timeline_renders_the_project(self, window: MainWindow) -> None:
        assert [lane.kind for lane in window.timeline.scene.lanes()] == [
            "video",
            "audio",
        ]

    def test_the_preview_drives_the_playhead(self, window: MainWindow) -> None:
        window.preview.report_duration(10 * SEC)
        window.preview.report_position(2 * SEC)
        assert window.timeline.scene.playhead_ticks() == 2 * SEC

    def test_add_video_track_is_disabled_from_the_start(
        self, window: MainWindow
    ) -> None:
        # A new project already has V1, so the affordance is off immediately.
        assert window.timeline.headers.add_video_button.isEnabled() is False

    def test_adding_an_audio_track_updates_the_timeline(
        self, window: MainWindow
    ) -> None:
        before = len(window.timeline.scene.lanes())
        window._on_add_track("audio")
        assert len(window.timeline.scene.lanes()) == before + 1
        assert len(window._project.audio_tracks()) == 2

    def test_adding_a_second_video_track_is_refused(
        self, window: MainWindow, monkeypatch
    ) -> None:
        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error", lambda *args: shown.append(args)
        )
        window._on_add_track("video")
        assert len(window._project.video_tracks()) == 1
        assert len(shown) == 1
        assert "compositing" in shown[0][2]

    def test_muting_an_audio_track_invalidates_the_bed(
        self, window: MainWindow
    ) -> None:
        seen: list[int] = []
        window.audio_bed.bed_invalidated.connect(lambda: seen.append(1))
        audio_track = window._project.audio_tracks()[0]
        window._on_track_muted(audio_track.id, True)
        assert audio_track.muted is True
        assert seen == [1]

    def test_muting_a_video_track_does_not_invalidate_the_bed(
        self, window: MainWindow
    ) -> None:
        seen: list[int] = []
        window.audio_bed.bed_invalidated.connect(lambda: seen.append(1))
        video_track = window._project.video_tracks()[0]
        window._on_track_muted(video_track.id, True)
        assert video_track.muted is True
        assert seen == []

    def test_loading_a_project_points_the_bed_at_it(
        self, window: MainWindow
    ) -> None:
        project = Project(name="loaded", tracks=[Track(name="V1", kind="video")])
        window._set_project(project, None)
        assert window.audio_bed._project is project

    def test_status_bar_reports_the_project(self, window: MainWindow) -> None:
        assert window._status_name.text() == "Untitled"
        assert window._status_duration.text() == "00:00:00:00"
        assert "1920x1080" in window._status_rate.text()
        assert "30 fps" in window._status_rate.text()

    def test_media_bin_shows_a_poster_frame(self, window: MainWindow) -> None:
        from pathlib import Path

        from core.model import MediaInfo
        from ui import poster

        window._add_media(
            MediaInfo(
                path=Path("C:/media/take.mp4"),
                duration_ticks=100 * SEC,
                width=1920,
                height=1080,
                frame_rate=FrameRate(30, 1),
                has_video=True,
                has_audio=True,
                sample_rate=48000,
            )
        )
        item = window.media_list.item(0)
        # The row is there and drawn before any decode has finished.
        assert not item.icon().isNull()
        assert window.media_list.iconSize() == poster.POSTER_SIZE

    def test_the_bin_populates_before_any_thumbnail_arrives(
        self, window: MainWindow, monkeypatch
    ) -> None:
        from pathlib import Path

        from core.model import MediaInfo

        # No worker may run at all: the list must not block on one.
        started: list[object] = []
        monkeypatch.setattr(
            "ui.workers.thumbnail_worker.media_pool",
            lambda: type("P", (), {"start": lambda self, job: started.append(job)})(),
        )
        window._thumbnails._cache.clear()
        window._thumbnails._in_flight.clear()

        for index in range(3):
            window._add_media(
                MediaInfo(
                    path=Path(f"C:/media/take{index}.mp4"),
                    duration_ticks=60 * SEC,
                    width=1920,
                    height=1080,
                    frame_rate=FrameRate(30, 1),
                    has_video=True,
                    has_audio=True,
                )
            )

        assert window.media_list.count() == 3
        for row in range(3):
            assert not window.media_list.item(row).icon().isNull()
        # The requests were made, but nothing waited for them.
        assert len(started) == 3

    def test_a_poster_frame_updates_the_row_in_place(
        self, window: MainWindow
    ) -> None:
        from pathlib import Path

        from PySide6.QtGui import QColor, QPixmap

        from core.model import MediaInfo
        from ui import poster
        from ui.workers.thumbnail_worker import quantise_tick

        path = Path("C:/media/late.mp4")
        info = MediaInfo(
            path=path,
            duration_ticks=100 * SEC,
            width=1920,
            height=1080,
            frame_rate=FrameRate(30, 1),
            has_video=True,
            has_audio=False,
        )
        window._add_media(info)
        row = window.media_list.count() - 1
        item = window.media_list.item(row)
        placeholder = item.icon().pixmap(poster.POSTER_SIZE).toImage()

        # Deliver a frame the way the worker would.
        tick = quantise_tick(poster.poster_tick(info))
        frame = QPixmap(160, 90)
        frame.fill(QColor("#ff00ff"))
        window._thumbnails._cache[(str(path), tick)] = frame
        window._thumbnails.thumbnail_ready.emit(str(path), tick)

        assert window.media_list.count() == row + 1, "a row was added, not updated"
        updated = window.media_list.item(row).icon().pixmap(poster.POSTER_SIZE).toImage()
        assert updated != placeholder

    def test_the_poster_frame_is_taken_from_inside_the_file(self) -> None:
        from pathlib import Path

        from core.model import MediaInfo
        from ui import poster

        # Not frame zero: many files open on black or a fade.
        info = MediaInfo(path=Path("a.mp4"), duration_ticks=100 * SEC, has_video=True)
        assert poster.poster_tick(info) == 10 * SEC
        assert poster.poster_tick(info) > 0

    def test_an_audio_only_file_gets_a_glyph_not_a_broken_image(
        self, window: MainWindow, monkeypatch
    ) -> None:
        from pathlib import Path

        from core.model import MediaInfo

        started: list[object] = []
        monkeypatch.setattr(
            "ui.workers.thumbnail_worker.media_pool",
            lambda: type("P", (), {"start": lambda self, job: started.append(job)})(),
        )
        window._add_media(
            MediaInfo(
                path=Path("C:/media/music.wav"),
                duration_ticks=60 * SEC,
                has_video=False,
                has_audio=True,
                sample_rate=48000,
            )
        )
        item = window.media_list.item(window.media_list.count() - 1)
        assert not item.icon().isNull()
        # No decode is attempted for a file with no picture.
        assert started == []

    def test_portrait_footage_is_not_distorted(self) -> None:
        from PySide6.QtGui import QColor, QPixmap

        from ui import poster

        # The 358x642 test file: taller than it is wide.
        frame = QPixmap(358, 642)
        frame.fill(QColor("#00ff00"))
        fitted = poster.fit_poster(frame)

        assert fitted.size() == poster.POSTER_SIZE
        image = fitted.toImage()
        green = [
            x
            for x in range(image.width())
            if QColor(image.pixel(x, image.height() // 2)).green() > 200
        ]
        # The frame keeps 358:642, so at 64 tall it is about 36 wide, centred
        # in the 114 wide box rather than stretched across it.
        assert 30 <= len(green) <= 42, f"portrait frame occupies {len(green)}px"
        assert abs((green[0] + green[-1]) / 2 - image.width() / 2) <= 2

    def test_the_bin_and_the_timeline_share_one_cache(
        self, window: MainWindow
    ) -> None:
        assert window._thumbnails is window.timeline.scene.thumbnails

    def test_media_bin_shows_a_summary_line(self, window: MainWindow) -> None:
        from core.model import MediaInfo
        from pathlib import Path

        window._add_media(
            MediaInfo(
                path=Path("C:/media/take.mp4"),
                duration_ticks=90 * SEC,
                width=1920,
                height=1080,
                frame_rate=FrameRate(30, 1),
                has_video=True,
                has_audio=True,
                sample_rate=48000,
            )
        )
        text = window.media_list.item(0).text()
        assert text.startswith("take.mp4\n")
        assert "1920x1080" in text

    def test_export_refuses_an_empty_project_before_asking_for_a_filename(
        self, window: MainWindow, monkeypatch
    ) -> None:
        # The user can reach this: File > New, then Export. It must be a
        # dialog, not a traceback, and no save dialog should appear.
        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error",
            lambda *args: shown.append(args),
        )

        def explode(*args, **kwargs):
            raise AssertionError("the save dialog must not open")

        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getSaveFileName", explode
        )

        window.export_project()

        assert len(shown) == 1
        assert shown[0][1] == "Cannot export"
        assert "no clips" in shown[0][2]

    def test_export_refuses_a_second_video_track(
        self, window: MainWindow, monkeypatch
    ) -> None:
        from pathlib import Path

        src = Path("a.mp4")
        window._set_project(
            Project(
                name="two",
                tracks=[
                    Track(
                        name="V1",
                        kind="video",
                        clips=[
                            Clip(src=src, src_in=0, src_out=SEC, timeline_start=0)
                        ],
                    ),
                    Track(
                        name="V2",
                        kind="video",
                        clips=[
                            Clip(src=src, src_in=0, src_out=SEC, timeline_start=0)
                        ],
                    ),
                ],
            ),
            None,
        )
        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error", lambda *args: shown.append(args)
        )
        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: pytest.fail("the save dialog must not open"),
        )

        window.export_project()

        assert len(shown) == 1
        assert "one video track" in shown[0][2]

    def test_a_renderable_project_passes_validation(
        self, window: MainWindow
    ) -> None:
        from pathlib import Path

        project = Project(
            name="ok",
            tracks=[
                Track(
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(
                            src=Path("a.mp4"),
                            src_in=0,
                            src_out=SEC,
                            timeline_start=0,
                        )
                    ],
                )
            ],
        )
        # The same call export_project makes before touching the file dialog.
        build_full(project)


class TestProgressWiring:
    def test_the_dialog_is_driven_by_the_callback_alone(
        self, qapp: QApplication
    ) -> None:
        # Amendment 2: no timer, no interpolation. The dialog only moves when
        # core.render calls back.
        from PySide6.QtWidgets import QProgressDialog

        window = MainWindow()
        window._render_dialog = QProgressDialog("", "Cancel", 0, 1000, window)
        window._render_dialog.setAutoClose(False)
        window._render_dialog.setAutoReset(False)
        window._render_dialog.setValue(0)

        assert window._render_dialog.value() == 0
        window._on_export_progress(15.0, 60.0)
        assert window._render_dialog.value() == 250
        assert window._render_dialog.labelText() == "15.0s of 1:00"

        window._on_export_progress(60.0, 60.0)
        assert window._render_dialog.value() == 1000

        window._teardown_export()
        window._dirty = False
        window.close()
        window.deleteLater()

    def test_cancel_sets_the_event(self, qapp: QApplication) -> None:
        import threading

        window = MainWindow()
        window._render_cancel = threading.Event()
        window._on_export_cancelled()
        assert window._render_cancel.is_set()
        window._dirty = False
        window.close()
        window.deleteLater()
