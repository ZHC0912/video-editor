"""Editing wired into the window: the command stack, the shortcuts, the bed.

The gesture arithmetic is tested in test_interaction.py and the commands
themselves in test_commands.py. What is checked here is that they are joined
up: that the only way the project changes is through the stack, that the stack
is what marks the project dirty, and that an audio edit invalidates the bed
once per gesture rather than once per mouse-move.
"""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402

from core.commands import (  # noqa: E402
    AddClipFromMedia,
    AddTrack,
    CommandError,
    DeleteClip,
    RemoveTrack,
    SetTrackMuted,
)
from core.model import MediaInfo, Track  # noqa: E402
from core.timebase import TICKS_PER_SECOND as T  # noqa: E402
from core.timebase import FrameRate, frames_to_ticks, ticks_to_frames  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.timeline.interaction import decode_media_payload  # noqa: E402
from ui.timeline.timeline_view import (  # noqa: E402
    ADD_VIDEO_LABEL,
    MULTIPLE_VIDEO_TRACK_TOOLTIP,
    VIDEO_LIMIT_LABEL,
)

SRC = Path("clip.mp4")
MUSIC = Path("music.wav")
NO_MODIFIER = Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp: QApplication) -> MainWindow:
    w = MainWindow()
    # Offscreen, nothing lays out until it is shown, and the scene needs a
    # viewport width before it can put lanes anywhere.
    w.timeline.scene.set_viewport_width(1200)
    yield w
    # close() prompts when dirty, and a modal dialog in a fixture teardown
    # would hang the suite.
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
        time.sleep(0.005)
    return predicate()


def tracks(window: MainWindow) -> tuple[Track, Track]:
    return window._project.video_tracks()[0], window._project.audio_tracks()[0]


def add_clip(window: MainWindow, track: Track, start: int, duration: int) -> str:
    src = SRC if track.kind == "video" else MUSIC
    assert window.run_command(
        AddClipFromMedia(track.id, src, 0, duration, start)
    ), "the setup command was refused"
    return next(c.id for c in track.clips if c.timeline_start == start)


def lane_point(window: MainWindow, track: Track, ticks: int) -> QPointF:
    scene = window.timeline.scene
    lane = scene.lane_for_track(track.id)
    return QPointF(scene.ticks_to_x(ticks), lane.top + lane.height / 2)


def select(window: MainWindow, clip_ids: list[str]) -> None:
    window.timeline.scene.set_selected_clip_ids(clip_ids)
    window._refresh_edit_actions()


class TestNothingMutatesTheModelDirectly:
    """Every mutation routes through core.commands, checked structurally.

    A control that changed a track directly would be invisible to undo and
    would leave the project modified with nothing having marked it dirty.
    """

    UI_FILES = sorted(Path("ui").rglob("*.py"))

    def test_no_ui_module_assigns_to_muted(self) -> None:
        offenders = []
        for path in self.UI_FILES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "muted":
                        offenders.append(f"{path}:{node.lineno}")
        assert offenders == [], f"direct mute assignment in {offenders}"

    def test_no_ui_module_appends_to_tracks_or_clips(self) -> None:
        offenders = []
        for path in self.UI_FILES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr not in (
                    "append",
                    "pop",
                    "insert",
                    "remove",
                ):
                    continue
                owner = func.value
                if isinstance(owner, ast.Attribute) and owner.attr in (
                    "tracks",
                    "clips",
                ):
                    offenders.append(f"{path}:{node.lineno}")
        assert offenders == [], f"direct model mutation in {offenders}"

    def test_the_ui_builds_model_objects_in_one_place_only(self) -> None:
        """Commands own that, so undo always has something to reverse.

        new_project is the exception, and is not one: making a fresh project is
        not an edit to a project, has no inverse, and is where the starting
        V1/A1 pair comes from.
        """
        offenders = []
        for path in self.UI_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id in ("Track", "Clip")
                        and node.name != "new_project"
                    ):
                        offenders.append(f"{path}:{inner.lineno} in {node.name}")
        assert offenders == [], f"model object built in the UI at {offenders}"


