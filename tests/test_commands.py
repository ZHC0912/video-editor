"""Editing commands and the undo stack.

No Qt anywhere in this file: core.commands is part of core and must stay
importable without a GUI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.commands import (
    AddClipFromMedia,
    AddTrack,
    Command,
    CommandError,
    CommandStack,
    DeleteClip,
    DuplicateClip,
    MacroCommand,
    MoveClip,
    RelinkMedia,
    RemoveTrack,
    SetClipGain,
    SetTrackMuted,
    SplitClip,
    TrimClip,
    free_span,
    snap_all,
)
from core.model import Clip, Project, Track
from core.timebase import TICKS_PER_SECOND as T
from core.timebase import FrameRate, snap_to_frame

SRC = Path("clip.mp4")


def make_project(rate: FrameRate = FrameRate(30, 1)) -> Project:
    return Project(
        name="Test",
        frame_rate=rate,
        tracks=[Track(name="V1", kind="video"), Track(name="A1", kind="audio")],
    )


def video(project: Project) -> Track:
    return project.video_tracks()[0]


def audio(project: Project) -> Track:
    return project.audio_tracks()[0]


def place(track: Track, start: int, duration: int, src: Path = SRC) -> Clip:
    clip = Clip(src=src, src_in=0, src_out=duration, timeline_start=start)
    track.clips.append(clip)
    track.clips.sort(key=lambda c: c.timeline_start)
    return clip


def spans(track: Track) -> list[tuple[int, int]]:
    return [(c.timeline_start, c.timeline_end) for c in track.clips]


class TestHelpers:
    def test_snap_all_uses_the_project_frame_rate(self) -> None:
        project = make_project(FrameRate(25, 1))
        one_frame = T // 25
        assert snap_all(project, one_frame + 10, 2 * one_frame - 10) == (
            one_frame,
            2 * one_frame,
        )

    def test_snap_all_returns_a_tuple_of_the_same_length(self) -> None:
        project = make_project()
        assert snap_all(project) == ()
        assert len(snap_all(project, 1, 2, 3)) == 3

    def test_free_span_is_half_open(self) -> None:
        track = Track(name="V1", kind="video")
        place(track, 0, T)
        # Butted up against the end is free; one tick earlier is not.
        assert free_span(track, T, 2 * T)
        assert not free_span(track, T - 1, 2 * T)

    def test_free_span_can_look_past_named_clips(self) -> None:
        track = Track(name="V1", kind="video")
        clip = place(track, 0, T)
        assert not free_span(track, 0, T)
        assert free_span(track, 0, T, ignore=(clip.id,))


class TestAddClipFromMedia:
    def test_it_places_a_clip(self) -> None:
        project = make_project()
        track = video(project)
        AddClipFromMedia(track.id, SRC, 0, 2 * T, T).do(project)
        assert spans(track) == [(T, 3 * T)]

    def test_undo_removes_exactly_the_clip_it_added(self) -> None:
        project = make_project()
        track = video(project)
        existing = place(track, 0, T)
        command = AddClipFromMedia(track.id, SRC, 0, T, 5 * T)
        command.do(project)
        command.undo(project)
        assert [c.id for c in track.clips] == [existing.id]

    def test_redo_reuses_the_same_clip_id(self) -> None:
        """Otherwise a command pushed after this one points at nothing."""
        project = make_project()
        track = video(project)
        command = AddClipFromMedia(track.id, SRC, 0, T, 0)
        command.do(project)
        first = track.clips[0].id
        command.undo(project)
        command.do(project)
        assert track.clips[0].id == first

    def test_an_overlap_is_refused_and_changes_nothing(self) -> None:
        project = make_project()
        track = video(project)
        place(track, 0, 2 * T)
        with pytest.raises(CommandError, match="overlap"):
            AddClipFromMedia(track.id, SRC, 0, T, T).do(project)
        assert spans(track) == [(0, 2 * T)]

    def test_an_empty_span_is_refused(self) -> None:
        project = make_project()
        with pytest.raises(CommandError):
            AddClipFromMedia(video(project).id, SRC, 0, 0, 0).do(project)

    def test_ticks_are_snapped_to_frames(self) -> None:
        project = make_project(FrameRate(30, 1))
        track = video(project)
        one_frame = T // 30
        AddClipFromMedia(track.id, SRC, 0, 2 * T, one_frame + 12).do(project)
        assert track.clips[0].timeline_start == one_frame

    def test_an_unknown_track_is_refused(self) -> None:
        project = make_project()
        with pytest.raises(CommandError, match="no such track"):
            AddClipFromMedia("nope", SRC, 0, T, 0).do(project)


class TestSplitClip:
    def test_it_cuts_in_two_at_the_position(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, 4 * T)
        SplitClip(track.id, clip.id, T).do(project)
        assert spans(track) == [(0, T), (T, 4 * T)]

    def test_the_source_span_is_split_at_the_same_offset(self) -> None:
        project = make_project()
        track = video(project)
        clip = Clip(src=SRC, src_in=10 * T, src_out=14 * T, timeline_start=0)
        track.clips.append(clip)
        SplitClip(track.id, clip.id, T).do(project)
        left, right = track.clips
        assert (left.src_in, left.src_out) == (10 * T, 11 * T)
        assert (right.src_in, right.src_out) == (11 * T, 14 * T)

    def test_the_left_half_keeps_the_original_id(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, 4 * T)
        SplitClip(track.id, clip.id, T).do(project)
        assert track.clips[0].id == clip.id

    def test_undo_restores_one_clip(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, 4 * T)
        command = SplitClip(track.id, clip.id, T)
        command.do(project)
        command.undo(project)
        assert spans(track) == [(0, 4 * T)]
        assert track.clips[0].src_out == 4 * T

    @pytest.mark.parametrize("at", [0, 4 * T, 5 * T])
    def test_a_cut_outside_the_clip_is_refused(self, at: int) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, 4 * T)
        with pytest.raises(CommandError, match="split point"):
            SplitClip(track.id, clip.id, at).do(project)
        assert spans(track) == [(0, 4 * T)]


class TestTrimClip:
    def test_the_right_edge_moves_src_out_alone(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, T, 4 * T)
        TrimClip(track.id, clip.id, 0, 2 * T, T).do(project)
        assert (clip.src_in, clip.src_out, clip.timeline_start) == (0, 2 * T, T)

    def test_the_left_edge_moves_src_in_and_start_together(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, 4 * T)
        TrimClip(track.id, clip.id, T, 4 * T, T).do(project)
        assert clip.timeline_start == T and clip.duration == 3 * T

    def test_undo_restores_all_three_values(self) -> None:
        project = make_project()
        track = video(project)
        clip = Clip(src=SRC, src_in=T, src_out=5 * T, timeline_start=2 * T)
        track.clips.append(clip)
        command = TrimClip(track.id, clip.id, 2 * T, 3 * T, 3 * T)
        command.do(project)
        command.undo(project)
        assert (clip.src_in, clip.src_out, clip.timeline_start) == (T, 5 * T, 2 * T)

    def test_trimming_to_nothing_is_refused(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, 4 * T)
        with pytest.raises(CommandError, match="trimmed to nothing"):
            TrimClip(track.id, clip.id, 2 * T, 2 * T, 0).do(project)

    def test_trimming_over_a_neighbour_is_refused(self) -> None:
        project = make_project()
        track = video(project)
        first = place(track, 0, 2 * T)
        place(track, 2 * T, 2 * T)
        with pytest.raises(CommandError, match="overlap"):
            TrimClip(track.id, first.id, 0, 3 * T, 0).do(project)
        assert spans(track) == [(0, 2 * T), (2 * T, 4 * T)]

    def test_a_negative_source_in_point_is_refused(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 2 * T, 2 * T)
        with pytest.raises(CommandError):
            TrimClip(track.id, clip.id, -T, 2 * T, T).do(project)


class TestMoveClip:
    def test_it_moves_along_the_track(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, T)
        MoveClip(clip.id, track.id, track.id, 5 * T).do(project)
        assert spans(track) == [(5 * T, 6 * T)]

    def test_it_moves_between_tracks_of_the_same_kind(self) -> None:
        project = make_project()
        first = audio(project)
        second = Track(name="A2", kind="audio")
        project.tracks.append(second)
        clip = place(first, 0, T)
        MoveClip(clip.id, first.id, second.id, 2 * T).do(project)
        assert first.clips == []
        assert spans(second) == [(2 * T, 3 * T)]

    def test_undo_puts_it_back_on_its_own_track(self) -> None:
        project = make_project()
        first = audio(project)
        second = Track(name="A2", kind="audio")
        project.tracks.append(second)
        clip = place(first, 3 * T, T)
        command = MoveClip(clip.id, first.id, second.id, 0)
        command.do(project)
        command.undo(project)
        assert spans(first) == [(3 * T, 4 * T)]
        assert second.clips == []

    def test_crossing_kinds_is_refused(self) -> None:
        project = make_project()
        source, target = video(project), audio(project)
        clip = place(source, 0, T)
        with pytest.raises(CommandError, match="video clip onto the audio"):
            MoveClip(clip.id, source.id, target.id, 0).do(project)
        assert spans(source) == [(0, T)]
        assert target.clips == []

    def test_an_overlapping_destination_is_refused(self) -> None:
        project = make_project()
        track = video(project)
        first = place(track, 0, 2 * T)
        place(track, 4 * T, 2 * T)
        with pytest.raises(CommandError, match="overlap"):
            MoveClip(first.id, track.id, track.id, 3 * T).do(project)
        assert spans(track) == [(0, 2 * T), (4 * T, 6 * T)]

    def test_a_clip_can_move_onto_its_own_position(self) -> None:
        """It must not collide with itself."""
        project = make_project()
        track = video(project)
        clip = place(track, 2 * T, T)
        MoveClip(clip.id, track.id, track.id, 2 * T).do(project)
        assert spans(track) == [(2 * T, 3 * T)]

    def test_the_track_stays_sorted(self) -> None:
        project = make_project()
        track = video(project)
        first = place(track, 0, T)
        place(track, 5 * T, T)
        MoveClip(first.id, track.id, track.id, 8 * T).do(project)
        assert spans(track) == [(5 * T, 6 * T), (8 * T, 9 * T)]


class TestDeleteClip:
    def test_undo_restores_the_whole_clip(self) -> None:
        project = make_project()
        track = audio(project)
        clip = Clip(src=SRC, src_in=T, src_out=3 * T, timeline_start=7 * T, gain_db=-6.0)
        track.clips.append(clip)
        command = DeleteClip(track.id, clip.id)
        command.do(project)
        assert track.clips == []
        command.undo(project)
        restored = track.clips[0]
        assert restored.id == clip.id
        assert restored.gain_db == -6.0
        assert (restored.src_in, restored.src_out) == (T, 3 * T)

    def test_deleting_something_that_is_not_there_is_refused(self) -> None:
        project = make_project()
        with pytest.raises(CommandError, match="no such clip"):
            DeleteClip(video(project).id, "nope").do(project)


class TestDuplicateClip:
    def test_the_copy_lands_immediately_after_the_original(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, T, 2 * T)
        DuplicateClip(track.id, clip.id).do(project)
        assert spans(track) == [(T, 3 * T), (3 * T, 5 * T)]

    def test_the_copy_carries_the_same_source_span_and_gain(self) -> None:
        project = make_project()
        track = audio(project)
        clip = Clip(src=SRC, src_in=T, src_out=2 * T, timeline_start=0, gain_db=3.5)
        track.clips.append(clip)
        DuplicateClip(track.id, clip.id).do(project)
        copy = track.clips[1]
        assert (copy.src_in, copy.src_out, copy.gain_db) == (T, 2 * T, 3.5)
        assert copy.id != clip.id

    def test_it_moves_past_a_neighbour_rather_than_refusing(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, 2 * T)
        place(track, 2 * T, T)
        DuplicateClip(track.id, clip.id).do(project)
        assert spans(track) == [(0, 2 * T), (2 * T, 3 * T), (3 * T, 5 * T)]

    def test_undo_removes_only_the_copy(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, T)
        command = DuplicateClip(track.id, clip.id)
        command.do(project)
        command.undo(project)
        assert [c.id for c in track.clips] == [clip.id]

    def test_redo_puts_the_copy_back_where_it_was(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, 2 * T)
        place(track, 2 * T, T)
        command = DuplicateClip(track.id, clip.id)
        command.do(project)
        placed = [c.timeline_start for c in track.clips]
        command.undo(project)
        command.do(project)
        assert [c.timeline_start for c in track.clips] == placed


class TestSetClipGain:
    def test_it_sets_and_restores(self) -> None:
        project = make_project()
        track = audio(project)
        clip = place(track, 0, T)
        command = SetClipGain(track.id, clip.id, -3.0)
        command.do(project)
        assert clip.gain_db == -3.0
        command.undo(project)
        assert clip.gain_db == 0.0


class TestTrackCommands:
    def test_set_track_muted_round_trips(self) -> None:
        project = make_project()
        track = audio(project)
        command = SetTrackMuted(track.id, True)
        command.do(project)
        assert track.muted is True
        command.undo(project)
        assert track.muted is False

    def test_the_label_says_which_way(self) -> None:
        assert SetTrackMuted("t", True).label == "Mute track"
        assert SetTrackMuted("t", False).label == "Unmute track"

    def test_add_track_appends_and_undo_removes(self) -> None:
        project = make_project()
        command = AddTrack("audio", "A2")
        command.do(project)
        assert [t.name for t in project.tracks] == ["V1", "A1", "A2"]
        command.undo(project)
        assert [t.name for t in project.tracks] == ["V1", "A1"]

    def test_a_second_video_track_is_refused(self) -> None:
        """v1 has no compositing; the refusal belongs here, not at export."""
        project = make_project()
        with pytest.raises(CommandError, match="compositing"):
            AddTrack("video", "V2").do(project)
        assert len(project.video_tracks()) == 1

    def test_a_video_track_is_allowed_when_there_is_none(self) -> None:
        project = Project(name="Empty")
        AddTrack("video", "V1").do(project)
        assert len(project.video_tracks()) == 1

    def test_audio_tracks_are_unlimited(self) -> None:
        project = make_project()
        for index in range(2, 8):
            AddTrack("audio", f"A{index}").do(project)
        assert len(project.audio_tracks()) == 7

    def test_redo_restores_the_same_track_id(self) -> None:
        project = make_project()
        command = AddTrack("audio", "A2")
        command.do(project)
        track_id = project.tracks[-1].id
        command.undo(project)
        command.do(project)
        assert project.tracks[-1].id == track_id

    def test_remove_track_restores_its_index(self) -> None:
        project = make_project()
        project.tracks.append(Track(name="A2", kind="audio"))
        command = RemoveTrack(project.tracks[1].id)
        command.do(project)
        assert [t.name for t in project.tracks] == ["V1", "A2"]
        command.undo(project)
        assert [t.name for t in project.tracks] == ["V1", "A1", "A2"]

    def test_remove_track_keeps_its_clips(self) -> None:
        project = make_project()
        track = audio(project)
        place(track, 0, T)
        command = RemoveTrack(track.id)
        command.do(project)
        command.undo(project)
        assert len(project.audio_tracks()[0].clips) == 1


class TestTouchesAudio:
    """Drives audio bed invalidation, so a wrong answer is a silent bug."""

    def test_an_audio_track_edit_touches_audio(self) -> None:
        project = make_project()
        track = audio(project)
        clip = place(track, 0, T)
        command = MoveClip(clip.id, track.id, track.id, 2 * T)
        command.do(project)
        assert command.touches_audio is True

    def test_a_video_track_edit_does_not(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, T)
        command = MoveClip(clip.id, track.id, track.id, 2 * T)
        command.do(project)
        assert command.touches_audio is False

    def test_add_track_knows_without_running(self) -> None:
        assert AddTrack("audio", "A2").touches_audio is True
        assert AddTrack("video", "V1").touches_audio is False

    def test_muting_a_video_track_does_not_touch_the_bed(self) -> None:
        """The bed is mixed from audio tracks only, so this cannot change it."""
        project = make_project()
        command = SetTrackMuted(video(project).id, True)
        command.do(project)
        assert command.touches_audio is False

    @pytest.mark.parametrize("kind", ["video", "audio"])
    @pytest.mark.parametrize(
        "build",
        [
            lambda t, c: SplitClip(t.id, c.id, 2 * T),
            lambda t, c: DeleteClip(t.id, c.id),
            lambda t, c: DuplicateClip(t.id, c.id),
            lambda t, c: TrimClip(t.id, c.id, 0, 3 * T, c.timeline_start),
            lambda t, c: SetClipGain(t.id, c.id, -3.0),
        ],
    )
    def test_every_clip_command_answers_for_its_track(self, kind: str, build) -> None:
        project = make_project()
        track = video(project) if kind == "video" else audio(project)
        clip = place(track, 0, 4 * T)
        command = build(track, clip)
        command.do(project)
        assert command.touches_audio is (kind == "audio")


class TestMacroCommand:
    def test_it_applies_every_part(self) -> None:
        project = make_project()
        track = video(project)
        first = place(track, 0, T)
        second = place(track, 2 * T, T)
        MacroCommand(
            "Delete 2 clips",
            [DeleteClip(track.id, first.id), DeleteClip(track.id, second.id)],
        ).do(project)
        assert track.clips == []

    def test_one_undo_reverses_all_of_them(self) -> None:
        project = make_project()
        track = video(project)
        clips = [place(track, 0, T), place(track, 2 * T, T), place(track, 4 * T, T)]
        command = MacroCommand(
            "Delete 3 clips", [DeleteClip(track.id, c.id) for c in clips]
        )
        command.do(project)
        command.undo(project)
        assert spans(track) == [(0, T), (2 * T, 3 * T), (4 * T, 5 * T)]

    def test_a_failing_part_rolls_the_others_back(self) -> None:
        project = make_project()
        track = video(project)
        clip = place(track, 0, T)
        command = MacroCommand(
            "Two things",
            [DeleteClip(track.id, clip.id), DeleteClip(track.id, "missing")],
        )
        with pytest.raises(CommandError):
            command.do(project)
        assert spans(track) == [(0, T)]

    def test_it_touches_audio_when_any_part_does(self) -> None:
        project = make_project()
        video_clip = place(video(project), 0, T)
        audio_clip = place(audio(project), 0, T)
        command = MacroCommand(
            "Delete 2 clips",
            [
                DeleteClip(video(project).id, video_clip.id),
                DeleteClip(audio(project).id, audio_clip.id),
            ],
        )
        command.do(project)
        assert command.touches_audio is True

    def test_an_empty_macro_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            MacroCommand("Nothing", [])


class TestCommandStack:
    def test_push_runs_the_command(self) -> None:
        project = make_project()
        stack = CommandStack()
        stack.push(AddTrack("audio", "A2"), project)
        assert len(project.tracks) == 3

    def test_labels_follow_the_top_of_each_stack(self) -> None:
        project = make_project()
        stack = CommandStack()
        assert not stack.can_undo() and stack.undo_label() == ""
        stack.push(AddTrack("audio", "A2"), project)
        assert stack.undo_label() == "Add track"
        assert not stack.can_redo()
        stack.undo(project)
        assert stack.redo_label() == "Add track"
        assert not stack.can_undo()

    def test_a_refused_command_is_not_recorded(self) -> None:
        project = make_project()
        stack = CommandStack()
        with pytest.raises(CommandError):
            stack.push(AddTrack("video", "V2"), project)
        assert not stack.can_undo()

    def test_a_new_edit_discards_the_redo_branch(self) -> None:
        project = make_project()
        stack = CommandStack()
        stack.push(AddTrack("audio", "A2"), project)
        stack.undo(project)
        assert stack.can_redo()
        stack.push(AddTrack("audio", "A3"), project)
        assert not stack.can_redo()

    def test_undo_and_redo_on_an_empty_stack_return_none(self) -> None:
        project = make_project()
        stack = CommandStack()
        assert stack.undo(project) is None
        assert stack.redo(project) is None

    def test_the_history_is_bounded(self) -> None:
        project = make_project()
        stack = CommandStack(limit=3)
        for index in range(6):
            stack.push(AddTrack("audio", f"A{index}"), project)
        assert stack.depth() == 3

    def test_clear_empties_both_directions(self) -> None:
        project = make_project()
        stack = CommandStack()
        stack.push(AddTrack("audio", "A2"), project)
        stack.undo(project)
        stack.clear()
        assert not stack.can_undo() and not stack.can_redo()


class TestFullSession:
    """A whole editing session against the commands alone, without the mouse."""

    def test_a_whole_edit_session_undoes_back_to_empty(self) -> None:
        project = make_project()
        track, bed = video(project), audio(project)
        stack = CommandStack()

        stack.push(AddClipFromMedia(track.id, SRC, 0, 4 * T, 0), project)
        first = track.clips[0].id
        stack.push(AddClipFromMedia(track.id, SRC, 0, 4 * T, 6 * T), project)
        second = track.clips[1].id
        stack.push(AddClipFromMedia(bed.id, Path("music.wav"), 0, 8 * T, 0), project)
        music = bed.clips[0].id

        stack.push(TrimClip(track.id, first, 0, 3 * T, 0), project)
        stack.push(TrimClip(track.id, second, T, 4 * T, 7 * T), project)
        stack.push(SplitClip(track.id, first, T), project)
        stack.push(DuplicateClip(track.id, first), project)
        stack.push(MoveClip(music, bed.id, bed.id, 2 * T), project)

        assert len(track.clips) == 4
        assert project.duration > 0

        while stack.can_undo():
            stack.undo(project)

        assert track.clips == [] and bed.clips == []
        assert project.duration == 0

    def test_redoing_the_whole_session_reproduces_it(self) -> None:
        project = make_project()
        track = video(project)
        stack = CommandStack()
        stack.push(AddClipFromMedia(track.id, SRC, 0, 4 * T, 0), project)
        clip = track.clips[0].id
        stack.push(SplitClip(track.id, clip, 2 * T), project)
        stack.push(DuplicateClip(track.id, clip), project)
        after = spans(track)

        while stack.can_undo():
            stack.undo(project)
        while stack.can_redo():
            stack.redo(project)

        assert spans(track) == after

    def test_no_sequence_of_commands_leaves_an_overlap(self) -> None:
        project = make_project()
        track = video(project)
        stack = CommandStack()
        stack.push(AddClipFromMedia(track.id, SRC, 0, 2 * T, 0), project)
        stack.push(AddClipFromMedia(track.id, SRC, 0, 2 * T, 2 * T), project)
        first, second = (c.id for c in track.clips)

        for attempt in (
            MoveClip(second, track.id, track.id, T),
            TrimClip(track.id, first, 0, 3 * T, 0),
            AddClipFromMedia(track.id, SRC, 0, T, T),
        ):
            with pytest.raises(CommandError):
                stack.push(attempt, project)

        # Still exactly two clips, still in order, still not touching.
        assert spans(track) == [(0, 2 * T), (2 * T, 4 * T)]
        # And the model itself agrees, which it checks on validation.
        Track.model_validate(track.model_dump())


class TestFrameSnappingAtAVariableRate:
    """The rate a real VFR file probes to, where a frame is not a whole tick."""

    RATE = FrameRate(24249, 1000)

    def test_a_placed_clip_lands_on_a_frame_boundary(self) -> None:
        project = make_project(self.RATE)
        track = video(project)
        AddClipFromMedia(track.id, SRC, 0, 2 * T, 12345).do(project)
        clip = track.clips[0]
        assert clip.timeline_start == snap_to_frame(12345, self.RATE)

    def test_snapping_is_idempotent_across_a_redo(self) -> None:
        project = make_project(self.RATE)
        track = video(project)
        command = AddClipFromMedia(track.id, SRC, 0, 2 * T, 12345)
        command.do(project)
        first = track.clips[0].timeline_start
        command.undo(project)
        command.do(project)
        assert track.clips[0].timeline_start == first


class TestCommandContract:
    def test_every_command_is_abstract_about_both_directions(self) -> None:
        assert Command.do.__isabstractmethod__
        assert Command.undo.__isabstractmethod__

    def test_every_concrete_command_has_a_label(self) -> None:
        for cls in (
            SplitClip,
            TrimClip,
            MoveClip,
            DeleteClip,
            DuplicateClip,
            SetClipGain,
            SetTrackMuted,
            AddClipFromMedia,
            AddTrack,
            RemoveTrack,
        ):
            assert cls.label and cls.label != Command.label, cls.__name__


class TestRelinkMedia:
    """Addressed by clip id, and by nothing else."""

    def test_it_repoints_one_clip(self) -> None:
        project = make_project()
        clip = place(video(project), 0, T)

        RelinkMedia({clip.id: Path("new.mp4")}).do(project)

        assert clip.src == Path("new.mp4")

    def test_undo_puts_the_old_path_back(self) -> None:
        project = make_project()
        clip = place(video(project), 0, T, src=Path("old.mp4"))
        command = RelinkMedia({clip.id: Path("new.mp4")})

        command.do(project)
        command.undo(project)

        assert clip.src == Path("old.mp4")

    def test_several_clips_are_repointed_together(self) -> None:
        project = make_project()
        first = place(video(project), 0, T)
        second = place(video(project), 2 * T, T)

        RelinkMedia(
            {first.id: Path("a.mp4"), second.id: Path("b.mp4")}
        ).do(project)

        assert (first.src, second.src) == (Path("a.mp4"), Path("b.mp4"))

    def test_a_clip_on_any_track_is_found_by_id_alone(self) -> None:
        """No track id is given, and none is needed."""
        project = make_project()
        audio_clip = place(audio(project), 0, T)

        RelinkMedia({audio_clip.id: Path("new.wav")}).do(project)

        assert audio_clip.src == Path("new.wav")

    def test_track_order_does_not_affect_which_clip_is_repaired(self) -> None:
        """Model order and lane order can disagree.

        This project's tracks are ['A1', 'V1'], which is what removing and
        re-adding the video track leaves. Anything counting positions would
        relink the audio clip with the video file, and both are real clips so
        nothing would raise.
        """
        project = Project(name="inverted", tracks=[])
        audio_track = Track(name="A1", kind="audio")
        video_track = Track(name="V1", kind="video")
        project.tracks = [audio_track, video_track]
        audio_clip = place(audio_track, 0, T, src=Path("music.wav"))
        video_clip = place(video_track, 0, T, src=Path("shot.mp4"))

        RelinkMedia({video_clip.id: Path("new_shot.mp4")}).do(project)

        assert video_clip.src == Path("new_shot.mp4")
        assert audio_clip.src == Path("music.wav")

    def test_an_unknown_id_refuses_and_changes_nothing(self) -> None:
        project = make_project()
        clip = place(video(project), 0, T, src=Path("old.mp4"))

        with pytest.raises(CommandError):
            RelinkMedia({clip.id: Path("new.mp4"), "nope": Path("x.mp4")}).do(project)

        assert clip.src == Path("old.mp4"), "a refused command changed the project"

    def test_relinking_nothing_is_refused(self) -> None:
        with pytest.raises(CommandError):
            RelinkMedia({}).do(make_project())

    def test_a_video_relink_does_not_touch_the_audio_bed(self) -> None:
        project = make_project()
        clip = place(video(project), 0, T)
        command = RelinkMedia({clip.id: Path("new.mp4")})
        command.do(project)
        assert command.touches_audio is False

    def test_an_audio_relink_does(self) -> None:
        project = make_project()
        clip = place(audio(project), 0, T)
        command = RelinkMedia({clip.id: Path("new.wav")})
        command.do(project)
        assert command.touches_audio is True

    def test_a_mixed_relink_touches_audio(self) -> None:
        project = make_project()
        video_clip = place(video(project), 0, T)
        audio_clip = place(audio(project), 0, T)
        command = RelinkMedia(
            {video_clip.id: Path("v.mp4"), audio_clip.id: Path("a.wav")}
        )
        command.do(project)
        assert command.touches_audio is True

    def test_redo_reproduces_the_same_result(self) -> None:
        project = make_project()
        clip = place(video(project), 0, T, src=Path("old.mp4"))
        stack = CommandStack()

        stack.push(RelinkMedia({clip.id: Path("new.mp4")}), project)
        stack.undo(project)
        stack.redo(project)

        assert clip.src == Path("new.mp4")

    def test_a_string_path_is_accepted_and_stored_as_a_path(self) -> None:
        project = make_project()
        clip = place(video(project), 0, T)
        RelinkMedia({clip.id: "new.mp4"}).do(project)
        assert clip.src == Path("new.mp4")

    def test_it_does_not_check_whether_the_new_file_exists(self) -> None:
        """Relinking to something that is also missing is not an error.

        The clips stay unresolved, which is the state they were already in,
        and the user can relink again.
        """
        project = make_project()
        clip = place(video(project), 0, T)
        RelinkMedia({clip.id: Path("C:/nowhere/still_gone.mp4")}).do(project)
        assert clip.src == Path("C:/nowhere/still_gone.mp4")
