"""Finding, and repairing, clips whose source file is not where it was.

A project stores paths. Files move. Everything in this module is about the gap
between the two, and none of it touches Qt.

The whole module addresses clips and tracks BY ID, never by position. Model
order and lane order are not the same thing: removing and re-adding the video
track leaves ``project.tracks`` as ``['A1', 'V1']`` while the timeline still
draws video on top, so "the first track" means one thing to the model and
another to the window. An index taken from one and used on the other relinks
the wrong clip, and the failure is silent because both are valid tracks with
valid clips. Ids do not have that problem.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from core.model import Clip, Project, Track

__all__ = [
    "MissingSource",
    "iter_clips",
    "clip_by_id",
    "missing_sources",
    "has_missing_media",
    "missing_clip_ids",
    "search_folder",
    "relink_map",
    "without_missing_clips",
]


@dataclass(frozen=True)
class MissingSource:
    """One source file that is referenced but not present.

    ``clip_ids`` is every clip pointing at it, which is usually more than one:
    splitting a clip leaves both halves on the same file, so relinking has to
    fix all of them from a single choice by the user.
    """

    src: Path
    clip_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return self.src.name


def iter_clips(project: Project | None) -> Iterator[tuple[Track, Clip]]:
    """Every clip in the project, with the track it sits on."""
    if project is None:
        return
    for track in project.tracks:
        for clip in track.clips:
            yield track, clip


def clip_by_id(project: Project | None, clip_id: str) -> tuple[Track, Clip] | None:
    """Look a clip up by id. The only supported way to find one."""
    for track, clip in iter_clips(project):
        if clip.id == clip_id:
            return track, clip
    return None


def _key(path: Path) -> str:
    """One identity per file, case insensitively on Windows."""
    return os.path.normcase(str(Path(path)))


def missing_sources(
    project: Project | None, exists=None
) -> list[MissingSource]:
    """Distinct source files that no longer exist, in first-use order.

    ``exists`` is the filesystem test, injectable so this can be tested
    without touching a disk.
    """
    if exists is None:
        exists = Path.exists

    order: list[str] = []
    clips: dict[str, list[str]] = {}
    paths: dict[str, Path] = {}
    checked: dict[str, bool] = {}

    for _track, clip in iter_clips(project):
        key = _key(clip.src)
        if key not in checked:
            checked[key] = bool(exists(Path(clip.src)))
        if checked[key]:
            continue
        if key not in clips:
            order.append(key)
            clips[key] = []
            paths[key] = Path(clip.src)
        clips[key].append(clip.id)

    return [MissingSource(paths[k], tuple(clips[k])) for k in order]


def has_missing_media(project: Project | None, exists=None) -> bool:
    return bool(missing_sources(project, exists))


def missing_clip_ids(project: Project | None, exists=None) -> frozenset[str]:
    """Ids of every clip whose source is gone.

    The timeline paints these differently and the exporter drops them, and
    both want the answer as a set of ids rather than a list of files.
    """
    return frozenset(
        clip_id
        for missing in missing_sources(project, exists)
        for clip_id in missing.clip_ids
    )


def search_folder(
    folder: Path, names: Iterable[str], max_entries: int = 200_000
) -> dict[str, Path]:
    """Walk ``folder`` recursively looking for files with any of ``names``.

    Returns the first match for each name, keyed by the name as given. Case
    insensitive, because a file copied off a Windows share can come back with
    different capitalisation and refusing to find it would be pedantry.

    ``max_entries`` bounds the walk. Pointing this at the root of a drive
    should give up rather than run for a quarter of an hour.
    """
    wanted = {name.lower(): name for name in names}
    found: dict[str, Path] = {}
    seen = 0

    for root, _dirs, files in os.walk(folder):
        for filename in files:
            seen += 1
            if seen > max_entries:
                return found
            key = filename.lower()
            if key in wanted and wanted[key] not in found:
                found[wanted[key]] = Path(root) / filename
                if len(found) == len(wanted):
                    return found
    return found


def relink_map(
    project: Project | None, replacements: dict[Path, Path]
) -> dict[str, Path]:
    """Turn "this file is now that file" into "this clip id gets that path".

    The dialog works in files, because that is what the user chooses. The
    command works in clip ids, because that is what can be applied to a model
    whose track order may have changed since. This is the one place the two
    meet.
    """
    by_key = {_key(old): new for old, new in replacements.items()}
    return {
        clip.id: Path(by_key[_key(clip.src)])
        for _track, clip in iter_clips(project)
        if _key(clip.src) in by_key
    }


def without_missing_clips(
    project: Project, exists=None
) -> tuple[Project, list[str]]:
    """A copy of ``project`` with unresolved clips dropped, and their names.

    Export uses this. A clip whose file has gone cannot be encoded, and
    FFmpeg's failure for a missing input is unhelpful enough that letting it
    happen would be a worse experience than saying so first and rendering the
    rest.

    The copy is deep, so the caller cannot accidentally hand the trimmed model
    back to the window as the real project.
    """
    gone = missing_clip_ids(project, exists)
    copy = project.model_copy(deep=True)
    skipped: list[str] = []
    for track in copy.tracks:
        keep = []
        for clip in track.clips:
            if clip.id in gone:
                skipped.append(Path(clip.src).name)
            else:
                keep.append(clip)
        track.clips = keep
    return copy, skipped
