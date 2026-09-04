"""Save/load tests: nothing may change value across a round trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.model import Clip, Project, Track
from core.project_io import SCHEMA_VERSION, ProjectIOError, load, save
from core.timebase import FrameRate


def build_project(media_root: Path) -> Project:
    return Project(
        name="Round Trip",
        width=1280,
        height=720,
        frame_rate=FrameRate(30000, 1001),
        sample_rate=44100,
        tracks=[
            Track(
                id="track-v1",
                name="V1",
                kind="video",
                clips=[
                    Clip(
                        id="clip-a",
                        src=media_root / "a.mp4",
                        src_in=5005,
                        src_out=120120,
                        timeline_start=0,
                    ),
                    Clip(
                        id="clip-b",
                        src=media_root / "sub" / "b.mov",
                        src_in=0,
                        src_out=60060,
                        timeline_start=240240,
                        gain_db=-3.5,
                    ),
                ],
            ),
            Track(
                id="track-a1",
                name="A1",
                kind="audio",
                muted=True,
                clips=[
                    Clip(
                        id="clip-c",
                        src=media_root / "a.mp4",
                        src_in=1000,
                        src_out=2000,
                        timeline_start=10,
                        gain_db=6.0,
                    )
                ],
            ),
        ],
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "media" / "sub").mkdir(parents=True)
    return root


def test_round_trip_preserves_every_field(project_dir: Path) -> None:
    original = build_project(project_dir / "media")
    path = project_dir / "edit.vidproj"
    save(original, path)
    restored = load(path)

    assert restored.name == original.name
    assert restored.width == original.width
    assert restored.height == original.height
    assert restored.sample_rate == original.sample_rate
    assert restored.frame_rate == original.frame_rate
    assert restored.frame_rate.num == 30000 and restored.frame_rate.den == 1001
    assert len(restored.tracks) == len(original.tracks)

    for got, want in zip(restored.tracks, original.tracks):
        assert got.id == want.id
        assert got.name == want.name
        assert got.kind == want.kind
        assert got.muted == want.muted
        assert len(got.clips) == len(want.clips)
        for got_clip, want_clip in zip(got.clips, want.clips):
            assert got_clip.id == want_clip.id
            assert got_clip.src_in == want_clip.src_in
            assert got_clip.src_out == want_clip.src_out
            assert got_clip.timeline_start == want_clip.timeline_start
            assert got_clip.gain_db == want_clip.gain_db
            assert got_clip.src == want_clip.src.resolve()


def test_round_trip_of_an_empty_project(tmp_path: Path) -> None:
    path = tmp_path / "empty.vidproj"
    save(Project(name="Empty"), path)
    restored = load(path)
    assert restored.name == "Empty"
    assert restored.tracks == []
    assert restored.duration == 0


def test_file_layout(project_dir: Path) -> None:
    path = project_dir / "edit.vidproj"
    save(build_project(project_dir / "media"), path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION == 2
    assert raw["project"]["frame_rate"] == {"num": 30000, "den": 1001}


def test_paths_under_the_project_tree_are_stored_relative(project_dir: Path) -> None:
    path = project_dir / "edit.vidproj"
    save(build_project(project_dir / "media"), path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    stored = [c["src"] for t in raw["project"]["tracks"] for c in t["clips"]]
    assert sorted(set(stored)) == ["media/a.mp4", "media/sub/b.mov"]
    for value in stored:
        assert not Path(value).is_absolute()
        assert "\\" not in value


def test_paths_outside_the_project_tree_stay_absolute(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    outside = tmp_path / "elsewhere" / "far.mp4"
    outside.parent.mkdir()
    project = Project(
        name="p",
        tracks=[
            Track(
                name="V1",
                kind="video",
                clips=[Clip(src=outside, src_in=0, src_out=100, timeline_start=0)],
            )
        ],
    )
    path = project_dir / "edit.vidproj"
    save(project, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    stored = raw["project"]["tracks"][0]["clips"][0]["src"]
    assert Path(stored).is_absolute()
    assert load(path).tracks[0].clips[0].src == outside.resolve()


def test_moving_the_project_folder_keeps_relative_media_working(
    project_dir: Path,
) -> None:
    path = project_dir / "edit.vidproj"
    save(build_project(project_dir / "media"), path)

    moved_root = project_dir.parent / "moved"
    project_dir.rename(moved_root)
    restored = load(moved_root / "edit.vidproj")

    resolved = restored.tracks[0].clips[0].src
    assert resolved == (moved_root / "media" / "a.mp4").resolve()


def test_validation_still_runs_on_load(tmp_path: Path) -> None:
    path = tmp_path / "bad.vidproj"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "project": {
                    "name": "p",
                    "tracks": [
                        {
                            "id": "t",
                            "name": "V1",
                            "kind": "video",
                            "muted": False,
                            "clips": [
                                {
                                    "id": "a",
                                    "src": "a.mp4",
                                    "src_in": 0,
                                    "src_out": 1000,
                                    "timeline_start": 0,
                                    "gain_db": 0.0,
                                },
                                {
                                    "id": "b",
                                    "src": "a.mp4",
                                    "src_in": 0,
                                    "src_out": 1000,
                                    "timeline_start": 500,
                                    "gain_db": 0.0,
                                },
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="overlap"):
        load(path)


def test_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.vidproj"
    path.write_text(
        json.dumps({"schema_version": 99, "project": {"name": "p"}}), encoding="utf-8"
    )
    with pytest.raises(ProjectIOError, match="schema_version"):
        load(path)


def test_malformed_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "junk.vidproj"
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ProjectIOError):
        load(path)


def test_missing_project_object_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "hollow.vidproj"
    path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    with pytest.raises(ProjectIOError):
        load(path)


def test_save_creates_the_containing_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "edit.vidproj"
    save(Project(name="p"), path)
    assert path.is_file()
