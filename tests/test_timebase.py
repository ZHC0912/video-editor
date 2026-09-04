"""Timebase tests. The whole editor rests on these being right."""

from __future__ import annotations

from fractions import Fraction

import pytest

from core.timebase import (
    TICKS_PER_SECOND,
    FrameRate,
    frames_to_ticks,
    seconds_to_ticks,
    snap_to_frame,
    ticks_to_frames,
    ticks_to_seconds,
    ticks_to_timecode,
)

# Every rate the editor claims to support, with the tick count the module
# docstring promises.
RATES = [
    (FrameRate(24, 1), 5000),
    (FrameRate(25, 1), 4800),
    (FrameRate(30, 1), 4000),
    (FrameRate(50, 1), 2400),
    (FrameRate(60, 1), 2000),
    (FrameRate(120, 1), 1000),
    (FrameRate(24000, 1001), 5005),
    (FrameRate(30000, 1001), 4004),
    (FrameRate(60000, 1001), 2002),
]


@pytest.mark.parametrize("rate,expected", RATES, ids=lambda v: str(v))
def test_ticks_per_frame_is_a_whole_number(rate: FrameRate, expected: int) -> None:
    tpf = rate.ticks_per_frame
    assert isinstance(tpf, Fraction)
    assert tpf.denominator == 1, f"{rate} lands on a fractional tick: {tpf}"
    assert tpf == expected


def test_ticks_per_second_constant() -> None:
    assert TICKS_PER_SECOND == 120000


@pytest.mark.parametrize("rate,_tpf", RATES, ids=lambda v: str(v))
def test_one_second_of_frames_spans_one_second_of_ticks(
    rate: FrameRate, _tpf: int
) -> None:
    # A whole number of frames back to ticks is exact, with no accumulation.
    for frame in (0, 1, 7, 1000, 123456):
        assert frames_to_ticks(frame, rate) == frame * _tpf


def test_no_drift_over_an_hour() -> None:
    # The reason the tick domain exists: summing frame durations one at a time
    # must land on exactly the same tick as multiplying.
    rate = FrameRate(30000, 1001)
    frames = 30 * 3600
    stepwise = sum(rate.ticks_per_frame for _ in range(frames))
    assert stepwise == frames_to_ticks(frames, rate)


class TestFromFfprobe:
    def test_rational(self) -> None:
        assert FrameRate.from_ffprobe("30000/1001") == FrameRate(30000, 1001)

    def test_integer_string(self) -> None:
        assert FrameRate.from_ffprobe("25") == FrameRate(25, 1)

    def test_reduces_with_gcd(self) -> None:
        assert FrameRate.from_ffprobe("60/2") == FrameRate(30, 1)
        assert FrameRate.from_ffprobe("50/1") == FrameRate(50, 1)

    def test_whitespace_tolerated(self) -> None:
        assert FrameRate.from_ffprobe("  24/1 ") == FrameRate(24, 1)

    @pytest.mark.parametrize("bad", ["0/0", "30/0", "0/1"])
    def test_rejects_degenerate(self, bad: str) -> None:
        with pytest.raises(ValueError):
            FrameRate.from_ffprobe(bad)


def test_frame_rate_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        FrameRate(0, 1)
    with pytest.raises(ValueError):
        FrameRate(30, 0)
    with pytest.raises(ValueError):
        FrameRate(-30, 1)


def test_frame_rate_is_frozen_and_hashable() -> None:
    rate = FrameRate(30, 1)
    with pytest.raises(Exception):
        rate.num = 25  # type: ignore[misc]
    assert {FrameRate(30, 1), FrameRate(30, 1)} == {FrameRate(30, 1)}


def test_as_float_is_display_only_but_correct() -> None:
    assert FrameRate(30, 1).as_float == 30.0
    assert FrameRate(30000, 1001).as_float == pytest.approx(29.97002997)


class TestSecondsConversion:
    def test_whole_seconds(self) -> None:
        assert seconds_to_ticks(1.0) == 120000
        assert seconds_to_ticks(0.0) == 0
        assert seconds_to_ticks(2.5) == 300000

    def test_rounds_rather_than_truncates(self) -> None:
        # 1/3 of a tick past the boundary rounds back down, 2/3 rounds up.
        assert seconds_to_ticks(1.0 + 0.4 / TICKS_PER_SECOND) == 120000
        assert seconds_to_ticks(1.0 + 0.6 / TICKS_PER_SECOND) == 120001

    def test_round_trip(self) -> None:
        assert ticks_to_seconds(seconds_to_ticks(12.345)) == pytest.approx(12.345)
        assert ticks_to_seconds(120000) == 1.0


