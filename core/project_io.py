"""Saving and loading projects as JSON.

Source paths are stored relative to the project file's directory when the media
sits under the same tree, so a project folder can be moved or copied whole.
Media outside that tree is stored absolute. Both forms resolve back to absolute
paths on load.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.model import Project

__all__ = [
    "SCHEMA_VERSION",
    "PROJECT_EXTENSION",
    "PROJECT_FORMAT_NAME",
    "AUTOSAVE_SUFFIX",
    "autosave_path",
    "autosave_is_newer",
    "discard_autosave",
    "ProjectIOError",
    "is_project_path",
    "with_project_extension",
    "save",
    "load",
    "to_dict",
    "from_dict",
]

SCHEMA_VERSION = 2

#: The one place the project file extension is written down. The Open and Save
#: dialogs both build their filters from this; three separate literals is how a
#: project saved with one extension became invisible to a dialog filtering for
#: another.
PROJECT_EXTENSION = ".vedit"

#: Human readable name of the format, for file dialog filters.
PROJECT_FORMAT_NAME = "VidEditor project"

#: Appended to the whole filename, not substituted for the extension, so an
#: autosave is obviously a sidecar of one particular project file:
#: ``holiday.vedit`` autosaves to ``holiday.vedit.autosave``. Replacing the
#: extension would make two projects in one folder, ``a.vedit`` and ``a.bak``,
#: share an autosave.
AUTOSAVE_SUFFIX = ".autosave"


def autosave_path(project_path: Path) -> Path:
    """Where the autosave for a saved project lives."""
    return Path(str(Path(project_path)) + AUTOSAVE_SUFFIX)


def autosave_is_newer(project_path: Path) -> bool:
    """Whether an autosave holds work the project file on disk does not.

    Strictly newer. Saving writes the project and deletes its autosave, so an
    autosave that still exists and is newer means the application stopped
    without saving: a crash, a power cut, or Task Manager.

    Any OSError is a no. This runs on the startup path and a permissions
    problem on a sidecar file must not stop the application opening.
    """
    project_path = Path(project_path)
    sidecar = autosave_path(project_path)
    try:
        if not sidecar.is_file():
            return False
        if not project_path.is_file():
            # An autosave whose project has been deleted is not a recovery
            # candidate: there is nothing to recover it into.
            return False
        return sidecar.stat().st_mtime > project_path.stat().st_mtime
    except OSError:
        return False


def discard_autosave(project_path: Path) -> None:
    """Delete the autosave sidecar, if there is one. Never raises."""
    try:
        autosave_path(project_path).unlink(missing_ok=True)
    except OSError:
        pass


def is_project_path(path: Path) -> bool:
    """Whether a path names a project file, by extension. Case insensitive."""
    return Path(path).suffix.lower() == PROJECT_EXTENSION


def with_project_extension(path: Path) -> Path:
    """Add the project extension when the name has none at all.

    A name the user gave an extension to is left alone: they may be keeping
    projects as ``.bak`` or some scheme of their own, and load() does not care
    what a file is called.
    """
    path = Path(path)
    return path if path.suffix else path.with_suffix(PROJECT_EXTENSION)


class ProjectIOError(RuntimeError):
    """The file on disk is not a project this build can read."""


def _relativise(src: str, base: Path) -> str:
    """Return ``src`` relative to ``base`` if it lives under it, else absolute."""
    path = Path(src)
    try:
        absolute = path if path.is_absolute() else (base / path).resolve()
        relative = Path(os.path.relpath(absolute, base))
    except ValueError:
        # Different drive on Windows. Nothing sensible to make relative to.
        return str(path)
    if relative.parts and relative.parts[0] == "..":
        return str(absolute)
    return relative.as_posix()


def _absolutise(src: str, base: Path) -> str:
    path = Path(src)
    if path.is_absolute():
        return str(path)
    return str((base / path).resolve())


def to_dict(project: Project, base: Path | None = None) -> dict[str, Any]:
    """Serialisable form of ``project``. ``base`` is the project file's directory."""
    data = project.model_dump(mode="json")
    if base is not None:
        base = Path(base).resolve()
        for track in data["tracks"]:
            for clip in track["clips"]:
                clip["src"] = _relativise(clip["src"], base)
    return {"schema_version": SCHEMA_VERSION, "project": data}


def from_dict(data: dict[str, Any], base: Path | None = None) -> Project:
    """Rebuild a :class:`Project` from :func:`to_dict` output."""
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ProjectIOError(
            f"unsupported schema_version {version!r}, this build reads {SCHEMA_VERSION}"
        )
    payload = data.get("project")
    if not isinstance(payload, dict):
        raise ProjectIOError("project file has no 'project' object")
    if base is not None:
        base = Path(base).resolve()
        for track in payload.get("tracks", []):
            for clip in track.get("clips", []):
                clip["src"] = _absolutise(clip["src"], base)
    return Project.model_validate(payload)


def save(project: Project, path: Path) -> None:
    """Write ``project`` to ``path`` as JSON."""
    path = Path(path)
    base = path.resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    payload = to_dict(project, base)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load(path: Path) -> Project:
    """Read a project written by :func:`save`."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProjectIOError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectIOError(f"{path} does not hold a JSON object")
    return from_dict(raw, path.resolve().parent)
