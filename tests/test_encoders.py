"""Which encoders are offered, and the difference between the two questions.

Amendment 3 asks for the option to be hidden on a machine that cannot use it,
and specifies ``ffmpeg -encoders`` as the probe. That listing is a fact about
the BUILD, and the build ships with the application, so on its own it answers
the same way on every machine including one with no NVIDIA card in it. The
trial encode is what actually answers the question that was asked; both are
tested here, and the difference is the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.encoders import (
    NVENC_H264,
    SOFTWARE_H264,
    encoder_is_listed,
    encoder_runs,
    hardware_encoder_available,
    listed_encoders,
    parse_encoders,
    reset_cache,
)

SAMPLE = """Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ..S... = Slice-level multithreading
 ...X.. = Codec is experimental
 ....B. = Supports draw_horiz_band
 .....D = Supports direct rendering method 1
 ------
 V....D libx264              libx264 H.264 / AVC (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
 S..... srt                  SubRip subtitle
"""


class TestParsing:
    def test_it_reads_the_names_out_of_a_listing(self) -> None:
        assert parse_encoders(SAMPLE) == frozenset(
            {"libx264", "h264_nvenc", "aac", "srt"}
        )

    def test_the_legend_above_the_list_is_not_mistaken_for_encoders(self) -> None:
        names = parse_encoders(SAMPLE)
        assert "=" not in names
        assert "Video" not in names
        assert len(names) == 4

    def test_empty_output_is_no_encoders_rather_than_an_error(self) -> None:
        assert parse_encoders("") == frozenset()

    def test_rubbish_is_no_encoders(self) -> None:
        assert parse_encoders("ffmpeg: command not found\n") == frozenset()


@pytest.mark.slow
class TestTheVendoredBuild:
    """Against the real binary, which is what ships."""

    def test_the_software_encoder_is_both_listed_and_runnable(self) -> None:
        assert encoder_is_listed(SOFTWARE_H264)
        assert encoder_runs(SOFTWARE_H264)

    def test_the_listing_is_not_empty(self) -> None:
        assert len(listed_encoders()) > 50

    def test_an_encoder_that_does_not_exist_answers_no_to_both(self) -> None:
        assert encoder_is_listed("h264_madeup") is False
        assert encoder_runs("h264_madeup") is False

    def test_listing_and_running_are_different_questions(self) -> None:
        """The whole reason the trial exists.

        This build has NVENC compiled in, so it is listed on every machine.
        Whether it opens depends on the GPU and the driver in front of it. If
        both answers are the same here that is fine; what must never happen is
        the option being offered on the strength of the listing alone.
        """
        listed = encoder_is_listed(NVENC_H264)
        runs = encoder_runs(NVENC_H264)
        assert hardware_encoder_available() is (listed and runs)
        if not runs:
            assert hardware_encoder_available() is False

    def test_the_answers_are_cached(self) -> None:
        import time

        reset_cache()
        start = time.perf_counter()
        listed_encoders()
        cold = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(50):
            listed_encoders()
        warm = time.perf_counter() - start

        assert warm < cold, "50 cached calls cost more than one uncached one"


class TestTheAvailabilityRule:
    def test_an_unlisted_encoder_is_never_trialled(self, monkeypatch) -> None:
        """Listing is the cheap question and it is decisive when it says no."""
        trials: list[str] = []
        monkeypatch.setattr(
            "core.encoders.listed_encoders", lambda: frozenset({"libx264"})
        )
        monkeypatch.setattr(
            "core.encoders.encoder_runs",
            lambda name: trials.append(name) or True,
        )

        assert hardware_encoder_available() is False
        assert trials == []

    def test_listed_but_not_runnable_is_unavailable(self, monkeypatch) -> None:
        # A machine with no NVIDIA card, or one whose driver is older than the
        # NVENC API this build was compiled against. Both look like this.
        monkeypatch.setattr(
            "core.encoders.listed_encoders", lambda: frozenset({NVENC_H264})
        )
        monkeypatch.setattr("core.encoders.encoder_runs", lambda name: False)
        assert hardware_encoder_available() is False

    def test_listed_and_runnable_is_available(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "core.encoders.listed_encoders", lambda: frozenset({NVENC_H264})
        )
        monkeypatch.setattr("core.encoders.encoder_runs", lambda name: True)
        assert hardware_encoder_available() is True


class TestCoreStaysPure:
    def test_this_module_imports_no_qt(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent / "core" / "encoders.py"
        ).read_text(encoding="utf-8")
        assert "PySide6" not in source
        assert "from ui" not in source
