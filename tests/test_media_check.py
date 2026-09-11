"""Finding missing media, and repairing it without ever counting positions.

The heart of this file is TestNothingIsAddressedByPosition. Model order and
lane order can diverge: removing and re-adding the video track leaves
``project.tracks`` as ['A1', 'V1'] while the timeline still draws video on
top. Anything that relinked "the first track's second clip" would then
silently repair the wrong clip, and both clips are real so nothing raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.media_check import (
    clip_by_id,
    has_missing_media,
    iter_clips,
    missing_clip_ids,
    missing_sources,
    relink_map,
    search_folder,
    without_missing_clips,
)
from core.model import Clip, Project, Track
from core.timebase import TICKS_PER_SECOND as T

HERE = Path(__file__).resolve()
GONE = Path("C:/nowhere/gone.mp4")
ALSO_GONE = Path("C:/nowhere/also_gone.wav")


def clip(src: Path, start: int = 0, length: int = T) -> Clip:
    return Clip(src=src, src_in=0, src_out=length, timeline_start=start)


def project_with(*tracks: Track) -> Project:
    return Project(name="p", tracks=list(tracks))


def present(path: Path) -> bool:
    """A stub filesystem: only this test file exists."""
    return Path(path) == HERE


class TestFindingWhatIsGone:
    def test_a_project_whose_media_is_present_has_nothing_missing(self) -> None:
        p = project_with(Track(name="V1", kind="video", clips=[clip(HERE)]))
        assert missing_sources(p, exists=present) == []
        assert has_missing_media(p, exists=present) is False

    def test_a_missing_file_is_reported_once_with_all_its_clips(self) -> None:
        first, second = clip(GONE, 0), clip(GONE, 2 * T)
        p = project_with(Track(name="V1", kind="video", clips=[first, second]))

        missing = missing_sources(p, exists=present)

        assert len(missing) == 1, "one file, not one entry per clip"
        assert missing[0].src == GONE
        assert set(missing[0].clip_ids) == {first.id, second.id}

    def test_the_name_is_the_filename(self) -> None:
        p = project_with(Track(name="V1", kind="video", clips=[clip(GONE)]))
        assert missing_sources(p, exists=present)[0].name == "gone.mp4"

    def test_files_come_back_in_first_use_order(self) -> None:
        p = project_with(
            Track(name="V1", kind="video", clips=[clip(GONE)]),
            Track(name="A1", kind="audio", clips=[clip(ALSO_GONE)]),
        )
        assert [m.src for m in missing_sources(p, exists=present)] == [GONE, ALSO_GONE]

    def test_each_file_is_tested_once_however_many_clips_use_it(self) -> None:
        calls: list[Path] = []

        def counting(path: Path) -> bool:
            calls.append(Path(path))
            return False

        p = project_with(
            Track(
                name="V1",
                kind="video",
                clips=[clip(GONE, 0), clip(GONE, 2 * T), clip(GONE, 4 * T)],
            )
        )
        missing_sources(p, exists=counting)
        assert len(calls) == 1

    def test_missing_clip_ids_is_every_clip_of_every_missing_file(self) -> None:
        a, b, c = clip(GONE, 0), clip(HERE, 2 * T), clip(GONE, 4 * T)
        p = project_with(Track(name="V1", kind="video", clips=[a, b, c]))
        assert missing_clip_ids(p, exists=present) == frozenset({a.id, c.id})

    def test_none_and_empty_are_answered_not_raised(self) -> None:
        assert missing_sources(None) == []
        assert missing_clip_ids(None) == frozenset()
        assert has_missing_media(Project(name="empty")) is False

    def test_the_real_filesystem_is_the_default(self, tmp_path: Path) -> None:
        real = tmp_path / "there.mp4"
        real.write_bytes(b"x")
        p = project_with(
            Track(name="V1", kind="video", clips=[clip(real), clip(GONE, 2 * T)])
        )
        assert [m.src for m in missing_sources(p)] == [GONE]


class TestNothingIsAddressedByPosition:
    """Ids, never indices."""

    @staticmethod
    def reordered() -> tuple[Project, Clip, Clip]:
        """Audio first in the model, video second.

        This is what a project looks like after the video track has been
        removed and re-added, and the timeline still draws V1 on top.
        """
        video_clip = clip(GONE, 0)
        audio_clip = clip(ALSO_GONE, 0)
        project = project_with(
            Track(name="A1", kind="audio", clips=[audio_clip]),
            Track(name="V1", kind="video", clips=[video_clip]),
        )
        return project, video_clip, audio_clip

    def test_the_fixture_really_is_out_of_order(self) -> None:
        project, _video, _audio = self.reordered()
        assert [t.name for t in project.tracks] == ["A1", "V1"]
        assert project.video_tracks()[0].name == "V1"

    def test_relink_map_pairs_each_clip_with_its_own_file(self) -> None:
        project, video_clip, audio_clip = self.reordered()

        mapping = relink_map(
            project, {GONE: Path("new_video.mp4"), ALSO_GONE: Path("new_audio.wav")}
        )

        assert mapping == {
            video_clip.id: Path("new_video.mp4"),
            audio_clip.id: Path("new_audio.wav"),
        }

    def test_a_clip_is_found_wherever_its_track_sits(self) -> None:
        project, video_clip, _audio = self.reordered()
        found = clip_by_id(project, video_clip.id)
        assert found is not None
        track, clip_found = found
        assert track.kind == "video"
        assert clip_found is video_clip

    def test_an_unknown_id_is_none_not_a_guess(self) -> None:
        project, _v, _a = self.reordered()
        assert clip_by_id(project, "nope") is None

    def test_iter_clips_carries_the_track_with_each_clip(self) -> None:
        project, video_clip, audio_clip = self.reordered()
        pairs = {clip.id: track.kind for track, clip in iter_clips(project)}
        assert pairs == {video_clip.id: "video", audio_clip.id: "audio"}


class TestRelinkMap:
    def test_only_the_files_that_were_answered_appear(self) -> None:
        video_clip, audio_clip = clip(GONE), clip(ALSO_GONE)
        p = project_with(
            Track(name="V1", kind="video", clips=[video_clip]),
            Track(name="A1", kind="audio", clips=[audio_clip]),
        )
        assert relink_map(p, {GONE: Path("x.mp4")}) == {video_clip.id: Path("x.mp4")}

    def test_every_clip_of_one_file_is_relinked_from_one_answer(self) -> None:
        first, second = clip(GONE, 0), clip(GONE, 2 * T)
        p = project_with(Track(name="V1", kind="video", clips=[first, second]))
        mapping = relink_map(p, {GONE: Path("x.mp4")})
        assert set(mapping) == {first.id, second.id}

    def test_matching_ignores_case_the_way_windows_does(self) -> None:
        the_clip = clip(Path("C:/Media/Shot.MP4"))
        p = project_with(Track(name="V1", kind="video", clips=[the_clip]))
        mapping = relink_map(p, {Path("c:/media/shot.mp4"): Path("new.mp4")})
        assert mapping == {the_clip.id: Path("new.mp4")}

    def test_nothing_answered_is_an_empty_map(self) -> None:
        p = project_with(Track(name="V1", kind="video", clips=[clip(GONE)]))
        assert relink_map(p, {}) == {}


class TestSearchFolder:
    def test_it_finds_a_file_nested_several_levels_down(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "shot.mp4").write_bytes(b"x")

        found = search_folder(tmp_path, ["shot.mp4"])

        assert found == {"shot.mp4": deep / "shot.mp4"}

    def test_it_finds_several_at_once(self, tmp_path: Path) -> None:
        (tmp_path / "one.mp4").write_bytes(b"x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "two.wav").write_bytes(b"x")

        found = search_folder(tmp_path, ["one.mp4", "two.wav"])

        assert set(found) == {"one.mp4", "two.wav"}

    def test_a_name_that_is_not_there_is_simply_absent(self, tmp_path: Path) -> None:
        (tmp_path / "one.mp4").write_bytes(b"x")
        assert search_folder(tmp_path, ["missing.mp4"]) == {}

    def test_capitalisation_does_not_hide_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "SHOT.MP4").write_bytes(b"x")
        # A copy off a share can come back differently capitalised. Refusing
        # to find it would be pedantry.
        assert search_folder(tmp_path, ["shot.mp4"]) == {
            "shot.mp4": tmp_path / "SHOT.MP4"
        }

    def test_the_walk_is_bounded(self, tmp_path: Path) -> None:
        for index in range(20):
            (tmp_path / f"f{index}.bin").write_bytes(b"x")
        (tmp_path / "wanted.mp4").write_bytes(b"x")
        # Pointing this at the root of a drive should give up, not run for a
        # quarter of an hour.
        assert search_folder(tmp_path, ["wanted.mp4"], max_entries=3) in (
            {},
            {"wanted.mp4": tmp_path / "wanted.mp4"},
        )


class TestWithoutMissingClips:
    def test_missing_clips_are_dropped_and_named(self) -> None:
        p = project_with(
            Track(name="V1", kind="video", clips=[clip(HERE, 0), clip(GONE, 2 * T)])
        )

        trimmed, skipped = without_missing_clips(p, exists=present)

        assert [c.src for c in trimmed.tracks[0].clips] == [HERE]
        assert skipped == ["gone.mp4"]

    def test_the_original_project_is_untouched(self) -> None:
        p = project_with(Track(name="V1", kind="video", clips=[clip(GONE)]))

        trimmed, _skipped = without_missing_clips(p, exists=present)
        trimmed.name = "changed"

        assert len(p.tracks[0].clips) == 1, "the caller's project was modified"
        assert p.name == "p"

    def test_a_project_with_nothing_missing_comes_back_whole(self) -> None:
        p = project_with(Track(name="V1", kind="video", clips=[clip(HERE)]))
        trimmed, skipped = without_missing_clips(p, exists=present)
        assert skipped == []
        assert len(trimmed.tracks[0].clips) == 1

    def test_every_clip_missing_leaves_the_tracks_but_no_clips(self) -> None:
        p = project_with(
            Track(name="V1", kind="video", clips=[clip(GONE)]),
            Track(name="A1", kind="audio", clips=[clip(ALSO_GONE)]),
        )
        trimmed, skipped = without_missing_clips(p, exists=present)
        assert [t.name for t in trimmed.tracks] == ["V1", "A1"]
        assert all(not t.clips for t in trimmed.tracks)
        assert len(skipped) == 2

    def test_one_name_per_skipped_clip_not_per_file(self) -> None:
        p = project_with(
            Track(name="V1", kind="video", clips=[clip(GONE, 0), clip(GONE, 2 * T)])
        )
        _trimmed, skipped = without_missing_clips(p, exists=present)
        assert skipped == ["gone.mp4", "gone.mp4"]


class TestCoreStaysPure:
    def test_this_module_imports_no_qt(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent / "core" / "media_check.py"
        ).read_text(encoding="utf-8")
        assert "PySide6" not in source
        assert "from ui" not in source


@pytest.mark.parametrize("value", [None, Project(name="empty")])
def test_without_missing_clips_needs_a_project(value) -> None:
    if value is None:
        with pytest.raises(AttributeError):
            without_missing_clips(value)
    else:
        trimmed, skipped = without_missing_clips(value)
        assert skipped == []
        assert trimmed.tracks == []