class TestFrameConversion:
    @pytest.mark.parametrize("rate,tpf", RATES, ids=lambda v: str(v))
    def test_round_trip_on_boundaries(self, rate: FrameRate, tpf: int) -> None:
        # A frame index is an identity: converting to ticks and back must
        # return the same frame, at every rate, for every frame.
        for frame in range(500):
            assert ticks_to_frames(frames_to_ticks(frame, rate), rate) == frame
        for frame in (99, 100000, 12345678):
            assert ticks_to_frames(frames_to_ticks(frame, rate), rate) == frame

    def test_ticks_to_frames_floors_within_a_frame(self) -> None:
        rate = FrameRate(30, 1)  # 4000 ticks per frame
        assert ticks_to_frames(0, rate) == 0
        assert ticks_to_frames(3999, rate) == 0
        assert ticks_to_frames(4000, rate) == 1
        assert ticks_to_frames(4001, rate) == 1

    def test_snap_to_frame_goes_to_the_nearest_boundary(self) -> None:
        rate = FrameRate(30, 1)
        assert snap_to_frame(0, rate) == 0
        assert snap_to_frame(1999, rate) == 0
        assert snap_to_frame(2000, rate) == 4000  # exact half rounds up
        assert snap_to_frame(2001, rate) == 4000
        assert snap_to_frame(4000, rate) == 4000

    def test_snap_is_idempotent(self) -> None:
        rate = FrameRate(30000, 1001)
        for t in (0, 1, 4003, 4004, 987654321):
            once = snap_to_frame(t, rate)
            assert snap_to_frame(once, rate) == once

    @pytest.mark.parametrize("rate,tpf", RATES, ids=lambda v: str(v))
    def test_snap_result_is_always_on_a_boundary(self, rate: FrameRate, tpf: int) -> None:
        for t in (0, 1, 12345, 999999, 4_000_000_001):
            assert snap_to_frame(t, rate) % tpf == 0


class TestTimecode:
    def test_25fps_is_exact_against_wall_clock(self) -> None:
        rate = FrameRate(25, 1)
        assert ticks_to_timecode(0, rate) == "00:00:00:00"
        assert ticks_to_timecode(frames_to_ticks(24, rate), rate) == "00:00:00:24"
        assert ticks_to_timecode(frames_to_ticks(25, rate), rate) == "00:00:01:00"
        assert ticks_to_timecode(1 * TICKS_PER_SECOND, rate) == "00:00:01:00"
        assert ticks_to_timecode(61 * TICKS_PER_SECOND, rate) == "00:01:01:00"
        assert ticks_to_timecode(3600 * TICKS_PER_SECOND, rate) == "01:00:00:00"

    def test_29_97_counts_frames_not_wall_clock(self) -> None:
        rate = FrameRate(30000, 1001)
        assert ticks_to_timecode(0, rate) == "00:00:00:00"
        # 30 whole frames is one timecode second, slightly past one real second.
        assert ticks_to_timecode(frames_to_ticks(30, rate), rate) == "00:00:01:00"
        assert ticks_to_timecode(frames_to_ticks(29, rate), rate) == "00:00:00:29"
        # One real second is still frame 29: non-drop timecode runs slow.
        assert ticks_to_timecode(1 * TICKS_PER_SECOND, rate) == "00:00:00:29"
        # And over an hour it slips the familiar ~3.6 seconds.
        assert ticks_to_timecode(3600 * TICKS_PER_SECOND, rate) == "00:59:56:12"

    def test_negative_positions_are_signed(self) -> None:
        rate = FrameRate(25, 1)
        assert ticks_to_timecode(-frames_to_ticks(25, rate), rate) == "-00:00:01:00"

    def test_hours_do_not_wrap(self) -> None:
        rate = FrameRate(30, 1)
        assert ticks_to_timecode(30 * 3600 * TICKS_PER_SECOND, rate) == "30:00:00:00"


