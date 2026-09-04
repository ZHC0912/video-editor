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

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402
from ui.theme import apply_theme  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("VidEditor")
    app.setOrganizationName("VidEditor")
    apply_theme(app)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
