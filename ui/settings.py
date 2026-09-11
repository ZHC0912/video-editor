"""What the application remembers between runs.

One class over one QSettings, so that every persisted key is written down in
one file and nothing else in the application constructs a QSettings.

Values are stored as plain strings the format cannot mangle. Qt can serialise
a QRect or a QStringList natively, but the native forms are format dependent:
a one-element list read back out of an INI file comes back as a bare string,
not a list of one, which is the sort of bug that only appears for the user who
has exactly one recent file. Lists go through JSON and rectangles through four
integers, and both have a pure parser that a test can call without Qt.

Nothing here is authoritative. Every read has a default and every parse
tolerates rubbish: a settings file is user-writable and survives upgrades, so
it must never be able to stop the application starting.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QRect, QSettings

__all__ = [
    "ORGANISATION",
    "APPLICATION",
    "MAX_RECENT_FILES",
    "Settings",
    "parse_ints",
    "format_ints",
    "parse_rect",
    "format_rect",
    "recent_with",
]

ORGANISATION = "VidEditor"
APPLICATION = "VidEditor"

#: Enough to cover the projects in play, few enough to read without a
#: submenu that scrolls.
MAX_RECENT_FILES = 8


# -- pure helpers -----------------------------------------------------------


def parse_ints(text: object) -> list[int]:
    """``"360,1040"`` to ``[360, 1040]``. Anything unparseable gives ``[]``."""
    if not isinstance(text, str):
        return []
    values: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            return []
    return values


def format_ints(values: list[int]) -> str:
    return ",".join(str(int(v)) for v in values)


def parse_rect(text: object) -> QRect | None:
    """Four integers back into a QRect. None when it is not four integers.

    An empty rectangle is rejected rather than returned: it would restore a
    window with no size, and there is no way to grab one of those.
    """
    values = parse_ints(text)
    if len(values) != 4:
        return None
    rect = QRect(*values)
    return rect if not rect.isEmpty() else None


def format_rect(rect: QRect) -> str:
    return format_ints([rect.x(), rect.y(), rect.width(), rect.height()])


def recent_with(
    existing: list[str], path: str, limit: int = MAX_RECENT_FILES
) -> list[str]:
    """``path`` at the front, no duplicates, no more than ``limit``.

    Case insensitive comparison, because on Windows two spellings of one path
    are one file and a list showing both is a list showing a bug.
    """
    lowered = str(path).lower()
    kept = [p for p in existing if p.lower() != lowered]
    return [str(path), *kept][:limit]


# -- the store --------------------------------------------------------------


class Settings:
    """Everything the application remembers, by name.

    ``store`` is injectable so tests can point at a temporary INI file rather
    than at the user's real registry.
    """

    GEOMETRY = "window/geometry"
    MAXIMISED = "window/maximised"
    SPLIT_TOP = "window/split_top"
    SPLIT_MAIN = "window/split_main"
    ZOOM = "timeline/pixels_per_second"
    RECENT = "files/recent"

    def __init__(self, store: QSettings | None = None) -> None:
        self._store = store if store is not None else QSettings(ORGANISATION, APPLICATION)

    @property
    def store(self) -> QSettings:
        return self._store

    def sync(self) -> None:
        self._store.sync()

    # -- window ------------------------------------------------------------

    def geometry(self) -> QRect | None:
        return parse_rect(self._store.value(self.GEOMETRY))

    def set_geometry(self, rect: QRect) -> None:
        self._store.setValue(self.GEOMETRY, format_rect(rect))

    def maximised(self) -> bool:
        return str(self._store.value(self.MAXIMISED, "0")) in ("1", "true", "True")

    def set_maximised(self, maximised: bool) -> None:
        self._store.setValue(self.MAXIMISED, "1" if maximised else "0")

    # -- splitters ---------------------------------------------------------

    def splitter(self, name: str) -> list[int]:
        """Saved sizes for one splitter, or ``[]`` when there are none.

        A zero anywhere in the list is treated as no answer at all. Qt reports
        the sizes of a splitter that has never been laid out as zeros, and
        restoring those collapses the panel to nothing on the next run with no
        obvious way to get it back.
        """
        sizes = parse_ints(self._store.value(name))
        return sizes if sizes and all(size > 0 for size in sizes) else []

    def set_splitter(self, name: str, sizes: list[int]) -> None:
        self._store.setValue(name, format_ints(sizes))

    # -- timeline ----------------------------------------------------------

    def zoom(self) -> float | None:
        """Pixels per second. One of the three floats the codebase allows."""
        raw = self._store.value(self.ZOOM)
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def set_zoom(self, pixels_per_second: float) -> None:
        self._store.setValue(self.ZOOM, f"{float(pixels_per_second):.6g}")

    # -- recent files ------------------------------------------------------

    def recent_files(self) -> list[str]:
        raw = self._store.value(self.RECENT)
        if not isinstance(raw, str):
            return []
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(values, list):
            return []
        return [str(v) for v in values if isinstance(v, str)][:MAX_RECENT_FILES]

    def set_recent_files(self, paths: list[str]) -> None:
        self._store.setValue(
            self.RECENT, json.dumps([str(p) for p in paths][:MAX_RECENT_FILES])
        )

    def remember_recent(self, path: Path | str) -> list[str]:
        """Push a project to the front of the list. Returns the new list."""
        updated = recent_with(self.recent_files(), str(Path(path)))
        self.set_recent_files(updated)
        return updated

    def forget_recent(self, path: Path | str) -> list[str]:
        """Drop a path, for one that turned out not to open."""
        lowered = str(Path(path)).lower()
        updated = [p for p in self.recent_files() if p.lower() != lowered]
        self.set_recent_files(updated)
        return updated
