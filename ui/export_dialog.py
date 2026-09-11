"""Choosing what to export, before anything starts encoding.

Everything here is a choice that changes the FFmpeg command, and the command
is assembled in :mod:`core.render`, not here: this dialog returns a value
object and knows nothing about how a render is run.

The hardware encoding option is absent, not disabled, when the machine cannot
use it. A disabled checkbox invites the user to work out how to enable it, and
on a machine with no NVIDIA GPU there is nothing to work out.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.encoders import NVENC_H264, SOFTWARE_H264
from core.model import Project
from core.render import QUALITY_PRESETS, RESOLUTION_PRESETS, preset_size
from ui import format_utils

__all__ = ["ExportRequest", "ExportDialog", "SOURCE_PRESET", "HARDWARE_TOOLTIP"]

#: The resolution entry that means "leave it at the project's own size".
SOURCE_PRESET = "Source"

HARDWARE_TOOLTIP = (
    "Encode on the GPU with NVENC.\n"
    "Quality per bitrate is lower than the software encoder, "
    "but the export is several times faster."
)

_VIDEO_FILTER = "MP4 video (*.mp4);;All files (*)"


@dataclass(frozen=True)
class ExportRequest:
    """Everything the render needs, and nothing about how to run it."""

    out_path: Path
    crf: int
    encoder: str
    out_size: tuple[int, int] | None

    @property
    def hardware(self) -> bool:
        return self.encoder == NVENC_H264


class ExportDialog(QDialog):
    """Destination, resolution, quality, and whether to use the GPU."""

    def __init__(
        self,
        project: Project,
        parent: QWidget | None = None,
        hardware_available: bool = False,
        suggested_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self.setWindowTitle("Export video")
        self.setModal(True)

        self._path_edit = QLineEdit(str(suggested_path or ""), self)
        self._path_edit.setPlaceholderText("Choose where to write the file")
        self._path_edit.setMinimumWidth(320)
        browse = QPushButton("Browse...", self)
        browse.clicked.connect(self._on_browse)

        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse)

        self._resolution = QComboBox(self)
        self._resolution.addItem(SOURCE_PRESET)
        for name in RESOLUTION_PRESETS:
            self._resolution.addItem(name)
        self._resolution.setToolTip(
            "The timeline is always composed at the project's own size. "
            "A smaller preset scales the finished video on the way out."
        )

        self._quality = QComboBox(self)
        for name, crf in QUALITY_PRESETS.items():
            self._quality.addItem(f"{name} (CRF {crf})", crf)
        self._quality.setCurrentIndex(list(QUALITY_PRESETS).index("Medium"))
        self._quality.setToolTip(
            "Constant quality. A lower CRF is a better picture and a bigger file."
        )

        self._hardware = QCheckBox("Hardware encoding (NVENC)", self)
        self._hardware.setToolTip(HARDWARE_TOOLTIP)
        self._hardware.setChecked(False)
        # Absent rather than disabled: see the module docstring.
        self._hardware.setVisible(hardware_available)

        self._summary = QLabel("", self)
        self._summary.setProperty("muted", True)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("File", path_row)
        form.addRow("Resolution", self._resolution)
        form.addRow("Quality", self._quality)
        if hardware_available:
            form.addRow("", self._hardware)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._summary)
        layout.addWidget(self._buttons)

        self._resolution.currentIndexChanged.connect(self._refresh)
        self._quality.currentIndexChanged.connect(self._refresh)
        self._path_edit.textChanged.connect(self._refresh)
        self._refresh()

    # -- state -------------------------------------------------------------

    def out_path(self) -> Path | None:
        text = self._path_edit.text().strip()
        if not text:
            return None
        path = Path(text)
        # A name typed without an extension gets the container's, so the file
        # is playable by double-clicking it rather than mysteriously not.
        return path if path.suffix else path.with_suffix(".mp4")

    def crf(self) -> int:
        return int(self._quality.currentData())

    def encoder(self) -> str:
        return NVENC_H264 if self._hardware.isChecked() else SOFTWARE_H264

    def out_size(self) -> tuple[int, int] | None:
        return preset_size(self._project, self._resolution.currentText())

    def request(self) -> ExportRequest | None:
        """What was chosen, or None if there is no destination."""
        path = self.out_path()
        if path is None:
            return None
        return ExportRequest(
            out_path=path,
            crf=self.crf(),
            encoder=self.encoder(),
            out_size=self.out_size(),
        )

    # -- internals ---------------------------------------------------------

    def _on_browse(self) -> None:
        suggested = self._path_edit.text().strip() or f"{self._project.name}.mp4"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export video", suggested, _VIDEO_FILTER
        )
        if path:
            self._path_edit.setText(path)

    def _refresh(self) -> None:
        size = self.out_size() or (self._project.width, self._project.height)
        rate = self._project.frame_rate
        self._summary.setText(
            f"{size[0]}x{size[1]}  "
            f"{format_utils.frame_rate_text(rate)}  "
            f"{format_utils.timecode(self._project.duration, rate)}  "
            f"({format_utils.duration_text(self._project.duration)})"
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(self.out_path() is not None)
