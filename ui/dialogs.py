"""The application's error surface.

One helper, used everywhere. A traceback dialog is never acceptable: every
failure the user can provoke has a message written for them, and the ffmpeg
output that explains it goes in the collapsed Details section rather than in
the sentence they have to read.

The signature is the contract: a parent, a title, one sentence the user can
act on, and an optional block of detail behind a button. Everything that has
to tell the user something comes through here, so a failure and a question
look like they came from the same application.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

__all__ = ["show_error", "split_error", "confirm"]


def show_error(
    parent: QWidget | None,
    title: str,
    message: str,
    detail: str = "",
) -> None:
    """Report a failure. ``detail`` goes behind the Details button."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    if detail:
        box.setDetailedText(detail)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def confirm(parent: QWidget | None, title: str, message: str) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(message)
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def split_error(exc: BaseException) -> tuple[str, str]:
    """Split an exception into a headline and a detail block.

    RenderError and ProbeError both put a human sentence on the first line and
    the ffmpeg output underneath, so the first line is the message and the rest
    is the detail. ProbeError also carries its stderr separately; it is only
    appended if it is not already in the text.
    """
    text = str(exc).strip()
    if not text:
        text = exc.__class__.__name__

    headline, _, rest = text.partition("\n")
    detail = rest.strip()

    stderr = (getattr(exc, "stderr", "") or "").strip()
    if stderr and stderr not in detail:
        detail = f"{detail}\n\n{stderr}".strip()

    return headline.strip(), detail
