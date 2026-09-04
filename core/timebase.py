"""Integer tick timebase.

Every time value in VidEditor is an integer number of ticks. A tick is
1/120000 of a second.

Why 120000 and not the MPEG-standard 90000
------------------------------------------
90000 is the classic MPEG-2 / RTP clock. It is a fine choice for muxing but a
bad one here, because it does not divide evenly by the NTSC rates. At
24000/1001 fps one frame is 90000 * 1001 / 24000 = 3753.75 ticks, and at
30000/1001 fps it is 3003.0 -- exact -- but 60000/1001 gives 1501.5. Any rate
whose frame duration lands on a fraction of a tick reintroduces exactly the
rounding drift the integer timebase exists to eliminate.

120000 is divisible by 1000 and by 24, 25, 30, 50, 60 and 120, so every rate
this editor supports lands on a whole number of ticks per frame:

    fps            ticks per frame
    24             5000
    25             4800
    30             4000
    50             2400
    60             2000
    120            1000
    24000/1001     5005
    30000/1001     4004
    60000/1001     2002

Python integers are unbounded, so there is no overflow ceiling to design
around. A 24 hour timeline is 1.0368e10 ticks, which is simply a large int.

Floats appear in exactly two places: the display boundary (timecode, the ruler)
and the FFmpeg argument string. Both are one-way exits from the tick domain.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

TICKS_PER_SECOND = 120000


@dataclass(frozen=True)
class FrameRate:
    """A frame rate as an exact rational.

    30fps is ``FrameRate(30, 1)``. 29.97 is ``FrameRate(30000, 1001)``.
    Never store a frame rate as a decimal number.
    """

    num: int
    den: int = 1

    def __post_init__(self) -> None:
        if self.num <= 0:
            raise ValueError(f"frame rate numerator must be positive, got {self.num}")
        if self.den <= 0:
            raise ValueError(f"frame rate denominator must be positive, got {self.den}")

    @property
    def as_float(self) -> float:
        """Decimal value. For display and for FFmpeg arguments only."""
        return self.num / self.den

    @property
    def ticks_per_frame(self) -> Fraction:
        """Exact duration of one frame, in ticks.

        Returns a ``Fraction``, not an int. For every rate listed in the module
        docstring this happens to be a whole number, but that is a property of
        those rates, not a guarantee of the type. Do not encode the assumption.
        """
        return Fraction(TICKS_PER_SECOND * self.den, self.num)

    @property
    def as_fraction(self) -> Fraction:
        """The rate itself, reduced."""
        return Fraction(self.num, self.den)

    @classmethod
    def from_ffprobe(cls, r_frame_rate: str) -> "FrameRate":
        """Parse ffprobe's ``r_frame_rate`` field, e.g. ``"30000/1001"``.

        Accepts a bare integer string too. Reduces with :func:`math.gcd`.
        """
        text = r_frame_rate.strip()
        if "/" in text:
            num_text, _, den_text = text.partition("/")
            num, den = int(num_text), int(den_text)
        else:
            num, den = int(text), 1
        if den == 0:
            raise ValueError(f"invalid r_frame_rate {r_frame_rate!r}: zero denominator")
        if num == 0:
            raise ValueError(f"invalid r_frame_rate {r_frame_rate!r}: zero numerator")
        divisor = math.gcd(num, den)
        return cls(num // divisor, den // divisor)

    def __str__(self) -> str:
        return f"{self.num}/{self.den}"


def seconds_to_ticks(s: float) -> int:
    """Convert seconds to ticks. Import boundaries only, never internally."""
    return round(s * TICKS_PER_SECOND)


def ticks_to_seconds(t: int) -> float:
    """Convert ticks to seconds. Display and FFmpeg arguments only."""
    return t / TICKS_PER_SECOND


def frames_to_ticks(f: int, rate: FrameRate) -> int:
    """Tick position of the start of frame ``f``."""
    return round(Fraction(f) * rate.ticks_per_frame)


def ticks_to_frames(t: int, rate: FrameRate) -> int:
    """Index of the frame containing tick ``t``. Floors, so it is stable
    across the whole span of a frame."""
    return math.floor(Fraction(t) / rate.ticks_per_frame)


def snap_to_frame(t: int, rate: FrameRate) -> int:
    """Round ``t`` to the nearest frame boundary, halves rounding up."""
    index = Fraction(t) / rate.ticks_per_frame + Fraction(1, 2)
    return frames_to_ticks(math.floor(index), rate)


def ticks_to_timecode(t: int, rate: FrameRate) -> str:
    """Format ticks as non-drop-frame ``HH:MM:SS:FF``.

    The frame field counts whole frames at ``rate``, and the seconds field
    counts groups of ``round(rate)`` frames. For the NTSC rates that means
    timecode runs slightly slower than wall clock, which is the standard
    non-drop behaviour.
    """
    sign = "-" if t < 0 else ""
    frames = ticks_to_frames(abs(t), rate)
    per_second = round(rate.as_float)
    if per_second <= 0:
        raise ValueError(f"frame rate {rate} is too low to format as timecode")
    ff = frames % per_second
    total_seconds = frames // per_second
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
