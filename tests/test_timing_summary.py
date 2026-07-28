import pytest

from rabbithole.timing import timing_summary


def _document(
    duration_seconds: float,
    word_count: int,
    markers: list[dict] | None = None,
) -> dict:
    return {
        "duration_seconds": duration_seconds,
        "word_count": word_count,
        "words": [],
        "markers": markers or [],
    }


def test_measured_wpm_excludes_silence_from_the_denominator():
    # 60s total, 20s of silence -> 40s speech. 100 words in 40s = 150 wpm.
    # Including the silence in the denominator would give 100 wpm instead, a
    # materially different (and wrong) number.
    document = _document(
        duration_seconds=60.0,
        word_count=100,
        markers=[
            {"kind": "SILENCE", "arg": "20s", "word_index": 50, "line": 1, "seconds": 30.0},
        ],
    )

    summary = timing_summary(document)

    assert summary["silence_seconds"] == pytest.approx(20.0)
    assert summary["speech_seconds"] == pytest.approx(40.0)
    assert summary["measured_wpm"] == pytest.approx(150.0)


def test_in_band_is_true_inside_the_166_to_191_band():
    document = _document(duration_seconds=60.0, word_count=180)  # 180 wpm

    summary = timing_summary(document)

    assert summary["measured_wpm"] == pytest.approx(180.0)
    assert summary["in_band"] is True


def test_in_band_is_false_outside_the_166_to_191_band():
    document = _document(duration_seconds=60.0, word_count=100)  # 100 wpm

    summary = timing_summary(document)

    assert summary["measured_wpm"] == pytest.approx(100.0)
    assert summary["in_band"] is False


def test_marker_counts_are_correct():
    document = _document(
        duration_seconds=60.0,
        word_count=100,
        markers=[
            {"kind": "SFX", "arg": "a", "word_index": 1, "line": 1, "seconds": 1.0},
            {"kind": "SFX", "arg": "b", "word_index": 2, "line": 2, "seconds": 2.0},
            {"kind": "MUSIC", "arg": "out", "word_index": 3, "line": 3, "seconds": 3.0},
        ],
    )

    summary = timing_summary(document)

    assert summary["marker_counts"] == {"SFX": 2, "MUSIC": 1}


def test_marker_counts_are_ordered_most_frequent_first():
    document = _document(
        duration_seconds=60.0,
        word_count=100,
        markers=[
            {"kind": "MUSIC", "arg": "a", "word_index": 1, "line": 1, "seconds": 1.0},
            {"kind": "SFX", "arg": "b", "word_index": 2, "line": 2, "seconds": 2.0},
            {"kind": "SFX", "arg": "c", "word_index": 3, "line": 3, "seconds": 3.0},
            {"kind": "SFX", "arg": "d", "word_index": 4, "line": 4, "seconds": 4.0},
        ],
    )

    summary = timing_summary(document)

    assert list(summary["marker_counts"].keys()) == ["SFX", "MUSIC"]


def test_rehook_gaps_are_computed_between_consecutive_rehook_markers_in_minutes():
    document = _document(
        duration_seconds=600.0,
        word_count=1000,
        markers=[
            {"kind": "REHOOK", "arg": "", "word_index": 0, "line": 1, "seconds": 60.0},
            {"kind": "REHOOK", "arg": "", "word_index": 0, "line": 2, "seconds": 300.0},
            {"kind": "REHOOK", "arg": "", "word_index": 0, "line": 3, "seconds": 360.0},
        ],
    )

    summary = timing_summary(document)

    assert summary["rehook_gaps_minutes"] == pytest.approx([4.0, 1.0])


def test_no_rehook_markers_yields_an_empty_gap_list():
    document = _document(duration_seconds=60.0, word_count=100)

    summary = timing_summary(document)

    assert summary["rehook_gaps_minutes"] == []


def test_zero_speech_seconds_does_not_raise_and_reports_zero_wpm():
    # All duration is silence: speech_seconds is 0. Naive word_count /
    # (speech_seconds / 60) would raise ZeroDivisionError.
    document = _document(
        duration_seconds=30.0,
        word_count=50,
        markers=[
            {"kind": "SILENCE", "arg": "30s", "word_index": 0, "line": 1, "seconds": 0.0},
        ],
    )

    summary = timing_summary(document)

    assert summary["speech_seconds"] == pytest.approx(0.0)
    assert summary["measured_wpm"] == 0.0
    assert summary["in_band"] is False


def test_zero_word_count_does_not_raise_and_reports_zero_wpm():
    document = _document(duration_seconds=60.0, word_count=0)

    summary = timing_summary(document)

    assert summary["measured_wpm"] == pytest.approx(0.0)
    assert summary["in_band"] is False