class TestNonBroadcastRate:
    """A rate that 120000 does not divide by.

    The nine rates above all give a whole number of ticks per frame. A variable
    frame rate file does not: ffprobe reports whatever average it computed, and
    24249/1000 is a real example. Everything here must work without special
    casing, and nothing may assume the frame boundaries are whole ticks apart.
    """

    RATE = FrameRate(24249, 1000)

    def test_ticks_per_frame_is_a_fraction_and_not_a_whole_number(self) -> None:
        tpf = self.RATE.ticks_per_frame
        assert isinstance(tpf, Fraction)
        assert tpf == Fraction(40000000, 8083)
        assert tpf.denominator != 1, "this rate is only interesting if it does not divide"
        assert float(tpf) == pytest.approx(4948.6576766)

    def test_the_conversions_all_run(self) -> None:
        rate = self.RATE
        for frame in (0, 1, 2, 999, 100000):
            ticks = frames_to_ticks(frame, rate)
            assert isinstance(ticks, int)
            # frames_to_ticks rounds up, so the tick is at or after the true
            # boundary and less than one tick past it.
            exact = Fraction(frame) * rate.ticks_per_frame
            assert 0 <= Fraction(ticks) - exact < 1

    def test_the_frame_index_round_trips_exactly(self) -> None:
        # Frame 2 begins at 9897.31 ticks. Nearest would give 9897, and 9897
        # is still inside frame 1, so the index would come back as 1 and a
        # frame would have been lost simply by converting and converting back.
        # Rounding up gives 9898, which is inside frame 2 where it belongs.
        # This is the whole reason frames_to_ticks is a ceiling.
        rate = self.RATE
        assert frames_to_ticks(2, rate) == 9898
        assert ticks_to_frames(9898, rate) == 2

        for frame in range(500):
            assert ticks_to_frames(frames_to_ticks(frame, rate), rate) == frame

    def test_frames_to_ticks_multiplies_rather_than_accumulates(self) -> None:
        # Adding a rounded per frame constant 14549 times drifts by thousands
        # of ticks. Multiplying the exact Fraction does not.
        rate = self.RATE
        frames = 14549
        accumulated = frames * frames_to_ticks(1, rate)
        assert abs(accumulated - frames_to_ticks(frames, rate)) > 1000

    @pytest.mark.parametrize(
        "t", [0, 1, 2473, 2474, 4948, 4949, 9897, 120000, 123456789, 4_000_000_001]
    )
    def test_snap_to_frame_is_still_idempotent(self, t: int) -> None:
        once = snap_to_frame(t, self.RATE)
        assert snap_to_frame(once, self.RATE) == once
        assert snap_to_frame(once, self.RATE) == once  # and stays put

    def test_snapped_values_land_on_the_nearest_frame(self) -> None:
        rate = self.RATE
        for t in (0, 5000, 50_000, 1_000_000):
            snapped = snap_to_frame(t, rate)
            frame = ticks_to_frames(snapped, rate)
            assert snapped == frames_to_ticks(frame, rate)
            assert abs(snapped - t) <= round(rate.ticks_per_frame / 2) + 1

    def test_timecode_is_well_formed(self) -> None:
        rate = self.RATE
        assert ticks_to_timecode(0, rate) == "00:00:00:00"
        assert ticks_to_timecode(1 * TICKS_PER_SECOND, rate) == "00:00:01:00"
        assert ticks_to_timecode(60 * TICKS_PER_SECOND, rate) == "00:01:00:14"
        assert ticks_to_timecode(600 * TICKS_PER_SECOND, rate) == "00:10:06:05"

    def test_timecode_frame_field_stays_in_range(self) -> None:
        # The seconds field counts round(24.249) = 24 frames.
        rate = self.RATE
        for frame in range(0, 500):
            text = ticks_to_timecode(frames_to_ticks(frame, rate), rate)
            hh, mm, ss, ff = (int(part) for part in text.split(":"))
            assert 0 <= ff < 24
            assert 0 <= ss < 60
            assert 0 <= mm < 60
            assert hh == 0

    def test_timecode_never_goes_backwards(self) -> None:
        rate = self.RATE
        previous = ""
        for frame in range(0, 2000):
            text = ticks_to_timecode(frames_to_ticks(frame, rate), rate)
            assert text >= previous
            previous = text

    def test_no_drift_over_ten_minutes(self) -> None:
        # The same guarantee as the broadcast rates, one tick weaker: stepping
        # a frame at a time and multiplying agree to better than a whole tick,
        # which is all that is available when the frame is not a whole number
        # of ticks long.
        rate = self.RATE
        frames = round(600 * rate.as_float)
        assert frames == 14549

        stepwise = sum(rate.ticks_per_frame for _ in range(frames))
        multiplied = frames_to_ticks(frames, rate)

        assert isinstance(stepwise, Fraction)
        assert stepwise.denominator != 1
        assert abs(stepwise - multiplied) < 1
        assert abs(stepwise - multiplied) == pytest.approx(0.463, abs=0.001)