class TestTheStackIsTheOnlyThingThatMarksDirty:
    """The dirty flag belongs to the command stack and to nothing else."""

    @staticmethod
    def _tree() -> ast.Module:
        import ui.main_window as module

        return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    @staticmethod
    def _functions_calling(tree: ast.Module, name: str) -> list[str]:
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == name
                ):
                    found.append(node.name)
        return sorted(set(found))

    def test_no_control_sets_the_flag_directly(self) -> None:
        """No control anywhere turns the flag on by assigning to it."""
        setters: list[str] = []
        for node in ast.walk(self._tree()):
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

    def test_only_the_stack_marks_the_project_dirty(self) -> None:
        """The companion: mark_dirty's callers are named here or nowhere.

        _after_stack_change is the place every trip through the command
        stack ends up, and was the only caller for most of this project's
        life.

        offer_autosave_recovery is the one edit that is not an edit: it loads
        a whole model that differs from the file on disk, so there is no
        command to push and nothing else would ever mark it. It is listed
        rather than allowed for, so a third caller still fails this test.
        """
        assert self._functions_calling(self._tree(), "mark_dirty") == [
            "_after_stack_change",
            "offer_autosave_recovery",
        ]

    def test_only_push_undo_and_redo_reach_that_place(self) -> None:
        assert self._functions_calling(self._tree(), "_after_stack_change") == [
            "redo",
            "run_command",
            "undo",
        ]

    def test_each_of_those_three_drives_the_stack(self) -> None:
        tree = self._tree()
        for method, call in (
            ("run_command", "push"),
            ("undo", "undo"),
            ("redo", "redo"),
        ):
            assert method in self._functions_calling(tree, call), (
                f"{method} does not call commands.{call}"
            )

    def test_an_edit_makes_the_project_dirty(self, window: MainWindow) -> None:
        assert window.is_dirty() is False
        window.run_command(AddTrack("audio", "A2"))
        assert window.is_dirty() is True

    def test_a_refused_edit_does_not(self, window: MainWindow) -> None:
        assert window.run_command(AddTrack("video", "V2")) is False
        assert window.is_dirty() is False

    def test_undoing_past_a_save_leaves_it_dirty(
        self, window: MainWindow, tmp_path: Path, monkeypatch
    ) -> None:
        window.run_command(AddTrack("audio", "A2"))
        monkeypatch.setattr(
            "ui.main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(tmp_path / "p"), ""),
        )
        window.save_project_as()
        assert window.is_dirty() is False
        window.undo()
        assert window.is_dirty() is True


class TestTrackControlsGoThroughCommands:
    def test_the_mute_toggle_is_undoable(self, window: MainWindow) -> None:
        _, audio = tracks(window)
        window._on_track_muted(audio.id, True)
        assert audio.muted is True
        assert window.commands.undo_label() == "Mute track"
        window.undo()
        assert window._project.audio_tracks()[0].muted is False

    def test_toggling_to_what_it_already_is_pushes_nothing(
        self, window: MainWindow
    ) -> None:
        _, audio = tracks(window)
        window._on_track_muted(audio.id, False)
        assert window.commands.can_undo() is False

    def test_add_track_is_undoable(self, window: MainWindow) -> None:
        window._on_add_track("audio")
        assert [t.name for t in window._project.tracks] == ["V1", "A1", "A2"]
        window.undo()
        assert [t.name for t in window._project.tracks] == ["V1", "A1"]

    def test_a_second_video_track_is_refused_with_a_dialog(
        self, window: MainWindow, monkeypatch
    ) -> None:
        shown: list[tuple] = []
        monkeypatch.setattr(
            "ui.main_window.show_error", lambda *args, **kw: shown.append(args)
        )
        window._on_add_track("video")
        assert len(window._project.video_tracks()) == 1
        assert shown and "compositing" in shown[0][2]
        assert window.commands.can_undo() is False


