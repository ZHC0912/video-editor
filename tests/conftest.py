"""Shared fixtures.

Media fixtures are generated on demand with the vendored ffmpeg and lavfi
sources. No binary fixture is ever committed to the repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.binaries import resolve_binary

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _run_ffmpeg(args: list[str]) -> None:
    cmd = [str(resolve_binary("ffmpeg")), "-nostdin", "-y", *args]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "fixture generation failed:\n"
            + " ".join(cmd)
            + "\n"
            + "\n".join(completed.stderr.splitlines()[-20:])
        )


@pytest.fixture(scope="session", autouse=True)
def _redirect_qsettings(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Keep the test run out of the user's real settings.

    The application remembers window geometry, splitter sizes, zoom and
    recent files, so a MainWindow built by a test would otherwise write all of
    it into the registry of whoever ran pytest. Redirecting the INI format and
    making it the default sends every QSettings in the process to a temp file
    instead.

    Autouse and session scoped: it has to be in place before the first window
    is constructed, and there is no test that wants the real store.
    """
    from PySide6.QtCore import QSettings

    settings_dir = tmp_path_factory.mktemp("settings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, str(settings_dir))
    return settings_dir


@pytest.fixture(scope="session")
def _default_store(_redirect_qsettings: Path):
    """One long-lived handle on the default QSettings.

    Held for the session rather than built per test: destroying a QSettings
    flushes it to disk, and there is no reason to do that before every test in
    the suite.
    """
    from PySide6.QtCore import QSettings

    return QSettings()


@pytest.fixture(autouse=True)
def _fresh_settings(_default_store) -> None:
    """Start every test with an empty store.

    A MainWindow built without an explicit Settings uses the default one, and
    writes its geometry, splitter sizes and zoom back on close. Without this,
    a window closed by one test restores itself into the next test's window,
    and a test that expects a freshly placed window fails depending on what
    ran before it. That is a test isolation problem, not a product one: in the
    application, restoring the last session is the entire point.

    Cleared only when there is something to clear, since almost no test writes
    settings at all.
    """
    if _default_store.allKeys():
        _default_store.clear()


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def av_file(media_dir: Path) -> Path:
    """Two seconds of 320x240 testsrc at 30fps with a 440Hz tone."""
    out = media_dir / "av.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-shortest", str(out),
        ]
    )
    return out


@pytest.fixture(scope="session")
def video_only_file(media_dir: Path) -> Path:
    """One second of 640x480 testsrc at 25fps, no audio stream."""
    out = media_dir / "video_only.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc=size=640x480:rate=25:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-an", str(out),
        ]
    )
    return out


@pytest.fixture(scope="session")
def audio_only_file(media_dir: Path) -> Path:
    """One second of 48kHz stereo tone, no video stream."""
    out = media_dir / "audio_only.wav"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-ac", "2", "-c:a", "pcm_s16le", str(out),
        ]
    )
    return out
