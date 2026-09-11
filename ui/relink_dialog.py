"""Pointing a project at media that has moved.

Opened when a project is loaded whose sources are not where it left them, and
from the Edit menu at any time. Two ways to fix a file: pick its replacement,
or pick a folder and let it be searched recursively by filename, which is what
actually happens when a card is copied to a different drive.

Every row here is a FILE, because that is what the user recognises and what
they have one answer for. The clips are worked out afterwards, by id, in
:func:`core.media_check.relink_map` and :class:`core.commands.RelinkMedia`.
Nothing in this file addresses a clip or a track by position.

The folder search runs on a pool thread. Walking a directory tree is exactly
the kind of work that is instant on a project folder and takes a quarter of a
minute on the root of a drive, and a frozen window in the second case is not
worth the simplicity in the first.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.media_check import MissingSource, search_folder
from ui.workers import media_pool, report_safely

__all__ = ["RelinkDialog"]

_MEDIA_FILTER = (
    "Media files (*.mp4 *.mov *.mkv *.avi *.m4v *.webm *.wav *.mp3 *.aac *.flac);;"
    "All files (*)"
)


class _SearchSignals(QObject):
    #: name -> found path, for everything the walk turned up.
    done = Signal(object)


class _SearchJob(QRunnable):
    """One recursive walk of a folder, looking for a set of filenames."""

    def __init__(self, folder: Path, names: list[str]) -> None:
        super().__init__()
        self.signals = _SearchSignals()
        self._folder = folder
        self._names = names

    def run(self) -> None:
        try:
            found = search_folder(self._folder, self._names)
        except OSError:
            # An unreadable folder is a search that found nothing, not a
            # crash on a pool thread.
            found = {}
        report_safely(self.signals.done.emit, found)


class RelinkDialog(QDialog):
    """One row per missing file, with somewhere to put the answer."""

    def __init__(
        self, missing: list[MissingSource], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Missing media")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._missing = list(missing)
        #: The answers so far, keyed by the ORIGINAL path. Never by row.
        self._replacements: dict[Path, Path] = {}
        self._searching = False

        clips = sum(len(entry.clip_ids) for entry in self._missing)
        headline = QLabel(
            f"{len(self._missing)} source file(s) used by {clips} clip(s) "
            f"could not be found.",
            self,
        )
        headline.setWordWrap(True)

        hint = QLabel(
            "Locate a file to point its clips at a new copy, or search a folder "
            "to find several at once. Clips left unresolved stay on the timeline, "
            "marked, and are skipped on export.",
            self,
        )
        hint.setWordWrap(True)
        hint.setProperty("muted", True)

        self._list = QListWidget(self)
        self._list.setAlternatingRowColors(False)
        for entry in self._missing:
            item = QListWidgetItem(self._row_text(entry))
            # The path, not the row number: the list is rebuilt as answers
            # arrive and a stored index would go stale.
            item.setData(Qt.ItemDataRole.UserRole, str(entry.src))
            item.setToolTip(str(entry.src))
            self._list.addItem(item)
        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _item: self._on_locate())

        self._locate_button = QPushButton("Locate...", self)
        self._locate_button.clicked.connect(self._on_locate)
        self._search_button = QPushButton("Search folder...", self)
        self._search_button.clicked.connect(self._on_search)

        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.addWidget(self._locate_button)
        buttons_row.addWidget(self._search_button)
        buttons_row.addStretch(1)

        self._status = QLabel("", self)
        self._status.setProperty("muted", True)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Relink")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("Leave unresolved")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        self._buttons = box

        layout = QVBoxLayout(self)
        layout.addWidget(headline)
        layout.addWidget(hint)
        layout.addWidget(self._list, 1)
        layout.addLayout(buttons_row)
        layout.addWidget(self._status)
        layout.addWidget(box)

        self._refresh()

    # -- results -----------------------------------------------------------

    def replacements(self) -> dict[Path, Path]:
        """Original path -> replacement, for the ones that were answered."""
        return dict(self._replacements)

    # -- rows --------------------------------------------------------------

    def _row_text(self, entry: MissingSource) -> str:
        found = self._replacements.get(entry.src)
        clips = len(entry.clip_ids)
        suffix = f"  ({clips} clip{'s' if clips != 1 else ''})"
        if found is None:
            return f"{entry.name}{suffix}\n    missing: {entry.src}"
        return f"{entry.name}{suffix}\n    found: {found}"

    def _selected_entry(self) -> MissingSource | None:
        item = self._list.currentItem()
        if item is None:
            return None
        src = str(item.data(Qt.ItemDataRole.UserRole))
        for entry in self._missing:
            if str(entry.src) == src:
                return entry
        return None

    def _refresh(self) -> None:
        for row, entry in enumerate(self._missing):
            item = self._list.item(row)
            if item is not None:
                item.setText(self._row_text(entry))
        resolved = len(self._replacements)
        self._status.setText(
            f"{resolved} of {len(self._missing)} resolved"
            if not self._searching
            else "Searching..."
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            resolved > 0 and not self._searching
        )
        self._locate_button.setEnabled(not self._searching)
        self._search_button.setEnabled(not self._searching)

    # -- actions -----------------------------------------------------------

    def _on_locate(self) -> None:
        entry = self._selected_entry()
        if entry is None or self._searching:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"Locate {entry.name}", str(entry.src.parent), _MEDIA_FILTER
        )
        if path:
            self._replacements[entry.src] = Path(path)
            self._refresh()

    def _on_search(self) -> None:
        if self._searching:
            return
        unresolved = [e for e in self._missing if e.src not in self._replacements]
        if not unresolved:
            return
        folder = QFileDialog.getExistingDirectory(self, "Search folder")
        if not folder:
            return

        self._searching = True
        self._refresh()
        job = _SearchJob(Path(folder), [entry.name for entry in unresolved])
        job.signals.done.connect(self._on_search_finished)
        media_pool().start(job)

    def _on_search_finished(self, found: dict) -> None:
        self._searching = False
        matched = 0
        for entry in self._missing:
            if entry.src in self._replacements:
                continue
            hit = found.get(entry.name)
            if hit is not None:
                self._replacements[entry.src] = Path(hit)
                matched += 1
        self._refresh()
        if matched == 0:
            self._status.setText("Nothing matching was found in that folder")
