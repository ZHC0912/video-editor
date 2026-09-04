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
