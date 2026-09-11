"""VidEditor entry point.

    .venv\\Scripts\\activate
    python app.py
"""

from __future__ import annotations

import os
import sys

# Must be set before the QApplication is constructed: Qt reads the rules once,
# when the logging framework initialises.
#
# The FFmpeg media backend pipes libav's chatter through this category at info
# level, which is a full stream banner on stderr every time a file is opened.
# Suppressing only info and debug leaves warnings and errors coming through, so
# a real decode problem still reaches the console.
#
# The trailing wildcard matters. The backend logs under sub-categories of
# qt.multimedia.ffmpeg, so a rule naming the bare category matches nothing.
#
# Set QT_LOGGING_RULES yourself to override this while debugging.
os.environ.setdefault(
    "QT_LOGGING_RULES",
    "qt.multimedia.ffmpeg*.info=false;qt.multimedia.ffmpeg*.debug=false",
)

from pathlib import Path  # noqa: E402

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402
from ui.theme import apply_theme  # noqa: E402
from ui.workers.audio_bed_worker import purge_stale_beds  # noqa: E402

#: Bundled by build.spec into the same relative place, so this resolves in a
#: PyInstaller one-folder build as well as from a source checkout.
ICON_PATH = Path(__file__).resolve().parent / "assets" / "videditor.ico"


def application_icon() -> QIcon:
    """The window and taskbar icon, or an empty one if it is not there."""
    return QIcon(str(ICON_PATH)) if ICON_PATH.is_file() else QIcon()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("VidEditor")
    app.setOrganizationName("VidEditor")
    app.setWindowIcon(application_icon())
    apply_theme(app)

    # Audio beds are tens of megabytes of WAV in the temp directory. The
    # current session's is deleted on a clean exit; this clears up after the
    # sessions that did not get one. Startup, before the window exists,
    # because it must never race the bed this run is about to write.
    purge_stale_beds()

    window = MainWindow()
    window.show()

    # A project named on the command line, which is what double-clicking a
    # .vedit in Explorer passes. Opened before the recovery prompt so that an
    # explicit request wins over a leftover autosave.
    opened = _open_argument(window, app.arguments()[1:])
    if not opened:
        # After show(), never during construction: this can raise a modal
        # dialog, and a modal dialog owned by a window that has not been drawn
        # yet looks like the application hanging on launch.
        window.offer_autosave_recovery()
    return app.exec()


def _open_argument(window: MainWindow, arguments: list[str]) -> bool:
    """Open the first project file named on the command line, if any."""
    for argument in arguments:
        path = Path(argument)
        if path.is_file():
            return window.load_project_file(path, prompt=False)
    return False


if __name__ == "__main__":
    sys.exit(main())
