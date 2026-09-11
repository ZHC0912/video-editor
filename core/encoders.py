"""Which video encoders this build, and this machine, can actually use.

Two different questions, and the difference matters:

``encoder_is_listed`` asks what the vendored FFmpeg was compiled with. It is a
fact about the build, and the build ships with the application, so the answer
is the same on every machine that runs it.

``encoder_runs`` asks whether the encoder opens here, now, on this hardware
with this driver. That is the question a hardware encoder actually raises: the
binary lists ``h264_nvenc`` whether or not there is an NVIDIA GPU in the
machine, and an NVIDIA GPU whose driver is older than the build expects fails
the same way one that is absent does.

Both are cheap. Listing is a string parse of one FFmpeg invocation; the trial
is a single frame of black encoded to nothing, tens of milliseconds. Both are
cached, because the answer cannot change while the process is running.
"""

from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache

from core.binaries import resolve_binary

__all__ = [
    "SOFTWARE_H264",
    "NVENC_H264",
    "listed_encoders",
    "encoder_is_listed",
    "encoder_runs",
    "hardware_encoder_available",
    "reset_cache",
]

#: The encoder every export can rely on. Vendored, always present.
SOFTWARE_H264 = "libx264"

#: The optional one. Several times faster, worse quality per bitrate.
NVENC_H264 = "h264_nvenc"

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: ` V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)`
#:
#: The name has to be constrained as well as the flags. The legend above the
#: list is written in the same shape as the rows it describes -- ` V..... =
#: Video` matches six flag characters followed by a token just as an encoder
#: row does -- and its "name" is an equals sign.
_ENCODER_ROW = re.compile(r"^\s([VAS.][F.][S.][X.][B.][D.])\s+([A-Za-z0-9][\w.\-]*)")

#: FFmpeg prints this between the legend and the encoders.
_SEPARATOR = "------"


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Run the vendored ffmpeg, or return None if it could not be run at all."""
    try:
        return subprocess.run(
            [str(resolve_binary("ffmpeg")), "-hide_banner", "-nostdin", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        # A missing binary, a timeout, a machine refusing to start a process.
        # None of it is worth an exception: the answer is simply "no".
        return None


def parse_encoders(text: str) -> frozenset[str]:
    """Encoder names out of ``ffmpeg -encoders`` output. Pure, for testing.

    Everything before the separator line is skipped where there is one, and
    the row pattern is strict enough to stand on its own where there is not.
    Two defences because a listing that quietly includes a legend entry would
    make the application offer an encoder called ``=``.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(_SEPARATOR):
            lines = lines[index + 1 :]
            break

    names = set()
    for line in lines:
        match = _ENCODER_ROW.match(line)
        if match is not None:
            names.add(match.group(2))
    return frozenset(names)


@lru_cache(maxsize=1)
def listed_encoders() -> frozenset[str]:
    """Every encoder the vendored FFmpeg was built with."""
    completed = _run(["-encoders"], timeout=20.0)
    if completed is None or completed.returncode != 0:
        return frozenset()
    return parse_encoders(completed.stdout)


def encoder_is_listed(name: str) -> bool:
    return name in listed_encoders()


@lru_cache(maxsize=8)
def encoder_runs(name: str) -> bool:
    """Whether ``name`` actually opens on this machine.

    One frame of black to nowhere. The output goes to the null muxer, so
    nothing is written and nothing has to be cleaned up.

    This is the check that catches a machine with no NVIDIA card, and equally
    the machine that has one whose driver is older than the NVENC API the
    build was compiled against. Listing alone cannot tell either apart from a
    working GPU.
    """
    completed = _run(
        [
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", "color=c=black:s=256x256:r=25:d=0.2",
            "-frames:v", "1",
            "-c:v", name,
            "-f", "null", "-",
        ],
        timeout=30.0,
    )
    return completed is not None and completed.returncode == 0


def hardware_encoder_available() -> bool:
    """Whether the export dialog should offer hardware encoding at all.

    Listed first because it is the cheaper question and it is decisive when
    the answer is no. The trial only runs on a build that has the encoder.
    """
    return encoder_is_listed(NVENC_H264) and encoder_runs(NVENC_H264)


def reset_cache() -> None:
    """Forget both answers. For tests, and for nothing else."""
    listed_encoders.cache_clear()
    encoder_runs.cache_clear()
