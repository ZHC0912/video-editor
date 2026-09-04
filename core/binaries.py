"""Locating the vendored FFmpeg binaries.

VidEditor ships its own FFmpeg. Resolution never falls back to ``PATH``: a
system FFmpeg would be an unknown build with unknown codecs, and silently using
it would make render output depend on the machine.

Both :mod:`core.probe` and :mod:`core.render` go through this module. It is
written once, here.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

__all__ = ["BinaryNotFoundError", "resolve_binary", "ffmpeg_path", "ffprobe_path"]


class BinaryNotFoundError(FileNotFoundError):
    """Raised when a vendored binary is missing from every known location."""


def _candidate_roots() -> list[Path]:
    """Directories that may contain a ``bin`` folder, most specific first."""
    roots: list[Path] = []

    # PyInstaller one-file: everything is unpacked under sys._MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))

    if getattr(sys, "frozen", False):
        # PyInstaller one-folder: binaries sit beside the executable.
        roots.append(Path(sys.executable).resolve().parent)

    # Dev layout: bin/ is a sibling of the core/ package.
    roots.append(Path(__file__).resolve().parent.parent)

    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def _candidate_paths(name: str) -> list[Path]:
    if sys.platform == "win32" and not name.lower().endswith(".exe"):
        name = name + ".exe"
    paths: list[Path] = []
    for root in _candidate_roots():
        paths.append(root / "bin" / name)
        paths.append(root / name)
    return paths


@lru_cache(maxsize=None)
def resolve_binary(name: str) -> Path:
    """Return the absolute path to a vendored binary such as ``"ffprobe"``.

    ``name`` may be given with or without the ``.exe`` suffix. Raises
    :class:`BinaryNotFoundError` if it is not present in any known layout.
    """
    candidates = _candidate_paths(name)
    for path in candidates:
        if path.is_file():
            return path
    searched = "\n  ".join(str(p) for p in candidates)
    raise BinaryNotFoundError(
        f"could not find the vendored binary {name!r}. Searched:\n  {searched}"
    )


def ffmpeg_path() -> Path:
    return resolve_binary("ffmpeg")


def ffprobe_path() -> Path:
    return resolve_binary("ffprobe")
