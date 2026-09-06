"""The media bin list, and the drag-and-drop plumbing it shares with the window.

Files can be dropped on the bin or anywhere else on the window. Dropping on the
wrong panel is the ordinary mistake, and refusing the drop teaches nothing, so
both accept and both route to the same handler.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtCore import QByteArray, QMimeData, Qt
from PySide6.QtGui import (
    QDrag,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
)
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QWidget

from ui.timeline.interaction import MEDIA_MIME

__all__ = ["MediaBinList", "dropped_paths", "has_droppable_files", "set_drop_highlight"]


def dropped_paths(mime: QMimeData) -> list[Path]:
    """Local file paths carried by a drop, in the order they were given."""
    if not mime.hasUrls():
        return []
    paths: list[Path] = []
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        local = url.toLocalFile()
        if local:
            paths.append(Path(local))
    return paths


def has_droppable_files(mime: QMimeData) -> bool:
    return bool(dropped_paths(mime))


def set_drop_highlight(widget: QWidget, active: bool) -> None:
    """Turn the 2px accent border on or off.

    Done with a dynamic property and a stylesheet rule rather than a custom
    paintEvent, so the colour comes from the theme like everything else. Qt
    only re-reads the stylesheet on a polish, hence the unpolish/polish pair.
    """
    if widget.property("dropActive") == active:
        return
    widget.setProperty("dropActive", active)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class MediaBinList(QListWidget):
    """The bin. Accepts file drops and hands them to a callback."""

    def __init__(
        self,
        on_drop: Callable[[Iterable[Path]], None],
        parent: QWidget | None = None,
        payload_for: Callable[[QListWidgetItem], bytes | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_drop = on_drop
        self._payload_for = payload_for
        self.setAcceptDrops(True)
        # Files come in from outside, and rows go out to the timeline. The two
        # directions carry different formats and never collide: an incoming
        # drop is recognised by its URLs, an outgoing one by MEDIA_MIME, and
        # neither carries the other's.
        self.setDragDropMode(QListWidget.DragDropMode.DragDrop)
        self.setDragEnabled(True)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    # -- dragging a row out to the timeline -------------------------------

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        """Publish the row under the pointer as a droppable media reference.

        The drag carries the probe result, not just a path, so the timeline can
        size and validate its ghost without reaching back into the window. One
        row per drag: a bin selection can hold several, but a drop lands at one
        position on one track and there is no sensible answer for the rest.
        """
        item = self.currentItem()
        if item is None or self._payload_for is None:
            return
        raw = self._payload_for(item)
        if raw is None:
            # Still being probed, or unreadable. Nothing to place yet.
            return

        mime = QMimeData()
        mime.setData(MEDIA_MIME, QByteArray(raw))
        drag = QDrag(self)
        drag.setMimeData(mime)
        sizes = item.icon().availableSizes()
        if sizes:
            drag.setPixmap(item.icon().pixmap(sizes[0]))
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if has_droppable_files(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            set_drop_highlight(self, True)
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if has_droppable_files(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        set_drop_highlight(self, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        set_drop_highlight(self, False)
        paths = dropped_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.acceptProposedAction()
        self._on_drop(paths)