class TestAddVideoTrackAffordance:
    """The limit is real, so the button has to state it rather than just die.

    A greyed-out "+ Video" reads as a broken control. The label carries the
    reason; the tooltip carries the explanation.
    """

    @staticmethod
    def button(window: MainWindow):
        return window.timeline.headers.add_video_button

    def test_it_states_the_limit_when_a_video_track_exists(
        self, window: MainWindow
    ) -> None:
        button = self.button(window)
        assert button.isEnabled() is False
        assert button.text() == VIDEO_LIMIT_LABEL
        assert button.toolTip() == MULTIPLE_VIDEO_TRACK_TOOLTIP

    def test_removing_the_last_video_track_re_arms_it(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        assert window.run_command(RemoveTrack(video.id))
        button = self.button(window)
        assert button.isEnabled() is True
        assert button.text() == ADD_VIDEO_LABEL
        assert "compositing" not in button.toolTip()

    def test_the_re_armed_button_actually_works(self, window: MainWindow) -> None:
        """A project with no video track cannot export, so this path must run."""
        video, _ = tracks(window)
        window.run_command(RemoveTrack(video.id))
        window._on_add_track("video")
        assert [t.name for t in window._project.video_tracks()] == ["V1"]
        assert self.button(window).text() == VIDEO_LIMIT_LABEL
        assert self.button(window).isEnabled() is False

    def test_it_flips_back_and_forth_with_the_track(
        self, window: MainWindow
    ) -> None:
        button = self.button(window)
        states = []
        for _ in range(3):
            video = window._project.video_tracks()[0]
            window.run_command(RemoveTrack(video.id))
            states.append((button.text(), button.isEnabled()))
            window._on_add_track("video")
            states.append((button.text(), button.isEnabled()))
        assert states == [
            (ADD_VIDEO_LABEL, True),
            (VIDEO_LIMIT_LABEL, False),
        ] * 3

    def test_it_follows_undo_and_redo_of_remove_track(
        self, window: MainWindow
    ) -> None:
        button = self.button(window)
        video, _ = tracks(window)
        window.run_command(RemoveTrack(video.id))
        assert (button.text(), button.isEnabled()) == (ADD_VIDEO_LABEL, True)

        window.undo()
        assert (button.text(), button.isEnabled()) == (VIDEO_LIMIT_LABEL, False)

        window.redo()
        assert (button.text(), button.isEnabled()) == (ADD_VIDEO_LABEL, True)

        window.undo()
        assert (button.text(), button.isEnabled()) == (VIDEO_LIMIT_LABEL, False)

    def test_it_follows_undo_of_add_track_too(self, window: MainWindow) -> None:
        video, _ = tracks(window)
        window.run_command(RemoveTrack(video.id))
        window._on_add_track("video")
        button = self.button(window)
        assert (button.text(), button.isEnabled()) == (VIDEO_LIMIT_LABEL, False)
        window.undo()
        assert (button.text(), button.isEnabled()) == (ADD_VIDEO_LABEL, True)

    def test_loading_a_project_with_no_video_track_arms_it(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        """Not only commands: the button follows whatever project is open."""
        from core.model import Project
        from core.project_io import save

        path = tmp_path / "audio_only.vedit"
        save(Project(name="Audio only", tracks=[Track(name="A1", kind="audio")]), path)
        assert window.load_project_file(path, prompt=False)
        assert self.button(window).text() == ADD_VIDEO_LABEL
        assert self.button(window).isEnabled() is True

    def test_the_audio_button_is_never_limited(self, window: MainWindow) -> None:
        for index in range(2, 7):
            window.run_command(AddTrack("audio", f"A{index}"))
        assert window.timeline.headers.add_audio_button.isEnabled() is True


class TestEditMenu:
    def test_undo_and_redo_start_disabled(self, window: MainWindow) -> None:
        assert window.undo_action.isEnabled() is False
        assert window.redo_action.isEnabled() is False

    def test_they_show_the_command_label(self, window: MainWindow) -> None:
        window.run_command(AddTrack("audio", "A2"))
        assert window.undo_action.text() == "&Undo Add track"
        assert window.undo_action.isEnabled() is True
        window.undo()
        assert window.redo_action.text() == "&Redo Add track"
        assert window.undo_action.text() == "&Undo"
        assert window.undo_action.isEnabled() is False

    def test_redo_answers_to_ctrl_y(self, window: MainWindow) -> None:
        assert any(
            sequence.toString() == "Ctrl+Y"
            for sequence in window.redo_action.shortcuts()
        )

    def test_the_selection_actions_need_a_selection(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        assert window.split_action.isEnabled() is False
        clip_id = add_clip(window, video, 0, 4 * T)
        select(window, [clip_id])
        assert window.split_action.isEnabled() is True
        assert window.delete_action.isEnabled() is True
        assert window.duplicate_action.isEnabled() is True

    def test_a_new_project_empties_the_history(self, window: MainWindow) -> None:
        window.run_command(AddTrack("audio", "A2"))
        window._dirty = False
        window.new_project()
        assert window.commands.can_undo() is False
        assert window.undo_action.isEnabled() is False


class TestShortcutActions:
    def test_split_cuts_the_selected_clip_at_the_playhead(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        clip_id = add_clip(window, video, 0, 4 * T)
        select(window, [clip_id])
        window.move_playhead_to(T)
        window.split_selection()
        assert [(c.timeline_start, c.timeline_end) for c in video.clips] == [
            (0, T),
            (T, 4 * T),
        ]

    def test_split_does_nothing_when_the_playhead_is_outside(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        clip_id = add_clip(window, video, 0, 2 * T)
        select(window, [clip_id])
        window.move_playhead_to(9 * T)
        depth = window.commands.depth()
        window.split_selection()
        assert window.commands.depth() == depth
        assert len(video.clips) == 1

    def test_delete_removes_the_selection(self, window: MainWindow) -> None:
        video, _ = tracks(window)
        first = add_clip(window, video, 0, T)
        second = add_clip(window, video, 2 * T, T)
        select(window, [first, second])
        window.delete_selection()
        assert video.clips == []

    def test_one_undo_puts_a_multiple_delete_back(
        self, window: MainWindow
    ) -> None:
        """Three clips deleted is one thing the user did."""
        video, _ = tracks(window)
        ids = [
            add_clip(window, video, 0, T),
            add_clip(window, video, 2 * T, T),
            add_clip(window, video, 4 * T, T),
        ]
        select(window, ids)
        window.delete_selection()
        window.undo()
        assert len(video.clips) == 3

    def test_duplicate_copies_the_selection(self, window: MainWindow) -> None:
        video, _ = tracks(window)
        clip_id = add_clip(window, video, 0, 2 * T)
        select(window, [clip_id])
        window.duplicate_selection()
        assert [(c.timeline_start, c.timeline_end) for c in video.clips] == [
            (0, 2 * T),
            (2 * T, 4 * T),
        ]

    def test_m_toggles_the_selected_clips_track(self, window: MainWindow) -> None:
        _, audio = tracks(window)
        clip_id = add_clip(window, audio, 0, T)
        select(window, [clip_id])
        window.toggle_selected_track_mute()
        assert audio.muted is True
        window.toggle_selected_track_mute()
        assert audio.muted is False

    def test_m_with_nothing_selected_changes_nothing(
        self, window: MainWindow
    ) -> None:
        window.toggle_selected_track_mute()
        assert window.commands.can_undo() is False

    def test_a_selection_survives_the_edit_that_acted_on_it(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        clip_id = add_clip(window, video, 0, 4 * T)
        select(window, [clip_id])
        window.move_playhead_to(2 * T)
        window.split_selection()
        assert window.timeline.selected_clip_ids() == [clip_id]


class TestPlayheadNudges:
    def test_right_moves_one_frame(self, window: MainWindow) -> None:
        rate = window._project.frame_rate
        window.move_playhead_to(0)
        window.nudge_playhead(1)
        assert window.timeline.playhead_ticks() == frames_to_ticks(1, rate)

    def test_left_moves_back_one_frame(self, window: MainWindow) -> None:
        rate = window._project.frame_rate
        window.move_playhead_to(frames_to_ticks(10, rate))
        window.nudge_playhead(-1)
        assert window.timeline.playhead_ticks() == frames_to_ticks(9, rate)

    def test_shift_moves_a_whole_second(self, window: MainWindow) -> None:
        window.move_playhead_to(0)
        window.nudge_playhead(1, whole_second=True)
        assert window.timeline.playhead_ticks() == T

    def test_it_stops_at_zero(self, window: MainWindow) -> None:
        window.move_playhead_to(0)
        window.nudge_playhead(-1)
        assert window.timeline.playhead_ticks() == 0

    def test_end_goes_to_the_end_of_the_project(self, window: MainWindow) -> None:
        video, _ = tracks(window)
        add_clip(window, video, 0, 5 * T)
        window.playhead_to_end()
        assert window.timeline.playhead_ticks() == 5 * T
        window.playhead_to_start()
        assert window.timeline.playhead_ticks() == 0

    def test_stepping_does_not_drift_at_an_awkward_rate(
        self, window: MainWindow
    ) -> None:
        """The reason nudging converts through the frame index rather than
        adding a per-frame constant."""
        rate = FrameRate(24249, 1000)
        window._project.frame_rate = rate
        window.move_playhead_to(0)
        for _ in range(600):
            window.nudge_playhead(1)
        position = window.timeline.playhead_ticks()
        assert ticks_to_frames(position, rate) == 600
        assert position == frames_to_ticks(600, rate)

    def test_the_status_bar_shows_the_playhead(self, window: MainWindow) -> None:
        window.move_playhead_to(2 * T)
        assert window._status_playhead.text() == "00:00:02:00"


class TestAudioBedInvalidation:
    """Forgetting this one line breaks playback silently.

    The bed would go on playing audio the timeline no longer contains, and
    nothing would report an error.
    """

    def test_an_audio_edit_invalidates_the_bed(self, window: MainWindow) -> None:
        _, audio = tracks(window)
        calls: list[int] = []
        window.audio_bed.invalidate = lambda: calls.append(1)
        window.run_command(SetTrackMuted(audio.id, True))
        assert len(calls) == 1

    def test_a_video_edit_does_not(self, window: MainWindow) -> None:
        video, _ = tracks(window)
        clip_id = add_clip(window, video, 0, T)
        calls: list[int] = []
        window.audio_bed.invalidate = lambda: calls.append(1)
        window.run_command(DeleteClip(video.id, clip_id))
        assert calls == []

    def test_undo_of_an_audio_edit_invalidates_too(
        self, window: MainWindow
    ) -> None:
        _, audio = tracks(window)
        window.run_command(SetTrackMuted(audio.id, True))
        calls: list[int] = []
        window.audio_bed.invalidate = lambda: calls.append(1)
        window.undo()
        assert len(calls) == 1

    def test_a_trim_drag_invalidates_once_not_once_per_move(
        self, window: MainWindow
    ) -> None:
        _, audio = tracks(window)
        add_clip(window, audio, 0, 4 * T)
        scene = window.timeline.scene

        calls: list[int] = []
        window.audio_bed.invalidate = lambda: calls.append(1)

        interaction = window.timeline.interaction
        edge = QPointF(
            scene.ticks_to_x(4 * T) - 1,
            lane_point(window, audio, 0).y(),
        )
        interaction.press(edge, NO_MODIFIER)
        for step in range(30):
            interaction.drag(lane_point(window, audio, 3 * T - step * 1000), NO_MODIFIER)
        assert calls == [], "the model was touched during the drag"
        interaction.release(lane_point(window, audio, 2 * T), NO_MODIFIER)

        assert len(calls) == 1
        assert audio.clips[0].src_out == 2 * T

    def test_a_trim_drag_renders_exactly_one_bed(
        self, window: MainWindow
    ) -> None:
        """End to end, through the real debounce, counting actual renders."""
        _, audio = tracks(window)
        rendered: list[Path] = []

        def fake_render(project, out_path, cancel):
            rendered.append(Path(out_path))
            Path(out_path).write_bytes(b"RIFF")

        window.audio_bed._render_fn = fake_render
        window.audio_bed._timer.setInterval(40)

        add_clip(window, audio, 0, 4 * T)
        assert pump_until(lambda: len(rendered) == 1)
        rendered.clear()

        scene = window.timeline.scene
        interaction = window.timeline.interaction
        edge = QPointF(
            scene.ticks_to_x(4 * T) - 1, lane_point(window, audio, 0).y()
        )
        interaction.press(edge, NO_MODIFIER)
        for step in range(30):
            interaction.drag(lane_point(window, audio, 3 * T - step * 1000), NO_MODIFIER)
            QApplication.instance().processEvents()
        interaction.release(lane_point(window, audio, 2 * T), NO_MODIFIER)

        assert pump_until(lambda: len(rendered) == 1)
        # Give the debounce room to fire a second time if it were going to.
        deadline = time.perf_counter() + 0.4
        while time.perf_counter() < deadline:
            QApplication.instance().processEvents()
            time.sleep(0.01)
        assert len(rendered) == 1, f"{len(rendered)} beds rendered for one trim"


class TestDragFromTheBin:
    """Dragging a bin row onto a lane, from the window's side."""

    @staticmethod
    def bin_row(window: MainWindow, info: MediaInfo) -> QListWidgetItem:
        window._add_media(info)
        return window._bin_row(window._media_key(info.path))

    def test_a_row_publishes_its_probe_result(self, window: MainWindow) -> None:
        info = MediaInfo(
            path=SRC, duration_ticks=3 * T, has_video=True, has_audio=True
        )
        item = self.bin_row(window, info)
        payload = decode_media_payload(window._bin_drag_payload(item))
        assert payload["path"] == str(SRC)
        assert payload["duration_ticks"] == 3 * T
        assert payload["has_video"] is True

    def test_a_row_that_has_not_been_probed_yet_does_not_drag(
        self, window: MainWindow
    ) -> None:
        item = window._create_bin_row(SRC)
        assert window._bin_drag_payload(item) is None

    def test_a_drop_lands_a_full_length_clip(self, window: MainWindow) -> None:
        video, _ = tracks(window)
        info = MediaInfo(
            path=SRC, duration_ticks=3 * T, has_video=True, has_audio=True
        )
        item = self.bin_row(window, info)
        payload = decode_media_payload(window._bin_drag_payload(item))

        interaction = window.timeline.interaction
        interaction.begin_media_drag(payload)
        interaction.update_media_drag(lane_point(window, video, 2 * T), NO_MODIFIER)
        assert interaction.drop_media()

        assert [(c.timeline_start, c.timeline_end) for c in video.clips] == [
            (2 * T, 5 * T)
        ]
        assert window.commands.undo_label() == "Add clip"

    def test_the_bound_on_a_trim_comes_from_the_bin(
        self, window: MainWindow
    ) -> None:
        info = MediaInfo(
            path=SRC, duration_ticks=7 * T, has_video=True, has_audio=False
        )
        window._add_media(info)
        assert window._media_duration(SRC) == 7 * T
        assert window._media_duration(Path("never-seen.mp4")) is None


class TestScrubStopsPlayback:
    """The real gesture, through the interaction and the re-entrancy guard.

    Driving TimelineInteraction rather than the controller directly is the
    point: the guard around the playhead lives in MainWindow, and pausing from
    inside a scrub handler is exactly the path it exists for.
    """

    @staticmethod
    def setup(window: MainWindow) -> tuple:
        """A two-clip timeline with a bed, and a bed player that keeps count.

        A real QMediaPlayer with nothing loaded silently ignores setPosition
        and reports 0 forever, so the calls are recorded and the position is
        held here instead. What is under test is which calls reach the player,
        not what a decoder does with them.
        """
        video, audio = tracks(window)
        add_clip(window, video, 0, 4 * T)
        add_clip(window, video, 4 * T, 4 * T)
        add_clip(window, audio, 0, 8 * T)

        player = window._bed_player
        seeks: list[int] = []
        held = [0]

        def set_position(ms: int) -> None:
            seeks.append(int(ms))
            held[0] = int(ms)

        player.setPosition = set_position
        player.position = lambda: held[0]

        window.playback.set_bed(Path("bed.wav"))
        seeks.clear()
        return video, audio, seeks

    @staticmethod
    def ruler_point(window: MainWindow, ticks: int):
        return QPointF(window.timeline.scene.ticks_to_x(ticks), 4)

    def scrub(self, window: MainWindow, path_ticks: list[int]) -> None:
        interaction = window.timeline.interaction
        interaction.press(self.ruler_point(window, path_ticks[0]), NO_MODIFIER)
        for ticks in path_ticks[1:-1]:
            interaction.drag(self.ruler_point(window, ticks), NO_MODIFIER)
        interaction.release(self.ruler_point(window, path_ticks[-1]), NO_MODIFIER)

    def test_scrubbing_while_playing_leaves_it_paused(
        self, window: MainWindow
    ) -> None:
        _, _, seeks = self.setup(window)
        window.playback.play()
        assert window.playback.is_playing() is True

        self.scrub(window, [T, 2 * T, 3 * T, 5 * T])

        assert window.playback.is_playing() is False
        assert window.timeline.playhead_ticks() == 5 * T
        assert window.playback.position_ticks() == 5 * T

    def test_playback_stops_on_the_press_not_the_release(
        self, window: MainWindow
    ) -> None:
        _, _, seeks = self.setup(window)
        window.playback.play()
        interaction = window.timeline.interaction
        interaction.press(self.ruler_point(window, T), NO_MODIFIER)
        assert window.playback.is_playing() is False
        interaction.release(self.ruler_point(window, T), NO_MODIFIER)
        assert window.playback.is_playing() is False

    def test_the_bed_is_moved_once_on_release(self, window: MainWindow) -> None:
        _, _, seeks = self.setup(window)
        window.playback.play()
        seeks.clear()

        interaction = window.timeline.interaction
        interaction.press(self.ruler_point(window, T), NO_MODIFIER)
        for step in range(30):
            interaction.drag(
                self.ruler_point(window, T + step * T // 10), NO_MODIFIER
            )
        assert seeks == [], "the bed moved during the drag"
        interaction.release(self.ruler_point(window, 4 * T), NO_MODIFIER)
        assert len(seeks) == 1, f"{len(seeks)} bed seeks for one gesture"

    def test_space_after_a_scrub_resumes_from_the_scrubbed_position(
        self, window: MainWindow
    ) -> None:
        _, _, seeks = self.setup(window)
        window.playback.play()
        self.scrub(window, [T, 3 * T, 6 * T])
        assert seeks[-1] == 6000, "the bed was not aligned on release"

        window.preview.toggle_play()  # what Space is bound to
        assert window.playback.is_playing() is True
        assert window.playback.position_ticks() == 6 * T
        assert window._bed_player.position() == 6000

    def test_scrubbing_while_already_paused_does_not_start_playback(
        self, window: MainWindow
    ) -> None:
        _, _, seeks = self.setup(window)
        assert window.playback.is_playing() is False
        self.scrub(window, [T, 2 * T, 3 * T])
        assert window.playback.is_playing() is False

    def test_the_stage_shows_the_right_clip_mid_scrub(
        self, window: MainWindow
    ) -> None:
        video, _, _ = self.setup(window)
        window.playback.play()
        interaction = window.timeline.interaction
        interaction.press(self.ruler_point(window, T), NO_MODIFIER)
        for ticks, index in ((T, 0), (5 * T, 1), (2 * T, 0), (7 * T, 1)):
            interaction.drag(self.ruler_point(window, ticks), NO_MODIFIER)
            assert window.playback._current_clip_id == video.clips[index].id, ticks
            assert window.timeline.scene.playhead_ticks() == ticks
        interaction.release(self.ruler_point(window, 7 * T), NO_MODIFIER)

    def test_the_guard_does_not_swallow_the_scrub(
        self, window: MainWindow
    ) -> None:
        """Pausing calls back into the panel. The playhead must still land."""
        _, _, seeks = self.setup(window)
        window.playback.play()
        self.scrub(window, [T, 2 * T, 3 * T])
        assert window._syncing_playhead is False
        assert window.timeline.playhead_ticks() == 3 * T
        assert window.preview.position_ticks() == 3 * T
        assert window._status_playhead.text() == "00:00:03:00"

    def test_playback_still_drives_the_playhead_afterwards(
        self, window: MainWindow
    ) -> None:
        """The guard must be released, not left set by the scrub."""
        _, _, seeks = self.setup(window)
        window.playback.play()
        self.scrub(window, [T, 2 * T])
        window.playback.play()
        window.preview.report_position(5 * T)
        assert window.timeline.playhead_ticks() == 5 * T

    def test_escape_during_a_scrub_still_aligns_the_bed(
        self, window: MainWindow
    ) -> None:
        _, _, seeks = self.setup(window)
        window.playback.play()
        interaction = window.timeline.interaction
        interaction.press(self.ruler_point(window, T), NO_MODIFIER)
        interaction.drag(self.ruler_point(window, 3 * T), NO_MODIFIER)
        interaction.cancel()
        assert window.playback.is_playing() is False
        assert seeks[-1] == 3000


class TestTheScrubberIsTheSameGestureAsTheRuler:
    """One gesture, two widgets. They must reach the controller identically.

    A scrubber that kept playing while the ruler stopped it is the kind of
    inconsistency a user feels without being able to name it.
    """

    # The same fixtures and the same ruler gesture, so that what differs
    # between the two widgets is only the widget.
    setup = staticmethod(TestScrubStopsPlayback.setup)
    ruler_point = staticmethod(TestScrubStopsPlayback.ruler_point)
    scrub = TestScrubStopsPlayback.scrub

    @staticmethod
    def record(window: MainWindow) -> list[tuple]:
        """Every transport call the controller receives, in order."""
        calls: list[tuple] = []
        controller = window.playback

        def spy(name: str, original):
            def wrapper(*args):
                calls.append((name, *args))
                return original(*args)

            return wrapper

        for name in ("begin_scrub", "scrub_to", "end_scrub", "seek", "play", "pause"):
            setattr(controller, name, spy(name, getattr(controller, name)))
        return calls

    @staticmethod
    def slider_value(window: MainWindow, ticks: int) -> int:
        scrubber = window.preview._scrubber
        return round(ticks * scrubber.maximum() / window.preview.duration_ticks())

    def slide(self, window: MainWindow, path_ticks: list[int]) -> None:
        """The slider equivalent of press, drag, release.

        setSliderDown and setSliderPosition are the real Qt path: down emits
        sliderPressed, a position change while down emits sliderMoved, and up
        emits sliderReleased. Emitting those signals by hand would test the
        test rather than the wiring.
        """
        scrubber = window.preview._scrubber
        scrubber.setSliderDown(True)
        for ticks in path_ticks:
            scrubber.setSliderPosition(self.slider_value(window, ticks))
        scrubber.setSliderDown(False)

    def test_the_two_widgets_produce_the_same_call_sequence(
        self, window: MainWindow
    ) -> None:
        """The same drag, once through each widget.

        Pressing the ruler is itself a destination -- you click where you want
        to go -- so the ruler's press contributes the first position. The
        slider's equivalent is grabbing the handle where the playhead already
        is and dragging through that same first position, which is what a user
        does anyway.
        """
        self.setup(window)
        path = [2 * T, 5 * T, 7 * T]

        window.move_playhead_to(0)
        calls = self.record(window)
        self.scrub(window, path)
        ruler_calls = list(calls)

        window.move_playhead_to(0)
        calls.clear()
        self.slide(window, path)
        slider_calls = list(calls)

        assert ruler_calls == [
            ("begin_scrub",),
            ("scrub_to", 2 * T),
            ("scrub_to", 5 * T),
            ("scrub_to", 7 * T),
            ("end_scrub", 7 * T),
        ]
        assert slider_calls == ruler_calls

    def test_dragging_the_slider_mid_playback_leaves_it_paused(
        self, window: MainWindow
    ) -> None:
        self.setup(window)
        window.playback.play()
        assert window.playback.is_playing() is True

        self.slide(window, [2 * T, 5 * T, 7 * T])

        assert window.playback.is_playing() is False
        assert window.playback.position_ticks() == 7 * T

    def test_the_slider_stops_playback_on_the_press(
        self, window: MainWindow
    ) -> None:
        self.setup(window)
        window.playback.play()
        window.preview._scrubber.setSliderDown(True)
        assert window.playback.is_playing() is False
        window.preview._scrubber.setSliderDown(False)
        assert window.playback.is_playing() is False

    def test_the_slider_moves_the_bed_once_on_release(
        self, window: MainWindow
    ) -> None:
        _, _, seeks = self.setup(window)
        window.playback.play()
        seeks.clear()

        scrubber = window.preview._scrubber
        scrubber.setSliderDown(True)
        for step in range(30):
            scrubber.setSliderPosition(self.slider_value(window, step * T // 10))
        assert seeks == [], "the bed moved during the drag"
        scrubber.setSliderDown(False)
        assert len(seeks) == 1, f"{len(seeks)} bed seeks for one gesture"

    def test_space_after_a_slider_scrub_resumes_from_there(
        self, window: MainWindow
    ) -> None:
        _, _, seeks = self.setup(window)
        window.playback.play()
        self.slide(window, [2 * T, 6 * T])

        window.preview.toggle_play()
        assert window.playback.is_playing() is True
        assert window.playback.position_ticks() == 6 * T
        assert window._bed_player.position() == 6000

    def test_the_timeline_playhead_follows_a_slider_scrub(
        self, window: MainWindow
    ) -> None:
        self.setup(window)
        self.slide(window, [2 * T, 6 * T])
        assert window.timeline.playhead_ticks() == 6 * T
        assert window._syncing_playhead is False


class TestMovingDoesNotChangeTheTransport:
    def test_a_frame_step_while_playing_keeps_playing(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        add_clip(window, video, 0, 4 * T)
        window.playback.play()
        window.nudge_playhead(1)
        assert window.playback.is_playing() is True

    def test_home_and_end_do_not_start_playback(self, window: MainWindow) -> None:
        video, _ = tracks(window)
        add_clip(window, video, 0, 4 * T)
        window.playhead_to_end()
        assert window.playback.is_playing() is False
        window.playhead_to_start()
        assert window.playback.is_playing() is False

    def test_a_nudge_moves_the_playhead_and_the_preview_together(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        add_clip(window, video, 0, 4 * T)
        window.move_playhead_to(2 * T)
        assert window.timeline.playhead_ticks() == 2 * T
        assert window.playback.position_ticks() == 2 * T
        assert window.preview.position_ticks() == 2 * T


class TestFullSession:
    """A whole editing session driven through the window, undone to empty."""

    def test_import_edit_and_undo_back_to_empty(self, window: MainWindow) -> None:
        video, audio = tracks(window)

        first = add_clip(window, video, 0, 4 * T)
        add_clip(window, video, 6 * T, 4 * T)
        add_clip(window, audio, 0, 8 * T)

        select(window, [first])
        window.move_playhead_to(2 * T)
        window.split_selection()
        select(window, [first])
        window.duplicate_selection()

        assert window.run_command(
            AddTrack("audio", "A2")
        ), "a second audio track should be allowed"
        second_audio = window._project.audio_tracks()[1]
        music = audio.clips[0].id
        from core.commands import MoveClip

        assert window.run_command(MoveClip(music, audio.id, second_audio.id, T))

        assert len(video.clips) == 4
        assert window.is_dirty() is True

        while window.commands.can_undo():
            window.undo()

        assert window._project.video_tracks()[0].clips == []
        assert [t.name for t in window._project.tracks] == ["V1", "A1"]
        assert window._project.duration == 0
        assert window.undo_action.isEnabled() is False

    def test_the_export_graph_matches_what_the_timeline_shows(
        self, window: MainWindow
    ) -> None:
        """Both read the same project, so this is really a check that the
        commands leave a project the filter graph will accept."""
        from core.filtergraph import build_full

        video, audio = tracks(window)
        clip = add_clip(window, video, 0, 4 * T)
        add_clip(window, audio, 0, 4 * T)
        select(window, [clip])
        window.move_playhead_to(2 * T)
        window.split_selection()

        args = build_full(window._project)
        assert any("concat" in str(a) for a in args)
        assert window._project.duration == 4 * T

    def test_no_route_through_the_window_can_produce_an_overlap(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        first = add_clip(window, video, 0, 2 * T)
        add_clip(window, video, 2 * T, 2 * T)

        # Duplicate lands past the neighbour rather than on top of it.
        select(window, [first])
        window.duplicate_selection()

        # And an explicit overlapping command is refused.
        assert (
            window.run_command(AddClipFromMedia(video.id, SRC, 0, 2 * T, T)) is False
        )

        Track.model_validate(video.model_dump())

    def test_a_refused_command_leaves_the_history_alone(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        add_clip(window, video, 0, 2 * T)
        depth = window.commands.depth()
        assert (
            window.run_command(AddClipFromMedia(video.id, SRC, 0, 2 * T, T)) is False
        )
        assert window.commands.depth() == depth

    def test_run_command_reports_the_refusal_rather_than_raising(
        self, window: MainWindow
    ) -> None:
        video, _ = tracks(window)
        add_clip(window, video, 0, 2 * T)
        try:
            refused = window.run_command(
                AddClipFromMedia(video.id, SRC, 0, 2 * T, T)
            )
        except CommandError:  # pragma: no cover - the thing being ruled out
            pytest.fail("run_command let a CommandError escape to the caller")
        assert refused is False
        assert "overlap" in window.statusBar().currentMessage()
