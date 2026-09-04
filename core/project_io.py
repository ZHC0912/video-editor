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

__all__ = ["SCHEMA_VERSION", "ProjectIOError", "save", "load", "to_dict", "from_dict"]

SCHEMA_VERSION = 2


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
